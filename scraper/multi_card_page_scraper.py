import logging
import re
from bs4 import BeautifulSoup
from .base import BaseScraper

logger = logging.getLogger("FeeComparisonScraper")


class MultiCardPageScraper(BaseScraper):
    """Scrapes several products off of one page, where each product is its
    own card-style container (e.g. Century Bank's savings/CDs/retirement
    page: 7 products as Bootstrap `.card` divs on a single URL, alongside
    unrelated cards like "Private Banking" and "Access your account" that
    aren't products at all).

    Card boundaries come from a CSS selector (`card_selector`, default
    ".card"), scoped further by an explicit `products` allowlist keyed by
    the card's heading text -- deliberately not "every .card on the page",
    since this page mixes real product cards with marketing/navigation
    cards that happen to use the same component.

    `fee_pattern`/`balance_pattern` are configurable (default to Century's
    own "$X Monthly Service Fee" / "$X Min. Daily Balance..." wording,
    value-before-label) since not every institution's card page uses that
    exact phrase or order -- SECU NM's compare-accounts page, for example,
    states "Monthly Service Fee $X" (label-before-value).
    """

    def __init__(self, name, url, config):
        super().__init__(name, url, config)

    def scrape(self):
        response = self.fetch_url()
        if not response:
            return []

        soup = BeautifulSoup(response.content, "html.parser")
        card_selector = self.config.get("card_selector", ".card")
        heading_selector = self.config.get("heading_selector", "h2")
        products_cfg = self.config.get("products", {})

        fee_re = re.compile(
            self.config.get("fee_pattern", r"\$([\d,]+(?:\.\d{2})?)\s*Monthly Service Fee"),
            re.IGNORECASE,
        )
        balance_re = re.compile(
            self.config.get(
                "balance_pattern",
                r"\$([\d,]+(?:\.\d{2})?)\s*(?:Min\.\s*Daily Balance to Avoid Fee"
                r"|minimum daily balance to avoid (?:monthly )?service fee)",
            ),
            re.IGNORECASE,
        )

        found_headings = set()
        cards_out = []

        for card_el in soup.select(card_selector):
            heading_el = card_el.select_one(heading_selector)
            if not heading_el:
                continue
            heading_text = heading_el.get_text(strip=True)
            product_cfg = products_cfg.get(heading_text)
            if not product_cfg:
                continue
            found_headings.add(heading_text)

            text = " ".join(card_el.get_text(separator=" ", strip=True).split())
            card = {
                "card_name": product_cfg.get("product_name", heading_text),
                "category": product_cfg.get("category", "uncategorized"),
            }
            field_confidence = {}

            fee_m = fee_re.search(text)
            if fee_m:
                value = f"${fee_m.group(1)}"
                bal_m = balance_re.search(text)
                if bal_m and bal_m.group(1) != "0":
                    value += f"/month, waived with a ${bal_m.group(1)} minimum daily balance"
                elif fee_m.group(1) == "0":
                    value = "None"
                else:
                    value += "/month"
                card["monthly_maintenance_fee"] = value
                field_confidence["monthly_maintenance_fee"] = "high"
            else:
                self.log_field_warning(card["card_name"], "monthly_maintenance_fee")

            card["_field_confidence"] = field_confidence
            cards_out.append(self.finalize_card(card))

        missing = set(products_cfg.keys()) - found_headings
        if missing:
            msg = f"[{self.name}] Expected product card(s) not found on page: {sorted(missing)}"
            logger.warning(msg)
            self.warnings.append(msg)

        return cards_out
