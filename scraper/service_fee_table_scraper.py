import io
import logging
import re
from collections import defaultdict

import pdfplumber
import requests
from bs4 import BeautifulSoup

from .base import BaseScraper, default_field_description
from .line_item_fee_scraper import _LINE_PATTERN

logger = logging.getLogger("FeeComparisonScraper")

# Matches a fill-in-the-blank effective date stated as
# "...as of _0_7_/1_4_/_2_0_2_3______:" -- the source PDF is a form
# template where every character position in the blank gets its own
# underscore for visual fill-in styling, so the real date is interleaved
# with underscores rather than written cleanly. Captures the whole
# underscore-and-digit run and strips underscores after matching, rather
# than trying to skip them within the pattern itself.
_PDF_EFFECTIVE_DATE_RE = re.compile(r"as of\s+([_\d/]+)\s*:", re.IGNORECASE)


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

    `footnote_marker`/`footnote_selector`/`footnote_fields`: some rows
    carry a footnote marker in their own label (e.g. SECU NM's
    "International ATM/Debit Card Transactions*"), with the actual
    explanatory text living in a separate element after the table (e.g.
    "*At Cost. Fee is ... typically 1% ... International ATM/check card
    transactions only.") rather than in the table cell itself -- so the
    bare cell value ("Variable") loses the one thing that makes it useful.
    When configured, the footnote element matching `footnote_selector`
    whose text starts with `footnote_marker` (and not a longer run of the
    same marker character, so a single "*" doesn't swallow a "**"
    footnote) is fetched live and appended to any field in
    `footnote_fields` whose winning source row's own label carried that
    marker.
    """

    def __init__(self, name, url, config):
        super().__init__(name, url, config)

    @staticmethod
    def _extract_pdf_page(url, page_num, split_x):
        """Fetches `url` and reads page `page_num` (0-indexed) as a
        two-column fee schedule: (full_text, label_value_pairs).

        Some SECU-style PDFs lay their fee schedule out as two side-by-
        side columns of "Label $Value" lines flattened into ONE line of
        text per row by naive extraction (e.g. "Account Reconciliation or
        Research $20.00 per hour Garnishment or IRS Levy Fee $50.00" is
        actually two unrelated rows, one per column). Splitting words by
        x-position (`split_x`) into a left-column line stream and a
        right-column line stream BEFORE regex-parsing each line
        reconstructs two independent single-column documents -- each of
        which fits the same "Label $Value" per-line shape
        line_item_fee_scraper.py's `_LINE_PATTERN` already parses, reused
        here rather than re-implemented. A row that only exists as a
        section header on one line with its value on the NEXT visual row
        (e.g. "Wire Transfer" / "Domestic $15.00") simply doesn't produce
        a header+value pair -- only lines with a value are captured, so a
        bare header line is silently dropped rather than mis-paired.
        """
        response = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            timeout=20,
        )
        response.raise_for_status()
        pdf = pdfplumber.open(io.BytesIO(response.content))
        if page_num < 0 or page_num >= len(pdf.pages):
            raise ValueError(f"page {page_num + 1} out of range ({len(pdf.pages)} pages)")
        page = pdf.pages[page_num]
        full_text = page.extract_text() or ""

        rows = defaultdict(list)
        for w in page.extract_words():
            rows[round(w["top"], 1)].append(w)

        pairs = {}
        for top in sorted(rows.keys()):
            ws = sorted(rows[top], key=lambda w: w["x0"])
            left = " ".join(w["text"] for w in ws if w["x0"] < split_x)
            right = " ".join(w["text"] for w in ws if w["x0"] >= split_x)
            for line in (left, right):
                m = _LINE_PATTERN.match(line)
                if m:
                    label, value = m.group(1).strip(), m.group(2).strip()
                    if label:
                        pairs[label.lower()] = value

        return full_text, pairs

    @staticmethod
    def _find_footnote(soup, selector, marker):
        for el in soup.select(selector):
            text = " ".join(el.get_text(separator=" ", strip=True).split())
            if text.startswith(marker) and text[len(marker):len(marker) + 1] != marker[-1:]:
                return text[len(marker):].strip()
        return None

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

        footnote_marker = self.config.get("footnote_marker")
        footnote_selector = self.config.get("footnote_selector")
        footnote_fields = set(self.config.get("footnote_fields", []))
        footnote_text = None
        if footnote_marker and footnote_selector and footnote_fields:
            footnote_text = self._find_footnote(soup, footnote_selector, footnote_marker)
            if not footnote_text:
                msg = (
                    f"[{self.name}] Configured footnote marker '{footnote_marker}' via selector "
                    f"'{footnote_selector}' was not found on the page -- fields {sorted(footnote_fields)} "
                    f"will report their bare table value with no footnote appended."
                )
                logger.warning(msg)
                self.warnings.append(msg)

        for field, entries in candidates.items():
            distinct_values = list(dict.fromkeys(v for v, _ in entries))
            card[field] = distinct_values[-1]
            if (
                footnote_text
                and field in footnote_fields
                and footnote_marker in entries[-1][1]
            ):
                card[field] = f"{card[field]} -- {footnote_text}"
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

        # Fields this institution's config expects (has a field_keywords
        # entry for) but that matched NO row this run -- unlike a row
        # ending up in `unmatched` (a row exists, just isn't recognized),
        # this is the opposite: no row at all claimed this field. Only
        # tried via Gemini if it's a drift-eligible regression (see
        # try_llm_recovery) -- a field this table has never carried a row
        # for isn't a site change, it's just not on this schedule.
        page_text = soup.get_text(separator=" ", strip=True)
        for field in keywords:
            if field in card:
                continue
            if self.try_llm_recovery(card, field, page_text, default_field_description(field)):
                field_confidence[field] = "llm_assisted"

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

        # UNRESOLVED MAPPING, not a change -- config-supplied `mapping_review_note`
        # renders a Data Quality Note laying out a suspected but unconfirmed
        # mismapping, with the actual current figures pulled live from THIS
        # run's own `card`/`unmatched` data (via str.format(), never typed
        # into the note's text as literals) so the note can't go stale
        # independently of the values it's discussing -- see
        # secu_nm/returned_item_fee in config.yaml for the concrete case.
        # Never edits `card` itself; the mapping stays exactly what
        # field_keywords says until a human confirms it via
        # data/feedback_log.yaml.
        review_note = self.config.get("mapping_review_note")
        if review_note:
            unmatched_by_label = {label: value for label, value in unmatched}
            note_values = {}
            for key in review_note.get("fields", []):
                note_values[key] = card.get(key, "not scraped")
            for label in review_note.get("unmatched_fields", []):
                # str.format() placeholder names can't contain spaces/
                # apostrophes -- normalize the source document's own
                # label into a safe key (config's text: template uses
                # this same normalized form).
                safe_key = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")
                note_values[safe_key] = unmatched_by_label.get(label, "not found this run")
            try:
                msg = f"[{self.name}] " + review_note["text"].format(**note_values)
                logger.info(msg)
                self.warnings.append(msg)
            except KeyError as e:
                msg = (
                    f"[{self.name}] mapping_review_note references an unknown placeholder {e} -- "
                    f"note not rendered this run; fix the config."
                )
                logger.error(msg)
                self.warnings.append(msg)

        # Effective-date + value cross-check against a second document
        # covering the same fee schedule (e.g. SECU NM: fee-schedule.html
        # states its own "Effective" date; its Truth-in-Savings PDF's page
        # 7 fee schedule is the SAME data, independently typeset, with its
        # own "as of" date). Both dates are extracted live every run --
        # neither is ever hardcoded -- and every field this scraper
        # already matched on the HTML page is compared against the PDF's
        # own value for the same label (via the same get_keywords()
        # substring matching used for the HTML table): agreement is
        # reported as cross-source corroboration; a genuine value
        # disagreement gets a specific warning naming the field and both
        # values, instead of forcing every date mismatch into one generic
        # "verify which is authoritative" note regardless of whether the
        # underlying numbers actually differ.
        effective_date_pattern = self.config.get("effective_date_pattern")
        html_date = None
        if effective_date_pattern:
            body_text = soup.get_text(separator=" ", strip=True)
            date_match = re.search(effective_date_pattern, body_text, re.IGNORECASE)
            if date_match:
                html_date = date_match.group(1)
                logger.info(f"[{self.name}] Page states effective date: {html_date}")
            else:
                logger.info(f"[{self.name}] Could not find an effective date on the page matching the configured pattern.")

        cross_check_cfg = self.config.get("cross_check_pdf")
        if cross_check_cfg and html_date:
            try:
                pdf_text, pdf_pairs = self._extract_pdf_page(
                    cross_check_cfg["url"],
                    cross_check_cfg["page"] - 1,
                    cross_check_cfg.get("column_split_x", 340),
                )
            except Exception as e:
                msg = f"[{self.name}] Failed to cross-check against {cross_check_cfg['url']}: {e}"
                logger.error(msg)
                self.warnings.append(msg)
                pdf_text, pdf_pairs = None, None

            if pdf_text is not None:
                date_m = _PDF_EFFECTIVE_DATE_RE.search(pdf_text)
                pdf_date = date_m.group(1).replace("_", "") if date_m else None
                source_label = cross_check_cfg.get("label", cross_check_cfg["url"])

                agreements, disagreements = [], []
                for field in field_confidence:
                    if field not in card:
                        continue
                    matched_pdf_value = None
                    for kw in keywords.get(field, []):
                        for pdf_label, pdf_value in pdf_pairs.items():
                            if kw in pdf_label:
                                matched_pdf_value = pdf_value
                                break
                        if matched_pdf_value:
                            break
                    if not matched_pdf_value:
                        continue
                    html_value = card[field].split(" -- ")[0]  # strip any footnote suffix before comparing
                    if self.clean_value(matched_pdf_value) == html_value:
                        agreements.append(field)
                    else:
                        disagreements.append((field, html_value, matched_pdf_value))

                if not pdf_date:
                    msg = (
                        f"[{self.name}] Cross-check against {source_label} ran, but its own effective "
                        f"date could not be extracted (expected an \"as of ...:\" phrase) -- "
                        f"{len(agreements)} field(s) still compared by value: {agreements or 'none'}."
                    )
                    logger.warning(msg)
                    self.warnings.append(msg)
                elif pdf_date == html_date:
                    logger.info(
                        f"[{self.name}] This page's effective date ({html_date}) matches {source_label} "
                        f"-- {len(agreements)} field(s) cross-checked by value, all agree."
                    )
                elif disagreements:
                    for field, html_value, pdf_value in disagreements:
                        msg = (
                            f"[{self.name}] Value disagreement for '{field}': this page ({html_date}) "
                            f"states {html_value!r}, but {source_label} ({pdf_date}) states {pdf_value!r} "
                            f"-- not silently resolved, verify which is authoritative."
                        )
                        logger.warning(msg)
                        self.warnings.append(msg)
                else:
                    msg = (
                        f"[{self.name}] This page states effective date '{html_date}', but {source_label} "
                        f"states a different date '{pdf_date}' -- despite that, all {len(agreements)} "
                        f"field(s) comparable between the two documents agree on value "
                        f"({', '.join(agreements)}), so the underlying fee amounts are corroborated "
                        f"across two independently typeset sources even though the stated dates "
                        f"disagree; which date is authoritative is still unresolved."
                    )
                    logger.info(msg)
                    self.warnings.append(msg)

                # Facts the cross-check PDF states that aren't fee_type
                # comparisons at all (e.g. membership's Par Value of One
                # Share) -- surfaced as a live-extracted note rather than
                # a fee row, since it isn't a fee any of this report's
                # other tracked products has an equivalent line for.
                for fact in cross_check_cfg.get("extra_facts", []):
                    value = pdf_pairs.get(fact["label"].lower())
                    if value:
                        msg = f"[{self.name}] " + fact["note"].format(value=self.clean_value(value))
                        logger.info(msg)
                        self.warnings.append(msg)
                    else:
                        msg = (
                            f"[{self.name}] Configured extra_facts label {fact['label']!r} was not found "
                            f"in {source_label} this run -- note not rendered."
                        )
                        logger.warning(msg)
                        self.warnings.append(msg)

        return [self.finalize_card(card)]
