import io
import logging

import pdfplumber

from .base import BaseScraper

logger = logging.getLogger("FeeComparisonScraper")


class TisTableScraper(BaseScraper):
    """Extracts a "Minimum Balance to Avoid a Service Fee" column, per
    account, from SECU NM's Truth-in-Savings PDF -- using pdfplumber's
    ruling-line-based table detection rather than raw text extraction.

    This document is a form-fill template where naive text extraction
    (pypdf) dumps the table skeleton and its numeric values as separate,
    misaligned blocks -- confirmed independently: dozens of blank `____`
    placeholders remain in the template body, and running plain
    `page.extract_text()` on it produces label/value soup with no
    reliable reading order. pdfplumber's extract_tables() instead reads
    the underlying grid geometry (the PDF's actual ruling lines), which
    correctly pairs each account name with its own row's values.

    Even so, this source is handled with more caution than a typical
    table scrape: every field pulled from it is forced to "low"
    confidence regardless of how clean a given extraction looks, so it's
    always gated behind human confirmation (via data/feedback_log.yaml)
    before being treated as fully reliable -- "has visible table rulings"
    isn't the same guarantee as "is safe to scrape unattended".

    Only accounts explicitly listed in config's `products` are ingested
    (never "every row in the table"), since this document mixes real
    accounts with unfilled template placeholder rows.

    Some products (Money Market Account, Advantage+ Money Market Account)
    are laid out as a HEADER row (their own name in column 0, every other
    column blank) followed by several balance-TIER sub-rows whose own
    column 0 is a placeholder ("$N/A" or a balance boundary), not the
    product name -- confirmed 2026-08-04. A plain exact-match lookup on
    the header row's own name finds a row with no value at all (the fee
    column is blank on the header row itself; the real values are one
    row down, per tier). For a header-only row (every column but 0 is
    empty), this scraper instead reads every sub-row between it and the
    next header-only row (or the end of the table), and uses the
    balance-column value ONLY if every tier agrees -- if the tiers
    disagree, that's surfaced as a warning rather than silently picking
    one, since "which tier's answer is the product's answer" would be a
    guess this scraper has no basis for making.
    """

    def __init__(self, name, url, config):
        super().__init__(name, url, config)

    @staticmethod
    def _is_header_only_row(row):
        return len(row) > 1 and all(c is None for c in row[1:])

    def scrape(self):
        response = self.fetch_url()
        if not response:
            return []

        try:
            pdf = pdfplumber.open(io.BytesIO(response.content))
        except Exception as e:
            msg = f"[{self.name}] Failed to open TIS PDF for table extraction: {e}"
            logger.error(msg)
            self.warnings.append(msg)
            return []

        page_num = self.config.get("table_page", 1) - 1
        if page_num < 0 or page_num >= len(pdf.pages):
            msg = f"[{self.name}] Configured table_page {page_num + 1} is out of range (PDF has {len(pdf.pages)} pages)."
            logger.error(msg)
            self.warnings.append(msg)
            return []

        tables = pdf.pages[page_num].extract_tables()
        if not tables:
            msg = f"[{self.name}] No table detected on page {page_num + 1} of the TIS PDF."
            logger.error(msg)
            self.warnings.append(msg)
            return []

        table_rows = tables[0]
        rows_by_account = {}
        header_row_index = {}
        for idx, row in enumerate(table_rows):
            if not row or not row[0]:
                continue
            account = " ".join(row[0].split())
            rows_by_account[account] = row
            if self._is_header_only_row(row):
                header_row_index.setdefault(account, idx)

        balance_col = self.config.get("min_balance_column_index", 7)
        products = self.config.get("products", {})
        cards = []

        for account_name, product_cfg in products.items():
            row = rows_by_account.get(account_name)
            if not row:
                msg = f"[{self.name}] Expected account '{account_name}' not found in the TIS table -- the document's layout may have changed."
                logger.warning(msg)
                self.warnings.append(msg)
                continue

            if self._is_header_only_row(row):
                idx = header_row_index[account_name]
                tier_values = []
                for sub_row in table_rows[idx + 1:]:
                    if not sub_row or self._is_header_only_row(sub_row):
                        break
                    v = sub_row[balance_col] if balance_col < len(sub_row) else None
                    if v:
                        tier_values.append(v.strip())
                distinct = set(tier_values)
                if not distinct:
                    self.log_field_warning(account_name, "monthly_maintenance_fee")
                    continue
                if len(distinct) > 1:
                    msg = (
                        f"[{self.name} - {account_name}] Balance tiers disagree on 'Minimum Balance "
                        f"to Avoid a Service Fee' ({sorted(distinct)!r}) -- not silently picking one; "
                        f"verify manually."
                    )
                    logger.warning(msg)
                    self.warnings.append(msg)
                    continue
                raw_value = tier_values[0]
            else:
                raw_value = row[balance_col] if balance_col < len(row) else None
                if not raw_value:
                    self.log_field_warning(account_name, "monthly_maintenance_fee")
                    continue

            value = "None" if raw_value.strip().lower() == "none" else self.clean_value(raw_value)
            card = {
                "card_name": product_cfg.get("product_name", account_name),
                "category": product_cfg.get("category", "uncategorized"),
                "monthly_maintenance_fee": value,
                "_field_confidence": {"monthly_maintenance_fee": "low"},
            }
            cards.append(self.finalize_card(card))

        return cards
