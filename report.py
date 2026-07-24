from collections import OrderedDict
from datetime import datetime, timezone
from jinja2 import Environment, FileSystemLoader

from feedback import flag_severity
from scraper.categories import CATEGORIES

# Canonical fee categories shown in the report, in display order. Institutions
# don't need to report every key -- rows with no data anywhere are dropped
# automatically.
FIELD_LABELS = [
    ("annual_fee", "Annual Fee"),
    ("cash_advance_fee", "Cash Advance Fee"),
    ("balance_transfer_fee", "Balance Transfer Fee"),
    ("foreign_transaction_fee", "Foreign Transaction Fee"),
    ("late_payment_fee", "Late Payment Fee"),
    ("returned_item_fee", "Returned Item Fee"),
    ("intro_offers", "Intro Offers"),
    ("rewards_structure", "Rewards Structure"),
    ("overdraft_fee", "Overdraft Fee"),
    ("returned_deposit_item_fee", "Returned Deposit Item Fee"),
    ("stop_payment_fee", "Stop Payment Fee"),
    ("dormant_account_fee", "Dormant Account Fee"),
    ("currency_exchange_fee", "Currency Exchange Fee"),
    ("account_research_fee", "Account Research Fee"),
    ("atm_out_of_network_fee", "Out-of-Network ATM Fee"),
    ("wire_transfer_incoming_fee", "Incoming Wire Transfer Fee"),
    ("wire_transfer_outgoing_domestic_fee", "Outgoing Domestic Wire Transfer Fee"),
    ("wire_transfer_outgoing_international_fee", "Outgoing International Wire Transfer Fee"),
    ("cashiers_check_fee", "Cashier's / Official Check Fee"),
    ("check_cashing_non_member_fee", "Non-Customer Check Cashing Fee"),
    ("safe_deposit_late_payment_fee", "Safe Deposit Box Late Payment Fee"),
    ("safe_deposit_drill_fee", "Safe Deposit Box Drilling Fee"),
    ("card_replacement_fee", "Card Replacement Fee"),
    ("card_replacement_rush_fee", "Rush Card Replacement Fee"),
    ("monthly_maintenance_fee", "Monthly Maintenance Fee"),
]
FIELD_LABEL_MAP = dict(FIELD_LABELS)
FIELD_ORDER = [key for key, _ in FIELD_LABELS]


def _fee_label(fee_key):
    return FIELD_LABEL_MAP.get(fee_key, fee_key.replace("_", " ").title())


def render_report(results, primary_institution, subject, facts_by_institution, track_records):
    """Renders the comparison email body.

    results: {inst_key: {"name": str, "cards": [...], "warnings": [...]}}
    facts_by_institution: from attribution.build_fee_facts, already
        annotated by drift.mark_drift with changed_since_last_snapshot.
    track_records: (specific, general) from feedback.compute_flag_track_record
        -- confirmed human verdict history used to calibrate how loudly a
        flag type is shown (see feedback.flag_severity).

    Fees are grouped into one section per product category (see
    scraper/categories.py) instead of one column per institution, so a
    credit card fee is never lined up against an unrelated deposit account
    fee just because they share a field name. Each section states which
    institutions actually have a product in that category -- a category
    backed by only one institution is rendered but marked "not comparable"
    rather than silently presented as a cross-institution comparison. Every
    cell also carries the scope its value was actually verified for (a
    single product name, or "verified across ..." -- see attribution.py),
    never assuming a fee applies more broadly than what was scraped.
    """
    specific_record, general_record = track_records

    all_warnings = []
    for inst in results.values():
        all_warnings.extend(inst.get("warnings", []))

    institution_order = list(facts_by_institution.keys())
    institution_order.sort(key=lambda name: name != primary_institution)

    category_institution_facts = OrderedDict((cat_key, OrderedDict()) for cat_key in CATEGORIES)
    for inst_name in institution_order:
        for fact in facts_by_institution.get(inst_name, []):
            category_institution_facts[fact["category"]].setdefault(inst_name, []).append(fact)

    sections = []
    for cat_key, per_institution in category_institution_facts.items():
        if not per_institution:
            continue

        institutions_present = [name for name in institution_order if name in per_institution]
        comparable = len(institutions_present) >= 2
        label = CATEGORIES[cat_key]

        if comparable:
            basis_note = (
                f"Comparison basis: products classified as \"{label}\" "
                f"across {len(institutions_present)} institutions."
            )
        else:
            basis_note = (
                f"Only {institutions_present[0]} has a product classified as \"{label}\" -- "
                "no comparable product found at other institutions, so this is shown for "
                "reference only rather than as a cross-institution comparison."
            )

        known_keys_present = [k for k in FIELD_ORDER if any(
            any(f["fee_type"] == k for f in facts) for facts in per_institution.values()
        )]
        extra_keys = sorted({
            f["fee_type"]
            for facts in per_institution.values()
            for f in facts
            if f["fee_type"] not in FIELD_LABEL_MAP
        })
        fee_types_present = known_keys_present + extra_keys

        rows = []
        low_confidence_values = 0
        total_values = 0
        for fee_key in fee_types_present:
            cells = []
            present_count = 0
            row_mechanisms = set()
            for inst_name in institutions_present:
                matches = [f for f in per_institution.get(inst_name, []) if f["fee_type"] == fee_key]
                if matches:
                    present_count += 1
                entries = []
                for f in matches:
                    total_values += 1

                    confidence_severity = "normal"
                    confidence_stats = None
                    if f["confidence"] == "low":
                        low_confidence_values += 1
                        confidence_severity, confidence_stats = flag_severity(
                            "low_confidence", fee_key, specific_record, general_record
                        )

                    drift_severity = "normal"
                    drift_stats = None
                    if f.get("changed_since_last_snapshot"):
                        drift_severity, drift_stats = flag_severity(
                            "drift", fee_key, specific_record, general_record
                        )

                    if f["mechanism"] not in (None, "unknown"):
                        row_mechanisms.add(f["mechanism"])

                    entries.append({
                        "value": f["value"],
                        "scope": f["scope"],
                        "confidence": f["confidence"],
                        "confidence_severity": confidence_severity,
                        "confidence_stats": confidence_stats,
                        "mechanism_label": f["mechanism_label"],
                        "changed": f.get("changed_since_last_snapshot", False),
                        "previous_value": f.get("previous_value"),
                        "drift_severity": drift_severity,
                        "drift_stats": drift_stats,
                    })
                cells.append(entries)

            mismatch = present_count >= 2 and len(row_mechanisms) > 1
            mismatch_severity, mismatch_stats = (
                flag_severity("mechanism_mismatch", fee_key, specific_record, general_record)
                if mismatch else ("normal", None)
            )
            rows.append({
                "label": _fee_label(fee_key),
                "cells": cells,
                "shared": present_count >= 2,
                # A shared row where the values use different fee mechanisms
                # (e.g. flat $ vs. percentage, or one-time vs. recurring)
                # isn't a true apples-to-apples number even though both
                # sides share a fee_type -- flag it instead of implying the
                # two figures are directly comparable.
                "mechanism_mismatch": mismatch,
                "mismatch_severity": mismatch_severity,
                "mismatch_stats": mismatch_stats,
            })

        if low_confidence_values:
            basis_note += (
                f" {low_confidence_values} of {total_values} value(s) in this table are "
                "low-confidence extractions (conflicting matches on the source page) -- verify manually."
            )

        sections.append({
            "label": label,
            "institutions": institutions_present,
            "comparable": comparable,
            "basis_note": basis_note,
            "rows": rows,
        })

    env = Environment(loader=FileSystemLoader("templates"))
    template = env.get_template("report_email.html.j2")

    return template.render(
        subject=subject,
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        primary_institution=primary_institution,
        sections=sections,
        warnings=all_warnings,
    )
