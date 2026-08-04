"""LLM-assisted extraction fallback, used only when deterministic
extraction (regex/CSS selectors) finds nothing on a page that loaded
successfully -- plus the once-a-day accuracy audit (see daily_audit.py),
which calls the same entry point to re-confirm a value that DID extract.

This is deliberately a fallback, not a replacement: regex/selector
extraction runs first everywhere it's used, since it's instant, free, and
either matches or it doesn't -- an LLM read is slower, costs API quota, and
is a probabilistic judgment call rather than a deterministic match. It
only gets invoked when the deterministic path has already failed (or, for
the daily audit, once per day per field to spot-check).

Every value this produces is tagged "llm_assisted" confidence -- the
weakest tier in attribution.py's confidence ranking, always flagged in the
report, never silently promoted to "verified" the way a human-confirmed
correction can be (see feedback.py). The extraction prompt explicitly
instructs the model to say it can't find something rather than guess,
because a plausible-sounding wrong number is worse than an honest gap.

Requires the GEMINI_API_KEY environment variable. If it's not set, or the
`google-genai` package isn't installed, every call here returns (None, a
clear reason) rather than raising -- a scraper using this fallback should
degrade to its pre-existing "field not found" behavior, not crash.

RATE LIMITING / CALL BUDGET, module-level, shared by EVERY caller
(try_llm_recovery's drift-gated recovery AND daily_audit.py's spot-check --
there is no separate budget per feature, on purpose, so the two can never
combine to exceed what's configured here regardless of how much either one
individually wants to call):

  - MIN_SECONDS_BETWEEN_CALLS paces requests well under free-tier RPM
    limits (Gemini 2.5 Flash's free tier is on the order of 10
    requests/minute -- verify your actual project limit in Google AI
    Studio, since Google no longer publishes a single guaranteed table).
  - MAX_CALLS_PER_RUN caps total calls in one `python run.py` invocation,
    so even a worst-case event (many fields regressing AND the daily audit
    firing in the same run) can't approach a daily quota that resets once
    every 24 hours while this workflow runs once a day.
  - A detected quota-exhausted response (HTTP 429 / RESOURCE_EXHAUSTED)
    sets a run-level flag that skips all further calls for the rest of
    this run, rather than retrying and failing repeatedly against an
    already-exhausted quota.

None of this is a substitute for checking your actual project quota in
Google AI Studio -- it's a conservative default meant to keep this
workflow's free-tier usage nowhere near whatever that quota turns out to
be, not a precise replica of it.
"""

import logging
import os
import time

logger = logging.getLogger("FeeComparisonScraper")

MODEL = "gemini-2.5-flash"
MAX_PAGE_CHARS = 15000  # keeps requests well under free-tier token limits

MIN_SECONDS_BETWEEN_CALLS = 6.5  # a bit over 60s/10RPM, as a safety margin
MAX_CALLS_PER_RUN = 20

_last_call_time = None
_calls_this_run = 0
_quota_exhausted = False


def reset_run_state():
    """Resets the call budget/rate-limit state -- call once at the start
    of each `python run.py` invocation (run.py does this). Exists mainly
    so tests and any other multi-run-in-one-process caller don't
    accidentally inherit a previous run's exhausted-quota flag or call
    count; a real, separate `python run.py` process starts with these at
    their module-load defaults regardless.
    """
    global _last_call_time, _calls_this_run, _quota_exhausted
    _last_call_time = None
    _calls_this_run = 0
    _quota_exhausted = False


def _looks_like_quota_error(exc):
    text = str(exc).lower()
    return "429" in text or "resource_exhausted" in text or "rate limit" in text or "quota" in text


def extract_field_via_llm(page_text, field_description, context=""):
    """Asks Gemini to find one specific fact in already-fetched page text.

    Returns (value, reason):
      - (str, None) on success -- `value` is the model's best-effort answer.
      - (None, str) on failure/unavailability -- `reason` explains why, for
        logging; never raises.
    """
    global _last_call_time, _calls_this_run, _quota_exhausted

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return None, "GEMINI_API_KEY not configured -- LLM fallback skipped"

    if _quota_exhausted:
        return None, "Gemini appeared to hit its quota earlier this run -- skipping further calls"

    if _calls_this_run >= MAX_CALLS_PER_RUN:
        return None, f"Reached this run's Gemini call budget ({MAX_CALLS_PER_RUN}) -- skipping further calls"

    try:
        from google import genai
        from google.genai import types
    except ImportError as e:
        return None, f"google-genai package not installed: {e}"

    if not page_text or not page_text.strip():
        return None, "no page text to search"

    prompt = (
        "You are extracting one specific fact from a financial institution's "
        "fee disclosure or agreement page. Find exactly this:\n\n"
        f"{field_description}\n"
        + (f"\nAdditional context: {context}\n" if context else "")
        + "\nReturn ONLY the value as it literally appears in the text (a dollar "
        "amount, percentage, or short phrase) -- no explanation, no extra words. "
        "If this specific information is not explicitly stated anywhere in the "
        "text below, respond with exactly: NOT_FOUND\n"
        "Do not guess, infer, or use knowledge from outside this text.\n\n"
        "--- PAGE TEXT ---\n"
        f"{page_text[:MAX_PAGE_CHARS]}\n"
        "--- END PAGE TEXT ---"
    )

    if _last_call_time is not None:
        wait = MIN_SECONDS_BETWEEN_CALLS - (time.monotonic() - _last_call_time)
        if wait > 0:
            logger.info(f"[LLM fallback] Pacing Gemini calls -- waiting {wait:.1f}s.")
            time.sleep(wait)

    try:
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model=MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(temperature=0.0, max_output_tokens=200),
        )
        answer = (response.text or "").strip()
    except Exception as e:
        _last_call_time = time.monotonic()
        _calls_this_run += 1
        msg = f"Gemini API call failed: {e}"
        if _looks_like_quota_error(e):
            _quota_exhausted = True
            msg += " -- looks like a quota/rate-limit error, skipping further Gemini calls this run"
        logger.error(f"[LLM fallback] {msg}")
        return None, msg

    _last_call_time = time.monotonic()
    _calls_this_run += 1

    if not answer or answer.upper() == "NOT_FOUND":
        return None, "LLM could not find this field in the page text"

    logger.info(f"[LLM fallback] Extracted via {MODEL}: {field_description!r} -> {answer!r}")
    return answer, None
