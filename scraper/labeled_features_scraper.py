import logging
import re
from bs4 import BeautifulSoup
from .base import BaseScraper

logger = logging.getLogger("FeeComparisonScraper")


class LabeledFeaturesScraper(BaseScraper):
    """Scrapes a single-product marketing page whose fee facts appear as
    short labeled phrases (a bullet-point feature list plus a prose
    paragraph) rather than a structured table -- e.g. Century Bank's
    checking product pages ("$25 Minimum opening balance", "No monthly
    service fee with a minimum daily balance of $500").

    Extraction is scoped to a single container (config's `content_selector`,
    default "main") to avoid picking up unrelated dollar amounts from nav,
    footer, or cross-sell widgets elsewhere on the page.

    The monthly-fee pattern specifically handles the conditional/waivable
    phrasing ("No monthly service fee *with* a minimum daily balance of $X")
    separately from a genuinely unconditional "No monthly service fee" --
    the two read almost identically at a glance but mean very different
    things, so the conditional pattern is checked first and is the only one
    that consumes the phrase; naively matching "no monthly service fee" as
    a substring would misread every waived-fee product as free.
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
            "category": self.config.get("category", "checking_general"),
        }
        field_confidence = {}
        unmatched_notes = []

        fee_m = re.search(r"monthly service fee of \$([\d,]+(?:\.\d{2})?)\s*will be applied", text, re.IGNORECASE)
        bal_m = re.search(r"minimum daily balance of \$([\d,]+(?:\.\d{2})?)", text, re.IGNORECASE)
        unconditional_no_fee = re.search(r"no monthly service fee(?!\s+with)", text, re.IGNORECASE)

        if fee_m:
            value = f"${fee_m.group(1)}/month"
            if bal_m:
                value += f", waived with a ${bal_m.group(1)} minimum daily balance"
            card["monthly_maintenance_fee"] = value
            field_confidence["monthly_maintenance_fee"] = "high"
        elif unconditional_no_fee:
            card["monthly_maintenance_fee"] = "None"
            field_confidence["monthly_maintenance_fee"] = "high"
        else:
            self.log_field_warning(card["card_name"], "monthly_maintenance_fee")

        # Opening deposit and paper-statement fee aren't in the existing
        # canonical field set -- reported as informational facts rather
        # than forced into an existing field (see config.yaml comment for
        # why: schema conformance was an explicit constraint on this task).
        open_m = re.search(r"\$([\d,]+(?:\.\d{2})?)\s*Minimum opening balance", text, re.IGNORECASE)
        if open_m:
            unmatched_notes.append(f"Minimum opening balance: ${open_m.group(1)}")

        stmt_m = re.search(r"\$([\d,]+(?:\.\d{2})?)\s*per statement cycle", text, re.IGNORECASE)
        if stmt_m:
            unmatched_notes.append(f"Paper statement fee: ${stmt_m.group(1)} per cycle (e-statements free)")

        if unmatched_notes:
            msg = (
                f"[{self.name} - {card['card_name']}] Fee-adjacent facts found with no existing "
                f"canonical field: {'; '.join(unmatched_notes)}"
            )
            logger.info(msg)
            self.warnings.append(msg)

        card["_field_confidence"] = field_confidence
        return [self.finalize_card(card)]
