import logging
import requests
from bs4 import BeautifulSoup
from .base import BaseScraper

logger = logging.getLogger("FeeComparisonScraper")


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
        keywords = self.config.get("field_keywords", {})
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
                grouped.setdefault(field, [])
                if piece not in grouped[field]:
                    grouped[field].append(piece)

            card = {"card_name": product_name}
            for field, pieces in grouped.items():
                card[field] = self.clean_value("; ".join(pieces))
            cards.append(card)

        return cards
