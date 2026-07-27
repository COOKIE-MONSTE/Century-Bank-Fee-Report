import io
import logging
import re

import pypdf
from bs4 import BeautifulSoup

from .base import BaseScraper

logger = logging.getLogger("FeeComparisonScraper")


class AssertedFeeScraper(BaseScraper):
    """Scrapes a single product whose only fee-relevant content is a
    written catch-all statement (e.g. "There are no maintenance or
    activity fees associated with this account") rather than a structured
    table or line-item disclosure -- common on marketing pages and Kasasa-
    style checking disclosures.

    Each configured assertion is matched via regex and recorded through
    assert_universal_fee() rather than plain assignment, since it's a
    written claim about the product as a whole, not a per-product line
    item independently confirmed the way a Schumer box entry would be.

    Handles either an HTML page or a PDF, per config's `source_format`.
    """

    def __init__(self, name, url, config):
        super().__init__(name, url, config)

    def _get_text(self):
        response = self.fetch_url()
        if not response:
            return None
        if self.config.get("source_format") == "pdf":
            try:
                reader = pypdf.PdfReader(io.BytesIO(response.content))
                return "\n".join(page.extract_text() for page in reader.pages)
            except Exception as e:
                msg = f"[{self.name}] Failed to parse PDF: {e}"
                logger.error(msg)
                self.warnings.append(msg)
                return None
        soup = BeautifulSoup(response.content, "html.parser")
        container = soup.select_one(self.config.get("content_selector", "main")) or soup
        return " ".join(container.get_text(separator=" ", strip=True).split())

    def scrape(self):
        text = self._get_text()
        if text is None:
            return []

        card = {
            "card_name": self.config.get("product_name", self.name),
            "category": self.config.get("category", "checking_general"),
        }

        for assertion in self.config.get("assertions", []):
            field = assertion["field"]
            pattern = assertion["pattern"]
            value = assertion["value"]
            m = re.search(pattern, text, re.IGNORECASE)
            if m:
                quote = " ".join(m.group(0).split())
                self.assert_universal_fee(card, field, value, quote=quote, locator=self.url)
            else:
                self.log_field_warning(card["card_name"], field)

        return [self.finalize_card(card)]
