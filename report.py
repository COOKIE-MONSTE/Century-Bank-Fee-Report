from datetime import datetime, timezone
from jinja2 import Environment, FileSystemLoader

# Canonical fee categories shown in the report, in display order. Institutions
# don't need to report every key -- rows with no data anywhere are dropped
# automatically. Rows with 2+ real values across products are the "similar
# fees across products" matches the report highlights.
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

EMPTY_VALUES = {None, "", "Not disclosed"}


def render_report(results, primary_institution, subject):
    """Renders the comparison email body.

    results: {inst_key: {"name": str, "cards": [...], "warnings": [...]}}

    One institution can be backed by multiple config entries/scrapers (e.g.
    Nusenda's credit cards + retail fee products) -- all of those are merged
    into a single column per institution here, so the table stays one column
    per bank as more institutions get added. Where a field has more than one
    distinct value across an institution's products (e.g. Rewards Structure
    differing by credit card), all distinct values are kept, joined together,
    instead of silently dropping any of them.

    The table is pivoted so each row is a fee category and each column an
    institution, so equivalent fees from different institutions land on the
    same row.
    """
    all_warnings = []
    institution_order = []
    institution_cards = {}
    for inst in results.values():
        name = inst["name"]
        if name not in institution_cards:
            institution_cards[name] = []
            institution_order.append(name)
        institution_cards[name].extend(inst["cards"])
        all_warnings.extend(inst.get("warnings", []))

    institution_order.sort(key=lambda name: name != primary_institution)

    columns = []
    for name in institution_order:
        merged = {}
        for key, _ in FIELD_LABELS:
            distinct_values = []
            for card in institution_cards[name]:
                value = card.get(key)
                if value not in EMPTY_VALUES and value not in distinct_values:
                    distinct_values.append(value)
            if distinct_values:
                merged[key] = "; ".join(distinct_values)
        columns.append({
            "institution": name,
            "is_primary": name == primary_institution,
            "data": merged,
        })

    rows = []
    for key, label in FIELD_LABELS:
        values = [col["data"].get(key) for col in columns]
        present_count = sum(1 for v in values if v not in EMPTY_VALUES)
        if present_count == 0:
            continue
        rows.append({
            "label": label,
            "cells": values,
            "shared": present_count >= 2,
        })

    env = Environment(loader=FileSystemLoader("templates"))
    template = env.get_template("report_email.html.j2")

    return template.render(
        subject=subject,
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        primary_institution=primary_institution,
        columns=columns,
        rows=rows,
        warnings=all_warnings,
    )
