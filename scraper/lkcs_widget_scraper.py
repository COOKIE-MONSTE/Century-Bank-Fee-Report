import logging
import re

import requests
from bs4 import BeautifulSoup
from .base import BaseScraper
from . import categories

logger = logging.getLogger("FeeComparisonScraper")

# Some rows state the fee's period in the TYPE label rather than the
# amount itself -- e.g. "Dormant Account (per month)" / "$5.00" -- which
# left the extracted value reading as a flat one-time charge with no way
# for attribution.classify_mechanism() to tell it's actually recurring.
_PERIOD_QUALIFIER = re.compile(r"\(per\s+(month|hour|year|day|item|transaction)\)", re.IGNORECASE)

# Some rows state the fee as a *waivable* balance-threshold charge in the
# TYPE label rather than the amount itself -- e.g. "Minimum Average
# Balance/Share Draft < $2,500" / "$7.50" -- which left the value reading
# as an unconditional flat fee. Confirmed 2026-07-30 this affects Dividend
# Checking, Essential Checking, and Money Market, all via this same label
# pattern -- not a one-off, so fixed generically here rather than only for
# whichever one was reported.
_BALANCE_THRESHOLD_QUALIFIER = re.compile(r"Minimum Average Balance(?:/Share Draft)?\s*<\s*\$([\d,]+)", re.IGNORECASE)


class LKCSFeeScraper(BaseScraper):
    """Scrapes fee tables served by the lk-cs.com rates widget API used by
    Nusenda's Rates & Fees page. The page itself renders no fee data in its
    static HTML -- it loads each section via a small AJAX call. Each
    configured widget ID returns one HTML fragment: an <h2> product title
    plus a <table class='tbl-rates'> of Type/Fee (or Type/Items/Fee) rows.
    """

    def __init__(self, name, url, config):
        super().__init__(name, url, config)

    def _fetch_widget(self, s_value):
        client_id = self.config["client_id"]
        r_value = self.config["endpoint_r"]
        endpoint = f"https://clients.lk-cs.com/id/{client_id}/custom/rates/"
        params = {"r": r_value, "s": s_value, "id": client_id}
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": self.url or "https://www.nusenda.org/news-resources/rates-fees",
        }
        try:
            logger.info(f"Fetching {self.name} widget s={s_value}")
            response = requests.get(endpoint, params=params, headers=headers, timeout=20)
            response.raise_for_status()
            return response.text
        except Exception as e:
            msg = f"Failed to fetch {self.name} widget s={s_value}: {str(e)}"
            logger.error(msg)
            self.warnings.append(msg)
            return None

    def _match_keyword(self, type_text):
        keywords = self.get_keywords()
        type_lower = type_text.lower()
        for field, kw_list in keywords.items():
            for kw in kw_list:
                if kw in type_lower:
                    return field
        return None

    def _parse_rows(self, table):
        """Returns [(type_text, cells)], forward-filling blank Type cells so
        tiered fee tables (e.g. NSF fee-per-item tiers) stay grouped under
        the row that actually names the fee."""
        rows = []
        last_type = ""
        for tr in table.find_all("tr", class_="tbl-rates-row"):
            cells = [td.get_text(separator=" ", strip=True) for td in tr.find_all("td")]
            if not cells or all(c == "" for c in cells):
                continue
            type_text = cells[0].strip() or last_type
            last_type = type_text
            rows.append((type_text, cells))
        return rows

    def scrape(self):
        cards = []
        for s_value in self.config.get("widgets", []):
            html = self._fetch_widget(s_value)
            if not html:
                continue

            soup = BeautifulSoup(html, "html.parser")
            h2 = soup.find("h2")
            product_name = self.clean_value(h2.get_text(strip=True)) if h2 else f"Widget {s_value}"

            table = soup.find("table", class_="tbl-rates")
            if not table:
                self.warnings.append(f"No fee table found for {self.name} widget s={s_value}")
                continue

            grouped = {}
            for type_text, cells in self._parse_rows(table):
                field = self._match_keyword(type_text)
                if not field:
                    continue
                if len(cells) >= 3:
                    items, fee = cells[1].strip(), cells[2].strip()
                    piece = f"{fee} for {items}" if items else fee
                else:
                    piece = cells[-1].strip()
                if not piece:
                    continue
                period_m = _PERIOD_QUALIFIER.search(type_text)
                if period_m and period_m.group(1).lower() not in piece.lower():
                    piece = f"{piece} per {period_m.group(1).lower()}"
                balance_m = _BALANCE_THRESHOLD_QUALIFIER.search(type_text)
                if balance_m and "waived" not in piece.lower():
                    piece = f"{piece}/month, waived with a ${balance_m.group(1)} minimum average balance"
                grouped.setdefault(field, [])
                if piece not in grouped[field]:
                    grouped[field].append(piece)

            widget_categories = self.config.get("widget_categories", {})
            category = widget_categories.get(s_value) or categories.guess_category(product_name)

            card = {"card_name": product_name, "category": category}
            for field, pieces in grouped.items():
                card[field] = self.clean_value("; ".join(pieces))
            self.apply_field_aliases(card)
            cards.append(self.finalize_card(card))

        return cards
