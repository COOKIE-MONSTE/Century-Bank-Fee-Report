import io
import logging
import re
from datetime import date

import pypdf
from bs4 import BeautifulSoup

from .base import BaseScraper

logger = logging.getLogger("FeeComparisonScraper")

STALE_AFTER_DAYS = 365  # flag a disclosure as stale once it's over a year old


def _dollar_or_none(raw):
    try:
        return "None" if float(raw.replace(",", "")) == 0 else f"${raw}"
    except ValueError:
        return f"${raw}"


class SharedCreditCardDisclosureScraper(BaseScraper):
    """Scrapes N credit card products that share ONE disclosure document for
    most terms, but each have their own APR sourced from a separate live
    rates table -- SECU NM's pattern: one PDF disclosure covers "Visa
    Credit Cards" collectively (annual fee, cash advance, balance
    transfer, foreign transaction, late payment all stated once for the
    whole family), while a rates page lists Visa Platinum/Gold/Classic's
    individual APRs.

    The PDF-sourced fields are set via assert_universal_fee() rather than
    plain assignment: the document states them once for the card family,
    not per-card independently, so attribution.py tracks them as an
    asserted claim (with the source quote attached) instead of blending
    them into the empirical "each card independently confirmed this"
    convergence that a real per-card Schumer box would produce.
    """

    def __init__(self, name, url, config):
        super().__init__(name, url, config)

    def _fetch_apr_by_card(self):
        """Returns {card_name: apr_string} from the live rates table."""
        rates_url = self.config.get("rates_url")
        if not rates_url:
            return {}
        response = self.fetch_url(rates_url)
        if not response:
            return {}

        soup = BeautifulSoup(response.content, "html.parser")
        aprs = {}
        for table in soup.find_all("table"):
            header_cells = [c.get_text(strip=True).lower() for c in table.find("tr").find_all(["td", "th"])]
            if not any("credit card" in c for c in header_cells):
                continue
            for row in table.find_all("tr")[1:]:
                cells = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
                if len(cells) >= 3:
                    aprs[cells[0]] = cells[2]
        return aprs

    def scrape(self):
        response = self.fetch_url()  # the shared PDF disclosure
        if not response:
            return []

        try:
            reader = pypdf.PdfReader(io.BytesIO(response.content))
            text = "\n".join(page.extract_text() for page in reader.pages)
        except Exception as e:
            msg = f"[{self.name}] Failed to parse credit card disclosure PDF: {e}"
            logger.error(msg)
            self.warnings.append(msg)
            return []

        quote_source = self.config.get("product_name", "Credit Card Disclosure")

        def find(pattern, group=1):
            m = re.search(pattern, text, re.IGNORECASE)
            return m.group(group).strip() if m else None

        # Dollar-amount fields: match digits/commas plus an optional .XX
        # decimal specifically, so the capture stops before a sentence-
        # ending period instead of swallowing it (e.g. "$0.00." should
        # capture "0.00", not "0.00.").
        annual_fee = find(r"Annual Fee:\s*\$?([\d,]+(?:\.\d{2})?)")
        balance_transfer = find(r"Balance Transfer:\s*\$?([\d,]+(?:\.\d{2})?)")
        late_payment = find(r"Late Payment Fee:\s*\$?([\d,]+(?:\.\d{2})?)")
        # Free-text fields: capture up to a period NOT followed by a digit,
        # so a decimal point inside a dollar amount ("$25.00 max.") doesn't
        # get mistaken for the sentence boundary.
        cash_advance = find(r"Cash Advances?:\s*(.+?\.(?!\d))")
        foreign_tx = find(r"Foreign Transaction Fee:\s*(.+?\.(?!\d))")
        stale_date_str = find(r"current as of\s+([A-Za-z]+ \d{1,2},\s*\d{4})")

        shared_fields = {}
        if annual_fee is not None:
            shared_fields["annual_fee"] = _dollar_or_none(annual_fee)
        if balance_transfer is not None:
            shared_fields["balance_transfer_fee"] = _dollar_or_none(balance_transfer)
        if cash_advance:
            shared_fields["cash_advance_fee"] = self.clean_value(cash_advance)
        if foreign_tx:
            shared_fields["foreign_transaction_fee"] = self.clean_value(foreign_tx)
        if late_payment is not None:
            shared_fields["late_payment_fee"] = _dollar_or_none(late_payment)

        for field in ("annual_fee", "balance_transfer_fee", "cash_advance_fee", "foreign_transaction_fee", "late_payment_fee"):
            if field not in shared_fields:
                self.log_field_warning(quote_source, field)

        if stale_date_str:
            try:
                parsed = date.fromisoformat(
                    __import__("datetime").datetime.strptime(stale_date_str, "%B %d, %Y").strftime("%Y-%m-%d")
                )
                age_days = (date.today() - parsed).days
                if age_days > STALE_AFTER_DAYS:
                    msg = (
                        f"[{self.name}] Credit card disclosure states it is \"current as of {stale_date_str}\" "
                        f"-- that's {age_days // 365} year(s) old. Transaction fee terms (annual fee, cash "
                        f"advance, balance transfer, foreign transaction, late payment) have not been "
                        f"independently reconfirmed since then; verify they're still accurate."
                    )
                    logger.warning(msg)
                    self.warnings.append(msg)
            except ValueError:
                pass

        apr_by_card = self._fetch_apr_by_card()
        products = self.config.get("products", {})

        cards = []
        for card_name, product_cfg in products.items():
            card = {
                "card_name": card_name,
                "category": product_cfg.get("category", "credit_card_standard"),
            }
            apr = apr_by_card.get(card_name)
            if apr:
                card["purchase_apr"] = apr
            else:
                self.log_field_warning(card_name, "purchase_apr")

            for field, value in shared_fields.items():
                self.assert_universal_fee(
                    card, field, value,
                    quote=f"See {quote_source} (applies to all State ECU Visa credit cards, not per-card)",
                    locator=self.url,
                )

            cards.append(self.finalize_card(card))

        return cards
