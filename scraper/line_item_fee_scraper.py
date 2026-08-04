import io
import logging
import re
from datetime import date, datetime

import pypdf

from .base import BaseScraper, default_field_description
from .tcm_issuer_scraper import NOT_PUBLICLY_DISCLOSED

logger = logging.getLogger("FeeComparisonScraper")

STALE_AFTER_DAYS = 365  # matches shared_credit_card_scraper.py's threshold

# Splits a PDF line into (label, value): the value is whatever starts at
# the first dollar amount, percentage, or one of the two non-numeric value
# phrases this document uses ("Varies", "Fees vary..."). Matching on where
# the VALUE starts -- not a fixed keyword list -- is what correctly tells
# "Stop Payments" apart from "Stop Payments originated through EBDIRECT":
# both are real, distinct line items in this document, and only the text
# before the value differs.
_LINE_PATTERN = re.compile(
    r"^(.*?)\s+(\$[\d,]+(?:\.\d{2})?.*|[\d.]+%.*|Fees vary.*|Varies.*)$", re.IGNORECASE
)


class LineItemFeeScraper(BaseScraper):
    """Scrapes a flat "Label Value" line-item fee schedule PDF with no
    table markup at all -- e.g. Enterprise Bank & Trust's Schedule of Fees,
    where every fee is its own text line like "Overdrafts Paid $30.00".

    Each line is split into (label, value) once (see _LINE_PATTERN above),
    then fields are matched against that label by EXACT text (case-
    insensitive), not substring: this document's own labels overlap in
    exactly the way that makes substring matching wrong -- "Domestic Wire
    Transfers (Outgoing)" is a literal prefix of "Domestic Wire Transfers
    (Outgoing) Originated from EBDIRECT", a different (business-channel)
    fee on the very next line. Four separate $30.00 NSF/overdraft lines
    exist too ("Overdrafts Paid", "Overdrafts Returned", "NSF's Paid",
    "NSF's Returned") that happen to agree today -- exact-label matching
    keeps the mapping correct even if they diverge later.

    `combined_fields` builds one canonical field out of two+ separately
    matched lines with a join template -- for schema fields that don't
    have a matching split in this document (e.g. one incoming-wire field
    covering both the domestic and foreign rates).

    `not_publicly_disclosed_fields` marks a field's value as confirmed to
    vary rather than centrally set (e.g. "Safe Deposit Box Drilling & Key
    Replacement: Fees vary by location") -- recorded as
    NOT_PUBLICLY_DISCLOSED instead of the literal phrase, matching
    tcm_issuer_scraper.py's convention for the same sentinel.

    Every field NOT explicitly listed in `field_keywords`/`combined_fields`
    is simply never extracted -- this is how business-only lines (Money
    Service Business fee, Fed Cash Handling, bulk coin/currency,
    garnishments, EBDIRECT-originated variants) stay out of consumer fee
    comparisons: there's no allowlist entry for them, so they're never
    looked up in the first place.

    Also emits a staleness warning based on the document's own "Printed on
    M/D/YYYY" footer -- this schedule is the current *published* one (the
    live site serves nothing else), but it's dated 2019 and that needs to
    be visible, not silently presented as current.
    """

    def __init__(self, name, url, config):
        super().__init__(name, url, config)

    def _parse_lines(self, text):
        """Returns {label_lower: value_text} for every "Label Value" line."""
        values = {}
        for line in text.split("\n"):
            line = " ".join(line.replace("\xa0", " ").split())
            if not line:
                continue
            m = _LINE_PATTERN.match(line)
            if m:
                label, value = m.group(1).strip(), m.group(2).strip()
                if label:
                    values[label.lower()] = value
        return values

    def _check_staleness(self, text):
        m = re.search(r"Printed on\s+(\d{1,2}/\d{1,2}/\d{4})", text, re.IGNORECASE)
        if not m:
            return
        try:
            parsed = datetime.strptime(m.group(1), "%m/%d/%Y").date()
        except ValueError:
            return
        age_days = (date.today() - parsed).days
        if age_days > STALE_AFTER_DAYS:
            msg = (
                f"[{self.name}] Schedule of Fees is dated {m.group(1)} (printed on the document "
                f"itself) -- that's {age_days // 365} year(s) old. It's still the current schedule "
                f"the bank publishes (nothing newer is served), but these fee amounts have not been "
                f"independently reconfirmed since then; verify they're still accurate."
            )
            logger.warning(msg)
            self.warnings.append(msg)

    def scrape(self):
        response = self.fetch_url()
        if not response:
            return []

        try:
            reader = pypdf.PdfReader(io.BytesIO(response.content))
            text = "\n".join(page.extract_text() for page in reader.pages)
        except Exception as e:
            msg = f"[{self.name}] Failed to parse PDF: {e}"
            logger.error(msg)
            self.warnings.append(msg)
            return []

        self._check_staleness(text)
        values_by_label = self._parse_lines(text)

        card = {
            "card_name": self.config.get("product_name", self.name),
            "category": self.config.get("category", "general_account_fees"),
        }
        field_confidence = {}
        not_disclosed_fields = set(self.config.get("not_publicly_disclosed_fields", []))

        for field, labels in self.config.get("field_keywords", {}).items():
            value = None
            for label in labels:
                value = values_by_label.get(label.lower())
                if value:
                    break
            if not value:
                # A field genuinely marked not_publicly_disclosed is
                # expected to never match a label here -- that's not a
                # regression, it's this document confirming the fee
                # varies/isn't centrally set (see e.g. Enterprise's safe
                # deposit drilling fee). try_llm_recovery's own drift
                # check (never-had-a-real-value = ineligible) already
                # protects those, so no separate exclusion is needed here.
                if self.try_llm_recovery(card, field, text, default_field_description(field)):
                    field_confidence[field] = "llm_assisted"
                continue
            if field in not_disclosed_fields:
                card[field] = NOT_PUBLICLY_DISCLOSED
            else:
                card[field] = self.clean_value(value)
            field_confidence[field] = "high"

        for field, spec in self.config.get("combined_fields", {}).items():
            parts = []
            complete = True
            for part in spec["parts"]:
                v = values_by_label.get(part["label"].lower())
                if not v:
                    complete = False
                    break
                parts.append(f"{part.get('prefix', '')}{self.clean_value(v)}")
            if complete:
                card[field] = spec.get("join", " / ").join(parts)
                field_confidence[field] = "high"
            else:
                self.log_field_warning(card["card_name"], field)

        card["_field_confidence"] = field_confidence
        return [self.finalize_card(card)]
