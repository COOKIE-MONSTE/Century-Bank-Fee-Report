import logging
import re
from collections import defaultdict
from bs4 import BeautifulSoup
from .base import BaseScraper

logger = logging.getLogger("FeeComparisonScraper")


class ServiceFeeTableScraper(BaseScraper):
    """Scrapes a generic two-column SERVICE / FEE disclosure table.

    Several institutions publish their fee schedule as one flat table
    rather than Nusenda's mix of tables and disclosure paragraphs. Some of
    these tables visually indent sub-items under a preceding row (e.g.
    "Wire Transfer: Domestic" followed by an indented "International" row
    meaning "Wire Transfer: International") -- this scraper reconstructs
    that grouping generically instead of hardcoding one page's structure.

    Every row is a fact about the *same* single card/product (this whole
    table is one fee schedule), so if two differently-worded rows both
    resolve to the same canonical fee_type, that's treated as
    corroboration (raising confidence) when they agree, and as a genuine
    conflict (flagged, not silently resolved) when they don't -- same
    principle as html_scraper's multi-table reconciliation.
    """

    def __init__(self, name, url, config):
        super().__init__(name, url, config)

    def _find_fee_table(self, soup):
        header_hints = [h.lower() for h in self.config.get("table_header_hints", ["service", "fee"])]
        for table in soup.find_all("table"):
            first_row = table.find("tr")
            if not first_row:
                continue
            header_cells = [c.get_text(strip=True).lower() for c in first_row.find_all(["td", "th"])]
            if all(any(hint in cell for cell in header_cells) for hint in header_hints):
                return table
        return None

    def _rows_with_grouping(self, table):
        """Yields (label, value) pairs, combining an indented sub-item's own
        label with its preceding top-level row's label -- see class
        docstring for why. A row with no value (a pure section header,
        e.g. "Copies") is not yielded itself, but still sets the group
        label for the indented rows under it.
        """
        group_label = None
        for tr in table.find_all("tr")[1:]:  # skip header row
            cells = tr.find_all(["td", "th"])
            if len(cells) < 2:
                continue
            first_cell = cells[0]
            label = first_cell.get_text(strip=True)
            value = cells[1].get_text(strip=True)
            is_indent = "indent" in (first_cell.get("class") or [])

            if is_indent and group_label:
                prefix = group_label.split(":")[0].strip()
                full_label = f"{prefix}: {label}"
            else:
                full_label = label
                group_label = label

            if value:
                yield full_label, value

    def scrape(self):
        response = self.fetch_url()
        if not response:
            return []

        soup = BeautifulSoup(response.content, "html.parser")
        table = self._find_fee_table(soup)
        if not table:
            msg = f"[{self.name}] Could not find a SERVICE/FEE table on the page."
            logger.error(msg)
            self.warnings.append(msg)
            return []

        keywords = self.get_keywords()
        candidates = defaultdict(list)  # fee_type -> [(value, source_label), ...]
        unmatched = []

        for label, value in self._rows_with_grouping(table):
            cleaned_value = self.clean_value(value)
            label_lower = label.lower()
            matched_field = None
            for field, kw_list in keywords.items():
                if any(kw in label_lower for kw in kw_list):
                    matched_field = field
                    break

            if matched_field:
                candidates[matched_field].append((cleaned_value, label))
            else:
                unmatched.append((label, cleaned_value))

        card = {
            "card_name": self.config.get("product_name", f"{self.name} Fee Schedule"),
            "category": self.config.get("category", "general_account_fees"),
        }
        field_confidence = {}

        for field, entries in candidates.items():
            distinct_values = list(dict.fromkeys(v for v, _ in entries))
            card[field] = distinct_values[-1]
            if len(distinct_values) == 1:
                field_confidence[field] = "high"
                if len(entries) > 1:
                    logger.info(
                        f"[{self.name}] Field '{field}' corroborated by {len(entries)} matching "
                        f"source row(s) {[l for _, l in entries]} -- all agree on {distinct_values[0]!r}."
                    )
            else:
                field_confidence[field] = "low"
                msg = (
                    f"[{self.name}] Field '{field}' had {len(distinct_values)} conflicting values from "
                    f"different source rows: {list(zip(distinct_values, (l for _, l in entries)))} -- "
                    f"using the last one seen ({distinct_values[-1]!r}); verify manually."
                )
                logger.warning(msg)
                self.warnings.append(msg)

        card["_field_confidence"] = field_confidence
        self.apply_field_aliases(card)

        if unmatched:
            preview = "; ".join(f"{label!r}: {value!r}" for label, value in unmatched)
            msg = (
                f"[{self.name}] {len(unmatched)} fee row(s) did not match any known category "
                f"(shown for review, not forced into an existing one): {preview}"
            )
            logger.info(msg)
            self.warnings.append(msg)

        # Effective-date conflict surfacing: this page may state its own
        # "effective" date, and a config-supplied list of other documents
        # covering the same data can state a *different* one (a real,
        # observed situation for SECU NM: fee-schedule.html says 01/23/23,
        # but its Truth-in-Savings PDF's own fee schedule section -- same
        # values -- says 07/14/2023). Rather than silently trusting
        # whichever date this page happens to show, the conflict is always
        # surfaced as a warning so a human decides which is authoritative.
        effective_date_pattern = self.config.get("effective_date_pattern")
        if effective_date_pattern:
            body_text = soup.get_text(separator=" ", strip=True)
            date_match = re.search(effective_date_pattern, body_text, re.IGNORECASE)
            if date_match:
                effective_date = date_match.group(1)
                logger.info(f"[{self.name}] Page states effective date: {effective_date}")
                for other_source, other_date in self.config.get("known_conflicting_dates", {}).items():
                    if other_date != effective_date:
                        msg = (
                            f"[{self.name}] Effective-date conflict: this page states '{effective_date}', "
                            f"but {other_source} states '{other_date}' for what appears to be the same "
                            f"fee schedule (values match) -- not silently resolved, verify which is "
                            f"authoritative."
                        )
                        logger.warning(msg)
                        self.warnings.append(msg)
            else:
                logger.info(f"[{self.name}] Could not find an effective date on the page matching the configured pattern.")

        return [self.finalize_card(card)]
