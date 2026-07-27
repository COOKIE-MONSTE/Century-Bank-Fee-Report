import io
import logging
import re
from urllib.parse import urljoin

import pypdf
from bs4 import BeautifulSoup

from .base import BaseScraper

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

    def _extract_agreement_fields(self, pdf_url, agreement_label):
        response = self.fetch_url(pdf_url)
        if not response:
            return {}, []

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
            return {}, []

        def dollar(pattern, anchor=None, window=600):
            m = _find_near(text, anchor, pattern, window) if anchor else re.search(pattern, text, re.IGNORECASE)
            return m.group(1) if m else None

        fields = {}
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
            unmatched.append(f"Minimum finance charge: ${min_finance}")

        paper_stmt = _find_near(text, "Paper Statement Fee", r"\$([\d.]+)\s*monthly")
        if paper_stmt:
            unmatched.append(f"Paper statement fee: ${paper_stmt.group(1)}/month")

        research = _find_near(text, "Research Fee", r"\$(\d+) for each photocopy.*?\$(\d+) for each duplicate")
        if research:
            unmatched.append(f"Research fee: ${research.group(1)}/photocopy, ${research.group(2)}/duplicate statement")

        expedited = _find_near(text, "Expedited Payment Fee", r"\$(\d+) for each\s*payment initiated by telephone")
        if expedited:
            unmatched.append(f"Expedited (phone) payment fee: ${expedited.group(1)}")

        return fields, unmatched

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
            shared_fields, unmatched_notes = agreement_cache[agreement_key]

            card = {
                "card_name": card_name,
                "category": product_cfg.get("category", "credit_card_standard"),
                "_issuer": "TCM Bank, N.A.",
                "annual_fee": NOT_PUBLICLY_DISCLOSED,
                "purchase_apr": NOT_PUBLICLY_DISCLOSED,
            }
            card.update(shared_fields)

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
