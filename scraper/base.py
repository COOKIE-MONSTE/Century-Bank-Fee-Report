import logging
import re
import requests

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
            "returned_payment_fee": "Not disclosed",
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

    def log_field_warning(self, card_name, field_name):
        msg = f"[{self.name} - {card_name}] Field '{field_name}' could not be parsed."
        logger.warning(msg)
        self.warnings.append(msg)
