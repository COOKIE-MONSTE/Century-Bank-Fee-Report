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
    """

    def __init__(self, name, url, config):
        super().__init__(name, url, config)

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

        rows_by_account = {}
        for row in tables[0]:
            if not row or not row[0]:
                continue
            account = " ".join(row[0].split())
            rows_by_account[account] = row

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
