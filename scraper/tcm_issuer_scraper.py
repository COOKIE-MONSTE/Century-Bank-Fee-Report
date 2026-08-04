import io
import logging
import re
from urllib.parse import urljoin

import pypdf
from bs4 import BeautifulSoup

from .base import BaseScraper, _NON_REAL_VALUES

logger = logging.getLogger("FeeComparisonScraper")

NOT_PUBLICLY_DISCLOSED = "Not publicly disclosed"


def _find_near(text, anchor, pattern, window=600):
    """Finds `pattern` within `window` characters after an occurrence of
    `anchor` -- trying every occurrence of `anchor` in turn, not just the
    first. A phrase like "Late Payment Fee" typically appears once early
    as a cross-reference ("...as described in Section 14.4") well before
    the actual definition section that contains the dollar amount, so
    stopping at the first occurrence misses the real content entirely.
    """
    for anchor_match in re.finditer(re.escape(anchor), text):
        idx = anchor_match.start()
        m = re.search(pattern, text[idx:idx + window], re.IGNORECASE | re.DOTALL)
        if m:
            return m
    return None


class TcmIssuerScraper(BaseScraper):
    """Scrapes credit card fee terms for cards issued by TCM Bank, N.A. on
    behalf of a partner institution (here, Century Bank).

    TCM sets these fee terms, not the partner bank, but a Century
    cardholder pays them -- so the comparison output still attributes
    them to Century Bank (each card's category/product_name matches
    Century as normal). What's issuer-specific is tracked separately via
    `card["_issuer"]`, used by report.py to footnote these fields as
    "set by the issuer" rather than by Century itself.

    Annual fee and APR are set individually per partner bank on a sheet
    delivered only at account opening -- never published anywhere. Rather
    than leaving those fields blank (which report.py would render as
    "not disclosed", indistinguishable from a scraper failure) or
    guessing from another partner's terms, they're recorded as the
    literal value "Not publicly disclosed", which is intentionally NOT
    the same string as the generic "Not disclosed" placeholder elsewhere
    in this codebase -- that one means "field not found"; this one means
    "confirmed to not exist publicly", a stronger and different claim.

    Agreement URLs are resolved fresh from the crawl root on every run,
    never pinned -- verified against a URL that had already rotated to a
    newer document within months of being noted down.
    """

    def __init__(self, name, url, config):
        super().__init__(name, url, config)

    def _had_real_previous_value(self, field):
        """This scraper builds one shared `fields` dict per AGREEMENT
        (consumer/secured), reused across several products with different
        categories -- unlike try_llm_recovery()'s per-card design, there's
        no single category to key on here. Scans across every category
        this institution had for `field` instead: if ANY of them had a
        real value yesterday, this counts as an extraction regression
        worth attempting recovery for. A field permanently unpublished
        (e.g. annual_fee/purchase_apr, set per-partner-bank and disclosed
        only at account opening) never had a real value in ANY category,
        so it's correctly never eligible -- Gemini is never asked to
        guess a fact no page will ever state.
        """
        return any(
            fld == field and value not in _NON_REAL_VALUES
            for (_category, fld), value in self._previous_values.items()
        )

    def _resolve_agreement_urls(self):
        response = self.fetch_url()
        if not response:
            return {}
        soup = BeautifulSoup(response.content, "html.parser")
        urls = {}
        for a in soup.find_all("a", href=True):
            label = a.get_text(strip=True).lower()
            href = a["href"]
            if "agreement" not in label:
                continue
            full_url = urljoin(self.url, href)
            if "consumer" in label:
                urls["consumer"] = full_url
            elif "secured" in label:
                urls["secured"] = full_url
            elif "business" in label:
                urls["business"] = full_url
        return urls

    # Human-readable descriptions of the 6 canonical fields, used only if
    # regex extraction below finds nothing -- passed to the LLM fallback so
    # it knows what to look for without this module handing it any regex
    # or assuming anything about the document's exact wording.
    _FIELD_DESCRIPTIONS = {
        "late_payment_fee": "The Late Payment Fee(s) charged when a cardholder's payment is late. If there are different amounts for a first offense vs. repeat late payments, include both.",
        "returned_item_fee": "The Returned Payment Fee charged when a cardholder's payment is returned unpaid (e.g. a bounced check or failed electronic payment).",
        "stop_payment_fee": "The fee charged for a stop payment request on a Convenience Check.",
        "cash_advance_fee": "The Cash Advance Fee charged per cash advance transaction (as a dollar amount and/or percentage of the advance).",
        "balance_transfer_fee": "The Balance Transfer Fee charged per balance transfer (as a dollar amount and/or percentage of the transfer).",
        "foreign_transaction_fee": "The Foreign Transaction Fee charged as a percentage of transactions made in a foreign currency or with a foreign merchant.",
    }

    def _extract_agreement_fields(self, pdf_url, agreement_label):
        """Returns (fields, extra, unmatched, llm_derived).

        fields: the canonical card fee fields (late_payment_fee, etc.).
        extra: fields that matter to credit_card_matrix.py's institution-
            level matrix (paper statement, research fee, etc.) but aren't
            part of the canonical per-product fee taxonomy -- kept
            separate rather than added to `fields` so this scraper's
            output doesn't quietly grow new canonical fields.
        unmatched: the same `extra` values, pre-formatted as human-
            readable notes for self.warnings (kept for backward
            compatibility with the existing "fee-adjacent facts" warning).
        llm_derived: set of field names in `fields` that came from the
            Gemini fallback rather than regex -- scrape() uses this to tag
            those fields' confidence as "llm_assisted" on the resulting
            card, so the report never presents an AI guess with the same
            confidence as a direct pattern match.
        """
        response = self.fetch_url(pdf_url)
        if not response:
            return {}, {}, [], set()

        try:
            reader = pypdf.PdfReader(io.BytesIO(response.content))
            raw_text = "\n".join(page.extract_text() for page in reader.pages)
            # Collapse all whitespace (including the mid-phrase line-wrap
            # newlines PDF extraction leaves in, e.g. "pay \na fee of $29")
            # to single spaces, so every literal-space pattern below works
            # the same regardless of a given document's exact line breaks --
            # confirmed the same phrase wraps differently across TCM's
            # Consumer vs Secured agreements.
            text = " ".join(raw_text.split())
        except Exception as e:
            msg = f"[{self.name}] Failed to parse {agreement_label} agreement PDF: {e}"
            logger.error(msg)
            self.warnings.append(msg)
            return {}, {}, [], set()

        def dollar(pattern, anchor=None, window=600):
            m = _find_near(text, anchor, pattern, window) if anchor else re.search(pattern, text, re.IGNORECASE)
            return m.group(1) if m else None

        fields = {}
        extra = {}
        unmatched = []

        late1 = dollar(r"\$([\d,]+(?:\.\d{2})?)\s*for a late payment if you have not been charged", "Late Payment Fee")
        late2 = dollar(r"\$([\d,]+(?:\.\d{2})?)\s*for a late payment if you have been charged", "Late Payment Fee")
        if late1 and late2:
            fields["late_payment_fee"] = f"${late1} (first offense) / ${late2} (repeat within 6 billing cycles)"
        elif late1:
            fields["late_payment_fee"] = f"${late1}"

        ret1 = dollar(r"\$([\d,]+(?:\.\d{2})?)\s*for a returned payment if you have not been charged", "Returned Payment Fee")
        ret2 = dollar(r"\$([\d,]+(?:\.\d{2})?)\s*for a returned payment if you have been charged", "Returned Payment Fee")
        if ret1 and ret2:
            fields["returned_item_fee"] = f"${ret1} (first offense) / ${ret2} (repeat within 6 billing cycles)"
        elif ret1:
            fields["returned_item_fee"] = f"${ret1}"

        stop = dollar(r"agree to pay a fee of \$([\d,]+(?:\.\d{2})?)", "Stop Payment")
        if stop:
            fields["stop_payment_fee"] = f"${stop}"

        cash_adv = _find_near(text, "Cash Advance Fee", r"either \$([\d,]+(?:\.\d{2})?) or (\d+)% .*?whichever is greater")
        if cash_adv:
            fields["cash_advance_fee"] = f"${cash_adv.group(1)} or {cash_adv.group(2)}% of the advance, whichever is greater; no maximum"

        bal_transfer = _find_near(text, "Balance Transfer Fee", r"greater of \$([\d,]+(?:\.\d{2})?) or (\d+)%")
        if bal_transfer:
            fields["balance_transfer_fee"] = f"${bal_transfer.group(1)} or {bal_transfer.group(2)}% of the transfer, whichever is greater; no maximum"

        foreign_tx = dollar(r"Foreign Transaction Fee of (\d+(?:\.\d+)?)%", "Foreign Transaction Fee")
        if foreign_tx:
            fields["foreign_transaction_fee"] = f"{foreign_tx}% of transaction amount"

        min_finance = dollar(r"\$([\d.]+)\s*minimum FINANCE CHARGE")
        if min_finance:
            extra["min_finance_charge"] = f"${min_finance}"
            unmatched.append(f"Minimum finance charge: ${min_finance}")

        paper_stmt = _find_near(text, "Paper Statement Fee", r"\$([\d.]+)\s*monthly")
        if paper_stmt:
            extra["paper_statement"] = f"${paper_stmt.group(1)}/month"
            unmatched.append(f"Paper statement fee: ${paper_stmt.group(1)}/month")

        research = _find_near(text, "Research Fee", r"\$(\d+) for each photocopy.*?\$(\d+) for each duplicate")
        if research:
            extra["research_copies"] = f"${research.group(1)}/photocopy, ${research.group(2)}/duplicate statement"
            unmatched.append(f"Research fee: ${research.group(1)}/photocopy, ${research.group(2)}/duplicate statement")

        expedited = _find_near(text, "Expedited Payment Fee", r"\$(\d+) for each\s*payment initiated by telephone")
        if expedited:
            extra["expedited_payment"] = f"${expedited.group(1)}"
            unmatched.append(f"Expedited (phone) payment fee: ${expedited.group(1)}")

        effective = re.search(r"Effective Date:\s*([A-Za-z]+ \d{1,2},\s*\d{4})", text, re.IGNORECASE)
        if effective:
            extra["effective_date"] = effective.group(1)

        # Regex-first, always -- this only runs for whichever of the 6
        # canonical fields the patterns above didn't find, and even then
        # ONLY when that field extracted a real value in a previous run
        # (see _had_real_previous_value): a field that has NEVER matched
        # (e.g. one genuinely absent from this agreement) is not a site
        # regression, so it's never worth spending API quota chasing --
        # and never risks a probabilistic guess quietly filling in what
        # should stay a visible gap. An LLM read is also slower and costs
        # API quota, so it's the exception path, not the default one, and
        # every field it does supply is tagged "llm_assisted" (see
        # scrape()) rather than trusted like a regex match.
        llm_derived = set()
        for field, description in self._FIELD_DESCRIPTIONS.items():
            if field in fields:
                continue
            if not self._had_real_previous_value(field):
                continue
            msg = (
                f"[{self.name}] Field '{field}' extracted a real value in the previous run "
                f"but not this one ({agreement_label} agreement) -- likely a document change, "
                f"not a newly-unpublished fee. Attempting Gemini recovery."
            )
            logger.warning(msg)
            self.warnings.append(msg)
            value = self.llm_extract_field(
                text, description,
                context=(
                    f"This is TCM Bank, N.A.'s {agreement_label} Cardholder Agreement, issued on "
                    f"behalf of Century Bank, located in New Mexico or a nearby region. Only use "
                    f"the page text provided below -- do not use outside knowledge about any "
                    f"other bank with a similar name."
                ),
            )
            if value:
                fields[field] = value
                llm_derived.add(field)

        return fields, extra, unmatched, llm_derived

    def scrape(self):
        agreement_urls = self._resolve_agreement_urls()
        if not agreement_urls:
            msg = f"[{self.name}] Could not resolve any cardholder agreement links from {self.url}."
            logger.error(msg)
            self.warnings.append(msg)
            return []

        agreement_cache = {}
        cards = []

        for card_name, product_cfg in self.config.get("products", {}).items():
            agreement_key = product_cfg.get("agreement", "consumer")
            pdf_url = agreement_urls.get(agreement_key)
            if not pdf_url:
                msg = f"[{self.name} - {card_name}] No '{agreement_key}' agreement link found on the crawl root."
                logger.error(msg)
                self.warnings.append(msg)
                continue

            if agreement_key not in agreement_cache:
                agreement_cache[agreement_key] = self._extract_agreement_fields(pdf_url, agreement_key)
            shared_fields, extra_fields, unmatched_notes, llm_derived = agreement_cache[agreement_key]

            card = {
                "card_name": card_name,
                "category": product_cfg.get("category", "credit_card_standard"),
                "_issuer": "TCM Bank, N.A.",
                "annual_fee": NOT_PUBLICLY_DISCLOSED,
                "purchase_apr": NOT_PUBLICLY_DISCLOSED,
            }
            card.update(shared_fields)
            if llm_derived:
                card["_field_confidence"] = {field: "llm_assisted" for field in llm_derived}
            # Not part of the canonical fee taxonomy -- only
            # credit_card_matrix.py reads this (via NON_FEE_KEYS exclusion
            # from the main attribution pipeline).
            card["_matrix_extra"] = extra_fields
            # Every field on this card came from the same agreement PDF --
            # recorded per-card so a consumer of this data (e.g. the
            # credit card matrix's source tracking) never has to guess
            # which fetch produced which value.
            card["_source_urls"] = {
                field: pdf_url for field in list(shared_fields.keys()) + list(extra_fields.keys())
                + ["annual_fee", "purchase_apr"]
            }

            for field in ("late_payment_fee", "returned_item_fee", "stop_payment_fee", "cash_advance_fee",
                          "balance_transfer_fee", "foreign_transaction_fee"):
                if field not in card:
                    self.log_field_warning(card_name, field)

            if unmatched_notes:
                msg = (
                    f"[{self.name} - {card_name}] Fee-adjacent facts found with no existing canonical "
                    f"field: {'; '.join(unmatched_notes)}"
                )
                logger.info(msg)
                self.warnings.append(msg)

            cards.append(self.finalize_card(card))

        return cards
