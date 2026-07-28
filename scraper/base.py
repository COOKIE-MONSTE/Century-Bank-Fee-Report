import logging
import re
import requests

from . import categories
from . import fee_taxonomy
from . import llm_fallback

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

    def fetch_url(self, url=None):
        """Fetches URL contents with headers to avoid bot detection.

        Defaults to self.url; pass `url` explicitly for a scraper that
        needs to pull from more than one source (e.g. a shared PDF
        disclosure plus a separate live rates page).
        """
        target = url or self.url
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5"
        }
        try:
            logger.info(f"Fetching URL for {self.name}: {target}")
            response = requests.get(target, headers=headers, timeout=20)
            response.raise_for_status()
            return response
        except Exception as e:
            msg = f"Failed to reach {self.name} URL ({target}): {str(e)}"
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

    def assert_universal_fee(self, card, field, value, quote, locator):
        """Marks a field's value as sourced from an explicit written
        statement (e.g. "There are no maintenance fees on this account")
        rather than a per-product line item.

        This is deliberately kept separate from ordinary field assignment:
        attribution.py tracks asserted values in their own bucket, never
        blending them into the empirical "N products independently agree"
        convergence that verified-across-all requires. A written catch-all
        statement is real evidence, but it is not the same strength of
        evidence as several independent scrapes agreeing, so it's surfaced
        as "asserted universal" -- visibly distinct, with the source quote
        attached -- rather than silently promoted to the same confidence.
        """
        card[field] = value
        card.setdefault("_asserted_universal", {})[field] = {"quote": quote, "locator": locator}
        return card

    def llm_extract_field(self, page_text, field_description, context=""):
        """Fallback extraction via Gemini, for use only after a regex/CSS
        selector attempt has already come up empty on a page that fetched
        successfully -- never called as the primary extraction path (see
        scraper/llm_fallback.py for the full reasoning).

        Returns the extracted string, or None if the LLM couldn't find it,
        isn't configured (no GEMINI_API_KEY), or the call failed -- any of
        which should be treated exactly like today's "field not found"
        case, not as an error. Logs why via self.warnings either way, so a
        human reviewing the report can tell "field genuinely absent" apart
        from "LLM fallback wasn't even available this run".
        """
        value, reason = llm_fallback.extract_field_via_llm(page_text, field_description, context)
        if value is None:
            msg = f"[{self.name}] LLM fallback for '{field_description}': {reason}"
            logger.info(msg)
            self.warnings.append(msg)
        return value

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
