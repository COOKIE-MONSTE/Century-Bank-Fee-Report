import copy
import logging

from bs4 import BeautifulSoup

from .base import BaseScraper

logger = logging.getLogger("FeeComparisonScraper")


class SchumerBoxScraper(BaseScraper):
    """Scrapes a single Truth-in-Lending "Schumer box" disclosure table that
    applies identically to several card tiers -- e.g. First National 1870's
    Classic/Gold/Platinum Visa cards, which all share one TILA table with no
    per-card fee differentiation published anywhere.

    The table has two row shapes, both handled here:
      - a plain two-cell row (`<td>Label</td><td>Value</td>`)
      - a grouped row, where the label cell is a group heading plus a
        `<ul><li>` list of sub-fees (e.g. "Transaction Fees" grouping
        Balance Transfer / Cash Advances / Foreign Transaction), and the
        value cell holds one `<br>`-separated segment per sub-fee in the
        same order. `<br>` tags can be nested inside inline formatting
        (e.g. `<strong>$15.00<br>$10.00</strong>`), so segments are split
        by replacing every `<br>` -- at any depth -- with a newline on a
        detached copy of the cell, not by walking only direct children.
        If the segment count doesn't match the sub-fee count, the row is
        kept as one combined low-confidence fact instead of guessing which
        segment belongs to which sub-fee.

    Field matching is by EXACT label text (case-insensitive, whitespace/
    &nbsp;-normalized), not substring containment like most other scrapers
    here: this table's own labels overlap in a way that makes substring
    matching actively wrong -- "APR for Cash Advances" contains "Cash
    Advances", which is also the bare sub-label for the Cash Advance FEE
    row, so a substring match would let one field's keyword steal the
    other's row.

    Every field applies identically to every configured product/tier (the
    whole reason this table gets its own scraper instead of HTMLScraper's
    per-card-template approach), so one shared dict is copied out to N
    cards, one per `products` entry in config.
    """

    def __init__(self, name, url, config):
        super().__init__(name, url, config)

    @staticmethod
    def _normalize(text):
        return " ".join(text.replace("\xa0", " ").split())

    @classmethod
    def _split_on_br(cls, cell):
        """Splits a value cell's text on every <br>, regardless of nesting
        depth, by replacing each <br> with a newline on a detached copy
        (so the live page tree is never mutated) and splitting the
        resulting text on that newline.
        """
        cell_copy = copy.deepcopy(cell)
        for br in cell_copy.find_all("br"):
            br.replace_with("\n")
        text = cell_copy.get_text(" ", strip=False)
        return [cls._normalize(seg) for seg in text.split("\n") if cls._normalize(seg)]

    def _extract_pairs(self, table):
        """Returns [(label, value, confidence)], one entry per leaf fact."""
        pairs = []
        for row in table.find_all("tr"):
            cells = row.find_all(["td", "th"], recursive=False)
            if len(cells) != 2:
                continue
            key_cell, val_cell = cells
            sub_items = key_cell.find_all("li")
            if sub_items:
                sub_labels = [self._normalize(li.get_text(" ", strip=True)) for li in sub_items]
                segments = self._split_on_br(val_cell)
                if len(segments) == len(sub_labels):
                    for label, value in zip(sub_labels, segments):
                        pairs.append((label, value, "high"))
                    continue
                # Segment count doesn't match sub-fee count -- don't guess
                # which segment belongs to which sub-label.
                combined_label = self._normalize(key_cell.get_text(" ", strip=True))
                combined_value = self._normalize(val_cell.get_text(" ", strip=True))
                pairs.append((combined_label, combined_value, "low"))
                continue
            label = self._normalize(key_cell.get_text(" ", strip=True))
            value = self._normalize(val_cell.get_text(" ", strip=True))
            if label and value:
                pairs.append((label, value, "high"))
        return pairs

    def scrape(self):
        response = self.fetch_url()
        if not response:
            return []

        soup = BeautifulSoup(response.content, "html.parser")
        table = soup.select_one(self.config.get("table_selector", "table"))
        if not table:
            msg = f"[{self.name}] No Schumer box table found on page."
            logger.warning(msg)
            self.warnings.append(msg)
            return []

        pairs = self._extract_pairs(table)

        field_keywords = self.config.get("field_keywords", {})
        matrix_extra_keywords = self.config.get("matrix_extra_keywords", {})
        range_fields = set(self.config.get("range_fields", []))
        first_percentage_fields = set(self.config.get("first_percentage_fields", []))

        shared_fields = {}
        field_confidence = {}
        matrix_extra = {}
        matched_labels = set()

        for label, value, confidence in pairs:
            label_lower = label.lower()
            for field, keywords in field_keywords.items():
                if any(label_lower == kw for kw in keywords):
                    final_value = value
                    if field in range_fields:
                        cleaned = self.extract_last_percentage_range(value)
                        final_value = cleaned or value
                    elif field in first_percentage_fields:
                        cleaned = self.extract_first_percentage(value)
                        final_value = cleaned or value
                    shared_fields[field] = self.clean_value(final_value)
                    field_confidence[field] = confidence
                    matched_labels.add(label_lower)
            for extra_key, keywords in matrix_extra_keywords.items():
                if any(label_lower == kw for kw in keywords):
                    matrix_extra[extra_key] = self.clean_value(value)
                    matched_labels.add(label_lower)

        for field in field_keywords:
            if field not in shared_fields:
                self.log_field_warning("Schumer box", field)

        source_urls = {field: self.url for field in shared_fields}
        source_urls.update({key: self.url for key in matrix_extra})

        cards = []
        for product_name, product_cfg in self.config.get("products", {}).items():
            card = dict(shared_fields)
            card["card_name"] = product_cfg.get("product_name", product_name)
            card["category"] = product_cfg.get("category", "uncategorized")
            card["_field_confidence"] = dict(field_confidence)
            card["_matrix_extra"] = dict(matrix_extra)
            card["_source_urls"] = dict(source_urls)
            cards.append(self.finalize_card(card))

        return cards
