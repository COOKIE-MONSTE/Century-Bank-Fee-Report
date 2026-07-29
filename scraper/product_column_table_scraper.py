import copy
import logging
import re
from datetime import date, datetime

from bs4 import BeautifulSoup

from .base import BaseScraper
from .tcm_issuer_scraper import NOT_PUBLICLY_DISCLOSED

logger = logging.getLogger("FeeComparisonScraper")


class ProductColumnTableScraper(BaseScraper):
    """Scrapes a simple table where the first row is column headers (one
    per product) and every following row is one label cell plus one value
    cell per product, in the SAME column order as the header row -- e.g.
    Enterprise Bank & Trust's credit card comparison table, which (unlike
    Century Bank, SECU NM, and First National 1870) genuinely
    differentiates terms per card tier: three different annual fees and
    two different APR bands.

    Column order from the header row is trusted directly by position --
    no per-cell mobile-title lookup is needed here, unlike
    comparison_table_scraper.py's responsive widget, since this is a plain
    table with no mobile-duplicated markup.

    Field matching is by EXACT label text (case-insensitive, whitespace-
    normalized), matching schumer_box_scraper.py's reasoning: a source
    whose own labels can collide shouldn't be substring-matched.

    A single source row can back MULTIPLE canonical fields -- e.g. one
    "APR for Purchases and Balance Transfers" cell contains both an intro-
    APR sentence and the standing APR range in one paragraph.
    `field_extract_patterns` lets a field pull just its own portion out of
    that row's cell text via a single-capture-group regex; a field with no
    pattern uses the cell's full cleaned text as-is.

    `fixed_not_disclosed_fields` sets a field to NOT_PUBLICLY_DISCLOSED on
    every included product regardless of what's on the page -- for fields
    this document simply never states at all (as opposed to a field this
    scraper looked for and didn't find).

    Footnote reference markers (`<sup>` links, e.g. "No¹") are stripped
    before any text extraction -- they're citation markers, not part of
    the fee value.

    Also emits a staleness warning from a configured `staleness_pattern`
    (one capture group, a date) against the page's own prose -- separate
    from, and potentially different in age from, any staleness warning a
    companion PDF/table source for the same institution might raise.
    """

    def __init__(self, name, url, config):
        super().__init__(name, url, config)

    @staticmethod
    def _clean_cell(cell):
        cell_copy = copy.deepcopy(cell)
        for sup in cell_copy.find_all("sup"):
            sup.decompose()
        return " ".join(cell_copy.get_text(" ", strip=True).replace("\xa0", " ").split())

    def _check_staleness(self, text):
        """Returns the page's own self-declared date string (e.g.
        "06/27/2022"), if a `staleness_pattern` is configured and matches --
        so callers can thread the live-extracted date through (e.g. into
        credit_card_matrix.py's effective_date row) instead of a hardcoded
        copy that could drift from what the page actually says. Also warns
        here, once, if that date is over a year old.
        """
        pattern = self.config.get("staleness_pattern")
        if not pattern:
            return None
        m = re.search(pattern, text, re.IGNORECASE)
        if not m:
            return None
        date_str = m.group(1)
        date_format = self.config.get("staleness_date_format", "%m/%d/%Y")
        try:
            parsed = datetime.strptime(date_str, date_format).date()
        except ValueError:
            return date_str
        age_days = (date.today() - parsed).days
        if age_days > 365:
            msg = (
                f"[{self.name}] Credit card page states rate information is accurate as of "
                f"{date_str} -- that's {age_days // 365} year(s) old. Card terms have not been "
                f"independently reconfirmed since then; verify they're still accurate."
            )
            logger.warning(msg)
            self.warnings.append(msg)
        return date_str

    def scrape(self):
        response = self.fetch_url()
        if not response:
            return []

        soup = BeautifulSoup(response.content, "html.parser")
        source_effective_date = self._check_staleness(" ".join(soup.get_text(separator=" ", strip=True).split()))

        table = soup.select_one(self.config.get("table_selector", "table"))
        if not table:
            msg = f"[{self.name}] No product comparison table found on page."
            logger.warning(msg)
            self.warnings.append(msg)
            return []

        rows = table.find_all("tr")
        if not rows:
            msg = f"[{self.name}] Comparison table found but has no rows."
            logger.warning(msg)
            self.warnings.append(msg)
            return []

        header_cells = rows[0].find_all(["th", "td"])
        product_titles = [self._clean_cell(c) for c in header_cells[1:]]

        products_cfg = self.config.get("products", {})
        included = [i for i, title in enumerate(product_titles) if title in products_cfg]
        missing = set(products_cfg.keys()) - set(product_titles)
        if missing:
            msg = f"[{self.name}] Expected product column(s) not found on page: {sorted(missing)}"
            logger.warning(msg)
            self.warnings.append(msg)

        field_row_labels = self.config.get("field_keywords", {})
        extract_patterns = self.config.get("field_extract_patterns", {})
        not_disclosed = self.config.get("fixed_not_disclosed_fields", [])

        cards = {
            i: {
                "card_name": products_cfg[product_titles[i]].get("product_name", product_titles[i]),
                "category": products_cfg[product_titles[i]].get("category", "uncategorized"),
                "_field_confidence": {},
            }
            for i in included
        }
        found_fields = {i: set() for i in included}

        for row in rows[1:]:
            cells = row.find_all(["th", "td"])
            if not cells:
                continue
            label = self._clean_cell(cells[0])
            label_lower = label.lower()
            matched_fields = [
                field for field, labels in field_row_labels.items()
                if any(label_lower == kw.lower() for kw in labels)
            ]
            if not matched_fields:
                continue

            data_cells = cells[1:]
            for i in included:
                if i >= len(data_cells):
                    continue
                raw_value = self._clean_cell(data_cells[i])
                if not raw_value:
                    continue
                for field in matched_fields:
                    pattern = extract_patterns.get(field)
                    if pattern:
                        m = re.search(pattern, raw_value, re.IGNORECASE)
                        value = m.group(1).strip() if m else None
                    else:
                        value = raw_value
                    if value:
                        cards[i][field] = self.clean_value(value)
                        cards[i]["_field_confidence"][field] = "high"
                        found_fields[i].add(field)

        out = []
        for i in included:
            card = cards[i]
            for field in field_row_labels:
                if field not in found_fields[i]:
                    self.log_field_warning(card["card_name"], field)
            for field in not_disclosed:
                card[field] = NOT_PUBLICLY_DISCLOSED
            if source_effective_date:
                card["_matrix_extra"] = {"effective_date": source_effective_date}
            out.append(self.finalize_card(card))

        return out
