"""Once-a-day accuracy spot-check: picks ONE canonical fee field, re-reads
every institution's current source page for it via Gemini, and corrects
the stored value if the fresh read disagrees.

This is deliberately different from scraper/base.py's try_llm_recovery():
that only fires when a field's regex/selector finds NOTHING (a hard
extraction failure). This module instead double-checks fields that DID
extract successfully -- on the theory that a pattern can keep matching
*something* on a page that was redesigned or repriced without the pattern
itself ever breaking, so a value can go quietly wrong while still "working"
every day. Between the two, most classes of drift get a check: a field
that stops matching entirely is caught by try_llm_recovery every run; a
field that keeps matching but drifted to a wrong number gets caught here,
roughly once every len(report.FIELD_ORDER) days as the pick rotates.

Both this module and try_llm_recovery call the exact same
scraper.llm_fallback.extract_field_via_llm(), which enforces one shared
call-spacing/budget/quota-exhaustion state -- there is no separate Gemini
budget for the audit, so it can never combine with recovery calls to
exceed what llm_fallback.py allows in a single run.

The pick is DETERMINISTIC per calendar day (date.toordinal() modulo the
field list), not pure random-each-call: a workflow re-run on the same day
(e.g. retrying a failed job) audits the same field instead of spending a
second field's worth of Gemini calls, and every field gets audited on a
predictable cadence instead of a pure random.choice() potentially leaving
some fields unchecked for a long stretch by chance.
"""

import io
import logging
from datetime import date

import pypdf
import requests
from bs4 import BeautifulSoup

from report import FIELD_ORDER
from scraper import llm_fallback
from scraper.base import _NON_REAL_VALUES, default_field_description

logger = logging.getLogger("FeeComparisonScraper")

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}


def field_of_the_day(today=None):
    d = today or date.today()
    return FIELD_ORDER[d.toordinal() % len(FIELD_ORDER)]


def _clean(text):
    return " ".join((text or "").split()).strip(":,; ")


def _fetch_page_text(url):
    """Same PDF-vs-HTML detection RegexValueScraper uses -- kept as a
    small standalone copy here rather than imported, since this module
    isn't a BaseScraper subclass and doesn't want the rest of that
    class's state.
    """
    try:
        response = requests.get(url, headers=_HEADERS, timeout=20)
        response.raise_for_status()
    except Exception as e:
        logger.warning(f"[Daily audit] Failed to fetch {url}: {e}")
        return None

    is_pdf = "pdf" in response.headers.get("Content-Type", "").lower() or url.lower().split("?")[0].endswith(".pdf")
    try:
        if is_pdf:
            reader = pypdf.PdfReader(io.BytesIO(response.content))
            return " ".join(" ".join((page.extract_text() or "").split()) for page in reader.pages)
        soup = BeautifulSoup(response.content, "html.parser")
        return " ".join(soup.get_text(separator=" ", strip=True).split())
    except Exception as e:
        logger.warning(f"[Daily audit] Failed to parse {url}: {e}")
        return None


def _values_roughly_agree(current, fresh):
    c, f = current.lower(), fresh.lower()
    return c in f or f in c


def run_daily_audit(config, results, today=None):
    """Mutates `results` in place (same shape scrape_all() returns:
    {inst_key: {"name", "cards", "warnings"}}) -- called after scraping,
    before the corrected values feed into attribution/drift/rendering, so
    a correction here shows up in the report exactly like a normal scrape
    would have, just tagged "llm_assisted" confidence.

    No-ops immediately (no page fetches, no Gemini calls) if
    GEMINI_API_KEY isn't configured, matching every other LLM-assisted
    path's degrade-cleanly behavior.
    """
    import os
    if not os.environ.get("GEMINI_API_KEY"):
        logger.info("[Daily audit] GEMINI_API_KEY not configured -- skipping.")
        return

    field = field_of_the_day(today)
    logger.info(f"[Daily audit] Field of the day: '{field}'")

    checked = 0
    updated = 0
    page_text_cache = {}

    for inst_key, inst_result in results.items():
        inst_cfg = config["institutions"].get(inst_key, {})
        url = inst_cfg.get("url")
        if not url:
            continue

        for card in inst_result.get("cards", []):
            current = card.get(field)
            if current in _NON_REAL_VALUES or current is None:
                continue

            if url not in page_text_cache:
                page_text_cache[url] = _fetch_page_text(url)
            page_text = page_text_cache[url]
            if not page_text:
                continue

            checked += 1
            fresh_value, reason = llm_fallback.extract_field_via_llm(
                page_text,
                default_field_description(field),
                context=(
                    f"This institution, {inst_result['name']}, is located in New Mexico or a "
                    f"nearby region. Only use the page text provided -- do not use outside "
                    f"knowledge about any other bank with a similar name. The value currently "
                    f"on record for this fee is {current!r} -- confirm whether the page still "
                    f"supports that figure, or state the figure it actually supports if different."
                ),
            )
            if fresh_value is None:
                logger.info(f"[Daily audit] {inst_result['name']} - '{field}': {reason}")
                continue

            cleaned = _clean(fresh_value)
            if not cleaned or _values_roughly_agree(current, cleaned):
                continue

            msg = (
                f"[Daily audit] {inst_result['name']} - {card.get('card_name')}: field '{field}' "
                f"currently reads {current!r}, but a fresh Gemini read of {url} says {cleaned!r} -- "
                f"updating (tagged llm_assisted); verify manually."
            )
            logger.warning(msg)
            inst_result.setdefault("warnings", []).append(msg)
            card[field] = cleaned
            card.setdefault("_field_confidence", {})[field] = "llm_assisted"
            updated += 1

    logger.info(f"[Daily audit] Checked {checked} value(s) for '{field}', updated {updated}.")
