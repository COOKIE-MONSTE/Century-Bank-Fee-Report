import io
import logging
import re

from bs4 import BeautifulSoup
import pypdf

from .base import BaseScraper, default_field_description

logger = logging.getLogger("FeeComparisonScraper")


class RegexValueScraper(BaseScraper):
    """Scrapes a single product's fee values straight out of a page's prose
    text via configured regexes -- for pages that state one fact in a
    paragraph rather than a table (e.g. Sunflower Bank's overdraft-privilege
    explainer, which states the fee inline: "...a fee (currently $29.00 for
    consumer accounts and $36.00 for business accounts)...").

    Works against either an HTML page or a PDF at `url` -- detected from
    the response's Content-Type header (falling back to the URL's own
    extension, since some CMS asset endpoints don't set it reliably) --
    since a Truth-in-Savings PDF states its per-product fees in the same
    kind of inline prose sentence an HTML explainer page does (e.g. First
    National 1870's HSA disclosure: "A fee of$25.00 will be charged to
    transfer your HSA..."). PDF text is extracted via pypdf and flattened
    to one whitespace-normalized string per page before regex matching --
    same clean_value() normalization either way.

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

    `field_fixed_values` is for detector-only patterns: when a field's
    pattern is really just checking whether a phrase is present (e.g. "No
    monthly fee") rather than extracting a number, listing that field here
    replaces the captured text with a fixed canonical value (typically
    "None") once the pattern matches -- so the field reads the same way
    every other institution's confirmed-no-fee values do, and so
    attribution.py's classify_mechanism() recognizes it as a real no-fee
    value instead of unclassifiable prose.

    `asserted_field_patterns` behaves like `field_patterns` but records the
    result via BaseScraper.assert_universal_fee() instead of a plain
    assignment -- for a written catch-all statement (e.g. "No fees for
    cashier's checks and money orders") that overrides what a general fee
    schedule would otherwise say for this one product, rather than an
    ordinary per-product line item.
    """

    def __init__(self, name, url, config):
        super().__init__(name, url, config)

    def scrape(self):
        response = self.fetch_url()
        if not response:
            return []

        content_type = response.headers.get("Content-Type", "")
        is_pdf = "pdf" in content_type.lower() or self.url.lower().split("?")[0].endswith(".pdf")
        if is_pdf:
            try:
                reader = pypdf.PdfReader(io.BytesIO(response.content))
                text = " ".join(" ".join((page.extract_text() or "").split()) for page in reader.pages)
            except Exception as e:
                msg = f"[{self.name}] Failed to parse PDF: {e}"
                logger.error(msg)
                self.warnings.append(msg)
                return []
        else:
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
        fixed_values = self.config.get("field_fixed_values", {})

        for field, pattern in self.config.get("field_patterns", {}).items():
            m = re.search(pattern, text, re.IGNORECASE)
            if m:
                value = fixed_values.get(field, m.group(1))
                card[field] = self.clean_value(value)
                field_confidence[field] = "high"
                source_urls[field] = self.url
            elif self.try_llm_recovery(card, field, text, default_field_description(field)):
                field_confidence[field] = "llm_assisted"
                source_urls[field] = self.url

        for field, pattern in self.config.get("asserted_field_patterns", {}).items():
            m = re.search(pattern, text, re.IGNORECASE)
            if m:
                value = fixed_values.get(field, m.group(1))
                quote = " ".join(m.group(0).split())
                self.assert_universal_fee(card, field, self.clean_value(value), quote=quote, locator=self.url)
                source_urls[field] = self.url
            else:
                self.log_field_warning(card["card_name"], field)

        card["_field_confidence"] = field_confidence
        card["_source_urls"] = source_urls
        return [self.finalize_card(card)]
