import logging

from bs4 import BeautifulSoup

from .base import BaseScraper

logger = logging.getLogger("FeeComparisonScraper")


class ComparisonTableScraper(BaseScraper):
    """Scrapes a side-by-side product comparison table where each column is
    a product and each row is a labeled fee/feature -- e.g. First National
    1870's checking-accounts and savings-accounts pages.

    Product identity is read from each DATA row's own per-cell mobile-title
    element (`config['mobile_title_selector']`), not from the header row:
    the header row's leading cell is inconsistent across pages on this CMS
    (sometimes a real title, sometimes a screen-reader-only empty-cell
    placeholder), while every data cell's mobile-title is guaranteed 1:1
    with that row's real `<td>` cells by construction.

    Row labels are matched against `field_keywords` by PREFIX (not
    substring): this page's own labels collide under substring matching --
    "Minimum Daily Balance to Avoid Monthly Account Maintenance Fee" ends
    with the same phrase used to identify the "Monthly Account Maintenance
    Fee" row itself, so a substring match would let the balance-waiver
    threshold overwrite the actual fee amount depending on row order.

    Rows matching `informational_keywords` (also by prefix) but no
    canonical field are collected into one "fee-adjacent facts" warning per
    product (see labeled_features_scraper.py for the same convention) --
    every other unmatched row (marketing copy, benefit callouts) is
    silently ignored rather than added to that note, so the warning stays
    a short, genuinely useful list instead of a dump of the whole table.
    """

    def __init__(self, name, url, config):
        super().__init__(name, url, config)

    @staticmethod
    def _normalize(text):
        return " ".join(text.replace("\xa0", " ").split())

    def scrape(self):
        response = self.fetch_url()
        if not response:
            return []

        soup = BeautifulSoup(response.content, "html.parser")
        table = soup.select_one(self.config.get("table_selector", "table.comparison-table"))
        if not table:
            msg = f"[{self.name}] No comparison table found on page."
            logger.warning(msg)
            self.warnings.append(msg)
            return []

        row_selector = self.config.get("row_selector", "tr.comparison-table__row")
        label_selector = self.config.get("label_selector", "th.comparison-table__data-category")
        data_cell_selector = self.config.get("data_cell_selector", "td.comparison-table__data")
        container_selector = self.config.get("container_selector", "div.comparison-table__data-container")
        mobile_title_selector = self.config.get("mobile_title_selector", "h3.comparison-table__mobile-title")

        data_rows = table.select(row_selector)
        if not data_rows:
            msg = f"[{self.name}] Comparison table found but no data rows matched '{row_selector}'."
            logger.warning(msg)
            self.warnings.append(msg)
            return []

        first_data_cells = data_rows[0].select(data_cell_selector)
        product_titles = []
        for cell in first_data_cells:
            title_el = cell.select_one(mobile_title_selector)
            product_titles.append(self._normalize(title_el.get_text(" ", strip=True)) if title_el else "")

        products_cfg = self.config.get("products", {})
        included = [i for i, title in enumerate(product_titles) if title in products_cfg]
        missing = set(products_cfg.keys()) - set(product_titles)
        if missing:
            msg = f"[{self.name}] Expected product column(s) not found on page: {sorted(missing)}"
            logger.warning(msg)
            self.warnings.append(msg)

        field_keywords = self.get_keywords()
        informational_keywords = [kw.lower() for kw in self.config.get("informational_keywords", [])]
        value_templates = self.config.get("field_value_templates", {})

        cards = {
            i: {
                "card_name": products_cfg[product_titles[i]].get("product_name", product_titles[i]),
                "category": products_cfg[product_titles[i]].get("category", "uncategorized"),
                "_field_confidence": {},
            }
            for i in included
        }
        unmatched_notes = {i: [] for i in included}
        found_fields = {i: set() for i in included}

        for row in data_rows:
            label_el = row.select_one(label_selector)
            if not label_el:
                continue
            label = self._normalize(label_el.get_text(" ", strip=True))
            label_lower = label.lower()

            matched_field = None
            for field, kw_list in field_keywords.items():
                if any(label_lower.startswith(kw.lower()) for kw in kw_list):
                    matched_field = field
                    break
            is_informational = matched_field is None and any(
                label_lower.startswith(kw) for kw in informational_keywords
            )

            data_cells = row.select(data_cell_selector)
            for i in included:
                if i >= len(data_cells):
                    continue
                container = data_cells[i].select_one(container_selector) or data_cells[i]
                mobile_title_el = container.select_one(mobile_title_selector)
                mobile_text = mobile_title_el.get_text(strip=True) if mobile_title_el else ""
                full_text = container.get_text(" ", strip=True)
                value = full_text.replace(mobile_text, "", 1).strip() if mobile_text else full_text
                value = self._normalize(value)
                if not value:
                    continue

                if matched_field:
                    template = value_templates.get(matched_field)
                    final_value = template.format(value=value) if template else value
                    cards[i][matched_field] = self.clean_value(final_value)
                    cards[i]["_field_confidence"][matched_field] = "high"
                    found_fields[i].add(matched_field)
                elif is_informational:
                    unmatched_notes[i].append(f"{label}: {value}")

        out = []
        for i in included:
            card = cards[i]
            for field in field_keywords:
                if field not in found_fields[i]:
                    self.log_field_warning(card["card_name"], field)
            if unmatched_notes[i]:
                msg = (
                    f"[{self.name} - {card['card_name']}] Fee-adjacent facts found with no existing "
                    f"canonical field: {'; '.join(unmatched_notes[i])}"
                )
                logger.info(msg)
                self.warnings.append(msg)
            out.append(self.finalize_card(card))

        return out
