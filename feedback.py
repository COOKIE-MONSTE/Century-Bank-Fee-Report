"""Turns human verdicts on flagged items into two concrete effects:

1. Promotion: a confirmed real error that included a `synonym` correction
   gets added to the shared cross-institution taxonomy (scraper/fee_taxonomy.py),
   so the phrase that caused it is recognized at every institution from the
   next run on -- this is the piece of state that actually compounds.

2. Calibration: a flag type (e.g. "low_confidence" table conflicts, or
   "mechanism_mismatch") that a human has repeatedly confirmed as a false
   alarm for a given fee_type gets its prominence demoted in future
   reports. It is never hidden outright -- only visually de-emphasized,
   with the track record that justified the demotion shown alongside it --
   so a system that's wrong about its own calibration is still auditable.

You give feedback by adding entries to data/feedback_log.yaml. See that
file for the schema and an example.
"""

import os
from collections import defaultdict

import yaml

FEEDBACK_LOG_PATH = "data/feedback_log.yaml"

# A flag type's false-alarm rate isn't trusted until it has this many
# human-confirmed verdicts behind it -- avoids demoting a warning after
# just one or two coincidental "false alarm" verdicts.
MIN_SAMPLES = 5
FALSE_ALARM_THRESHOLD = 0.7


def load_feedback_log(path=FEEDBACK_LOG_PATH):
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        entries = yaml.safe_load(f)
    return entries or []


def compute_flag_track_record(feedback_log):
    """Returns (specific, general):
        specific: {(flag_type, fee_type): {"total": n, "false_alarms": n, "rate": x}}
        general:  {flag_type: {...}} -- coarser fallback for combos that
                   don't have enough fee_type-specific samples on their own.
    """
    specific_counts = defaultdict(lambda: {"total": 0, "false_alarms": 0})
    general_counts = defaultdict(lambda: {"total": 0, "false_alarms": 0})

    for entry in feedback_log:
        flag_type = entry.get("flag_type")
        if not flag_type:
            continue
        is_false_alarm = entry.get("verdict") == "false_alarm"

        general_counts[flag_type]["total"] += 1
        if is_false_alarm:
            general_counts[flag_type]["false_alarms"] += 1

        fee_type = entry.get("fee_type")
        if fee_type:
            key = (flag_type, fee_type)
            specific_counts[key]["total"] += 1
            if is_false_alarm:
                specific_counts[key]["false_alarms"] += 1

    def _finalize(counts):
        out = {}
        for key, c in counts.items():
            out[key] = {**c, "rate": c["false_alarms"] / c["total"] if c["total"] else 0.0}
        return out

    return _finalize(specific_counts), _finalize(general_counts)


def flag_severity(flag_type, fee_type, specific_record, general_record):
    """Returns (severity, stats).

    severity is "demoted" when there's enough confirmed history (>= MIN_SAMPLES)
    of this flag type being a false alarm to lower its prominence, else
    "normal". Prefers the fee_type-specific track record over the coarser
    flag_type-only one whenever the specific one has enough samples to trust,
    since the same flag type can be reliable for one fee and noisy for
    another (e.g. table conflicts on annual_fee vs. on balance_transfer_apr).
    """
    stats = specific_record.get((flag_type, fee_type))
    if not stats or stats["total"] < MIN_SAMPLES:
        stats = general_record.get(flag_type)
    if not stats or stats["total"] < MIN_SAMPLES:
        return "normal", stats

    if stats["rate"] >= FALSE_ALARM_THRESHOLD:
        return "demoted", stats
    return "normal", stats


def promote_confirmed_synonyms(feedback_log):
    """Adds any `synonym: {phrase, fee_type}` block from a confirmed
    real_error verdict into the shared taxonomy, so the phrase that caused
    the miss is recognized at every institution starting with the very next
    scrape (call this before scraping, not just before rendering).
    """
    from scraper import fee_taxonomy

    promoted = []
    for entry in feedback_log:
        if entry.get("verdict") != "real_error":
            continue
        synonym = entry.get("synonym")
        if not synonym or not synonym.get("phrase") or not synonym.get("fee_type"):
            continue
        _, added = fee_taxonomy.add_learned_synonym(synonym["fee_type"], synonym["phrase"])
        if added:
            promoted.append(synonym)
    return promoted
