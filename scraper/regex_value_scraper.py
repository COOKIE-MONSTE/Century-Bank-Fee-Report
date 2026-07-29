import logging
import re

from bs4 import BeautifulSoup

from .base import BaseScraper

logger = logging.getLogger("FeeComparisonScraper")


class RegexValueScraper(BaseScraper):
    """Scrapes a single product's fee values straight out of a page's prose
    text via configured regexes -- for pages that state one fact in a
    paragraph rather than a table (e.g. Sunflower Bank's overdraft-privilege
    explainer, which states the fee inline: "...a fee (currently $29.00 for
    consumer accounts and $36.00 for business accounts)...").

    Distinct from AssertedFeeScraper: that class records a fixed config-
    supplied value when a written CLAIM's pattern matches (e.g. "no monthly
    fee" -> "None"). This class extracts the actual VALUE from the page via
    each pattern's single capture group, since the source states a real
    number that can change -- ordinary empirical extraction (high
    confidence when matched), not an assertion about the product overall.

    Each `field_patterns` regex must have exactly one capture group, which
    becomes the field's value verbatim (after clean_value()). Patterns
    should be anchored precisely enough to reject nearby unrelated numbers
    on the same page/sentence -- see fn1870_overdraft's config comment for
    a concrete case where two dollar amounts share one sentence.
    """

    def __init__(self, name, url, config):
        super().__init__(name, url, config)

    def scrape(self):
        response = self.fetch_url()
        if not response:
            return []

        soup = BeautifulSoup(response.content, "html.parser")
        selector = self.config.get("content_selector", "main")
        container = soup.select_one(selector) or soup
        text = " ".join(container.get_text(separator=" ", strip=True).split())

        card = {
            "card_name": self.config.get("product_name", self.name),
            "category": self.config.get("category", "uncategorized"),
        }
        field_confidence = {}
        source_urls = {}

        for field, pattern in self.config.get("field_patterns", {}).items():
            m = re.search(pattern, text, re.IGNORECASE)
            if m:
                card[field] = self.clean_value(m.group(1))
                field_confidence[field] = "high"
                source_urls[field] = self.url
            else:
                self.log_field_warning(card["card_name"], field)

        card["_field_confidence"] = field_confidence
        card["_source_urls"] = source_urls
        return [self.finalize_card(card)]
