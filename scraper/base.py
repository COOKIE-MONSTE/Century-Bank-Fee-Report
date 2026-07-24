import logging
import re
import requests

from . import categories
from . import fee_taxonomy

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger("FeeComparisonScraper")

class BaseScraper:
    def __init__(self, name, url, config):
        self.name = name
        self.url = url
        self.config = config
        self.warnings = []

    def fetch_url(self):
        """Fetches URL contents with headers to avoid bot detection."""
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5"
        }
        try:
            logger.info(f"Fetching URL for {self.name}: {self.url}")
            response = requests.get(self.url, headers=headers, timeout=20)
            response.raise_for_status()
            return response
        except Exception as e:
            msg = f"Failed to reach {self.name} URL ({self.url}): {str(e)}"
            logger.error(msg)
            self.warnings.append(msg)
            return None

    def clean_value(self, text):
        """Standardizes whitespaces and cleans extracted text."""
        if not text:
            return "Not disclosed"
        cleaned = " ".join(text.split())
        # Strip trailing/leading punctuation/symbols commonly left over
        cleaned = cleaned.strip(":,; ")
        return cleaned if cleaned else "Not disclosed"

    def get_default_fields(self):
        """Returns standard structure for card fields."""
        return {
            "annual_fee": "Not disclosed",
            "purchase_apr": "Not disclosed",
            "balance_transfer_apr": "Not disclosed",
            "balance_transfer_fee": "Not disclosed",
            "cash_advance_apr": "Not disclosed",
            "cash_advance_fee": "Not disclosed",
            "foreign_transaction_fee": "Not disclosed",
            "late_payment_fee": "Not disclosed",
            "returned_item_fee": "Not disclosed",
            "intro_offers": "Not disclosed",
            "rewards_structure": "Not disclosed"
        }

    def extract_last_percentage_range(self, text):
        """Finds all 'NN.NN% - NN.NN%' style ranges and returns the last one.

        Disclosure paragraphs typically mention a promotional rate first and
        the standard/ongoing rate last, so the last match is the one worth
        keeping for a short comparison value.
        """
        if not text:
            return None
        matches = re.findall(r"(\d{1,2}(?:\.\d{1,2})?)\s*%\s*(?:-|to)\s*(\d{1,2}(?:\.\d{1,2})?)\s*%", text, re.IGNORECASE)
        if not matches:
            return None
        low, high = matches[-1]
        return f"{low}% - {high}%"

    def extract_first_percentage(self, text):
        """Returns the first standalone 'N%' occurrence in text, if any."""
        if not text:
            return None
        match = re.search(r"(\d{1,2}(?:\.\d{1,2})?)\s*%", text)
        if not match:
            return None
        return f"{match.group(1)}%"

    def get_keywords(self):
        """Returns this institution's field_keywords, with each field's own
        phrase list extended by the shared cross-institution synonym list
        (scraper/fee_taxonomy.py).

        A phrase learned at one institution (via the feedback loop, see
        feedback.py) is recognized at every institution from here on for
        the *same* field, instead of needing to be re-added to each bank's
        config.yaml by hand. This only adds phrases to fields the
        institution already declares in config.yaml -- it deliberately
        never introduces a field the scraper wasn't already looking for,
        since that could both KeyError against get_default_fields() and
        start matching an unrelated fee type this page was never scoped to
        report on.
        """
        shared = fee_taxonomy.get_synonyms()
        merged = {}
        for field, kw_list in self.config.get("field_keywords", {}).items():
            merged[field] = list(kw_list)
            for kw in shared.get(field, []):
                if kw not in merged[field]:
                    merged[field].append(kw)
        return merged

    def log_field_warning(self, card_name, field_name):
        msg = f"[{self.name} - {card_name}] Field '{field_name}' could not be parsed."
        logger.warning(msg)
        self.warnings.append(msg)

    def finalize_card(self, card):
        """Ensures every card carries a category before it leaves the scraper.

        Prefers a category the scraper set explicitly (it knows the product
        best), then an institution-level override in config.yaml, and only
        falls back to guessing from the product name -- logging a warning,
        since a guessed category means this card wasn't deliberately
        classified and its comparisons should be treated with more caution.
        """
        if not card.get("category"):
            guessed = self.config.get("category") or categories.guess_category(card.get("card_name"))
            card["category"] = guessed
            self.warnings.append(
                f"[{self.name} - {card.get('card_name', 'Unknown product')}] "
                f"Category not explicitly set; guessed '{guessed}'."
            )
        elif card["category"] not in categories.CATEGORIES:
            msg = f"[{self.name} - {card.get('card_name')}] Unknown category '{card['category']}'."
            logger.warning(msg)
            self.warnings.append(msg)
            card["category"] = "uncategorized"
        return card
