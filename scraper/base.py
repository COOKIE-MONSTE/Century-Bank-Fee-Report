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

# Values that mean "no real figure here" for the purposes of deciding
# whether a missing field looks like a genuine site-structure regression
# (see try_llm_recovery below) -- kept separate from attribution.py's
# EMPTY_VALUES (which also treats an unset field the same way for display
# purposes) since this module can't import attribution.py without risking
# a cycle (attribution/report/credit_card_matrix all sit above scraper/*).
_NON_REAL_VALUES = {None, "", "Not disclosed", "Not stated", "Not publicly disclosed"}


def default_field_description(field):
    """Generic, human-readable description of a canonical field key, for
    scrapers that don't have a hand-written description per field (see
    scraper/tcm_issuer_scraper.py's _FIELD_DESCRIPTIONS for a hand-written
    example where the extra precision was worth the upkeep). Good enough
    for Gemini to locate a labeled fee in prose or a table -- it doesn't
    need report.py's display-quality labels, just an unambiguous target.
    """
    return f"The {field.replace('_', ' ')} -- a dollar amount, percentage, or short phrase stating it."


class BaseScraper:
    def __init__(self, name, url, config):
        self.name = name
        self.url = url
        self.config = config
        self.warnings = []
        # {(category, field): value} from yesterday's committed
        # data/history.json, scoped to THIS institution only -- set by
        # run.py via set_previous_values() before scrape() is called.
        # Empty by default so every scraper (including ones under test in
        # isolation, with no history available) degrades safely to "no
        # eligible previous value" rather than erroring.
        self._previous_values = {}

    def set_previous_values(self, previous_values):
        """Supplies yesterday's {(category, field): value} lookup for this
        institution -- see try_llm_recovery() for what it's used for.
        """
        self._previous_values = previous_values or {}

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

    def apply_field_aliases(self, card):
        """Copies an already-extracted field's value into another
        canonical field name, per config's `field_aliases:
        {new_field: source_field}`.

        For a source that publishes only ONE fee figure covering what
        this report tracks as two distinct fields -- e.g. a "Wire
        Transfer: Domestic $X" line with no separate incoming rate stated
        anywhere, confirmed by checking the page for "incoming"/
        "received" wording and finding none. This is opt-in per
        institution via config, never a default assumption: most
        institutions in this report DO distinguish incoming from
        outgoing, and this only applies where a human has confirmed the
        specific source genuinely doesn't.
        """
        confidence = card.get("_field_confidence", {})
        for new_field, source_field in self.config.get("field_aliases", {}).items():
            if source_field in card and new_field not in card:
                card[new_field] = card[source_field]
                if source_field in confidence:
                    confidence[new_field] = confidence[source_field]
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

    def try_llm_recovery(self, card, field, page_text, field_description):
        """Attempts a Gemini-assisted re-extraction for `field`, but ONLY
        when this exact (category, field) had a REAL value in yesterday's
        committed snapshot -- i.e. this looks like a genuine extraction
        regression (the source's structure likely changed), not a fact
        that has simply never been published. A field that has NEVER
        extracted (e.g. Century's annual fee, permanently
        NOT_PUBLICLY_DISCLOSED by TCM's own design) never qualifies, so
        this never spends API quota chasing a fact no page will ever
        state -- and never risks handing that field's genuinely-confirmed
        "not disclosed" status to a probabilistic guess.

        On success: sets card[field], tags its confidence "llm_assisted",
        logs a warning making clear this is a stopgap (the underlying
        pattern needs a real fix), and returns True. On any failure
        (ineligible, LLM unavailable, LLM found nothing): calls
        log_field_warning() exactly as the pre-existing "not found" path
        did, and returns False -- callers don't need a separate branch
        for "didn't even try" vs. "tried and failed".
        """
        card_name = card.get("card_name", "Unknown")
        category = card.get("category") or self.config.get("category")
        previous_value = self._previous_values.get((category, field))

        if previous_value in _NON_REAL_VALUES:
            self.log_field_warning(card_name, field)
            return False

        msg = (
            f"[{self.name} - {card_name}] Field '{field}' extracted a real value "
            f"({previous_value!r}) in the previous run but not this one -- likely a "
            f"site change, not a newly-undisclosed fee. Attempting Gemini recovery."
        )
        logger.warning(msg)
        self.warnings.append(msg)

        context = (
            f"This institution, {self.name}, is located in New Mexico or a nearby "
            f"region. Only use the page text provided below -- do not use outside "
            f"knowledge about any other bank with a similar name."
        )
        value = self.llm_extract_field(page_text, field_description, context=context)
        if not value:
            self.log_field_warning(card_name, field)
            return False

        card[field] = self.clean_value(value)
        card.setdefault("_field_confidence", {})[field] = "llm_assisted"
        recovery_msg = (
            f"[{self.name} - {card_name}] Recovered '{field}' via Gemini after an apparent "
            f"extraction regression: {value!r}. This is a stopgap, not a fix -- the "
            f"underlying pattern should be updated to match the source's new structure."
        )
        logger.warning(recovery_msg)
        self.warnings.append(recovery_msg)
        return True

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
