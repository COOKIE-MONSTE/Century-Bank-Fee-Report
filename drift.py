"""Flags fee facts whose value changed since the last committed snapshot.

This module doesn't try to tell a real site update apart from a new
scraping regression -- it only surfaces that *something* changed, so a
human can make that call. A confirmed verdict on a flagged change is what
feeds back into the system (see feedback.py) and is what actually reduces
how often this kind of flag needs a second look in the future.
"""

import json
import logging
import os

logger = logging.getLogger("FeeComparisonScraper")

HISTORY_PATH = "data/history.json"


def load_previous_fee_facts(history_path=HISTORY_PATH):
    """Returns the fee_facts list from the last committed history.json, or
    [] if none exists yet (e.g. the very first run). Must be called before
    that file gets overwritten by this run.
    """
    if not os.path.exists(history_path):
        return []
    try:
        with open(history_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("fee_facts", [])
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Could not read previous history for drift comparison: {e}")
        return []


def _fact_key(fact):
    return (fact["institution"], fact["category"], fact["fee_type"])


def mark_drift(facts_by_institution, previous_fee_facts):
    """Annotates each fact in-place with `changed_since_last_snapshot` and
    `previous_value`.

    Matched by (institution, category, fee_type) plus overlap of
    scope_products, so a fact that split into two more specific facts (or
    merged into a broader one) since yesterday still finds its closest
    predecessor instead of matching nothing and going unflagged.
    """
    previous_by_key = {}
    for fact in previous_fee_facts:
        previous_by_key.setdefault(_fact_key(fact), []).append(fact)

    for inst_name, facts in facts_by_institution.items():
        for fact in facts:
            key = (inst_name, fact["category"], fact["fee_type"])
            candidates = previous_by_key.get(key, [])

            match = None
            fact_products = set(fact["scope_products"])
            for cand in candidates:
                if set(cand.get("scope_products") or []) & fact_products:
                    match = cand
                    break
            if match is None and len(candidates) == 1:
                match = candidates[0]

            if match is not None and match["value"] != fact["value"]:
                fact["changed_since_last_snapshot"] = True
                fact["previous_value"] = match["value"]
            else:
                fact["changed_since_last_snapshot"] = False
                fact["previous_value"] = None
