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
]

EMPTY_VALUES = {None, "", "Not disclosed"}


def render_report(results, primary_institution, subject):
    """Renders the comparison email body.

    results: {inst_key: {"name": str, "cards": [...], "warnings": [...]}}

    The table is pivoted so each row is a fee category (not a card/product) and
    each column is a product, so equivalent fees from different institutions
    land on the same row regardless of product type.
    """
    ordered_keys = sorted(
        results.keys(),
        key=lambda k: results[k]["name"] != primary_institution,
    )

    columns = []
    all_warnings = []
    for inst_key in ordered_keys:
        inst = results[inst_key]
        is_primary = inst["name"] == primary_institution
        for card in inst["cards"]:
            columns.append({
                "institution": inst["name"],
                "product_name": card.get("card_name", "Unknown Product"),
                "is_primary": is_primary,
                "data": card,
            })
        all_warnings.extend(inst.get("warnings", []))

    rows = []
    for key, label in FIELD_LABELS:
        values = [col["data"].get(key) for col in columns]
        institutions_with_value = {
            col["institution"] for col, v in zip(columns, values) if v not in EMPTY_VALUES
        }
        if not institutions_with_value:
            continue
        rows.append({
            "label": label,
            "cells": values,
            # "Shared" means this fee shows up for more than one institution,
            # not just more than one product from the same institution.
            "shared": len(institutions_with_value) >= 2,
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
