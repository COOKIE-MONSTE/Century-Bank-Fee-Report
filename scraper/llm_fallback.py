"""LLM-assisted extraction fallback, used only when deterministic
extraction (regex/CSS selectors) finds nothing on a page that loaded
successfully.

This is deliberately a fallback, not a replacement: regex/selector
extraction runs first everywhere it's used, since it's instant, free, and
either matches or it doesn't -- an LLM read is slower, costs API quota, and
is a probabilistic judgment call rather than a deterministic match. It
only gets invoked when the deterministic path has already failed.

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
"""

import logging
import os

logger = logging.getLogger("FeeComparisonScraper")

MODEL = "gemini-2.5-flash"
MAX_PAGE_CHARS = 15000  # keeps requests well under free-tier token limits


def extract_field_via_llm(page_text, field_description, context=""):
    """Asks Gemini to find one specific fact in already-fetched page text.

    Returns (value, reason):
      - (str, None) on success -- `value` is the model's best-effort answer.
      - (None, str) on failure/unavailability -- `reason` explains why, for
        logging; never raises.
    """
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return None, "GEMINI_API_KEY not configured -- LLM fallback skipped"

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

    try:
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model=MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(temperature=0.0, max_output_tokens=200),
        )
        answer = (response.text or "").strip()
    except Exception as e:
        msg = f"Gemini API call failed: {e}"
        logger.error(f"[LLM fallback] {msg}")
        return None, msg

    if not answer or answer.upper() == "NOT_FOUND":
        return None, "LLM could not find this field in the page text"

    logger.info(f"[LLM fallback] Extracted via {MODEL}: {field_description!r} -> {answer!r}")
    return answer, None
