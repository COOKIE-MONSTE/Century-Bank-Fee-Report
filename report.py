from datetime import datetime, timezone
from jinja2 import Environment, FileSystemLoader

FIELD_LABELS = [
    ("annual_fee", "Annual Fee"),
    ("purchase_apr", "Purchase APR"),
    ("balance_transfer_apr", "Balance Transfer APR"),
    ("balance_transfer_fee", "Balance Transfer Fee"),
    ("cash_advance_apr", "Cash Advance APR"),
    ("cash_advance_fee", "Cash Advance Fee"),
    ("foreign_transaction_fee", "Foreign Transaction Fee"),
    ("late_payment_fee", "Late Payment Fee"),
    ("returned_payment_fee", "Returned Payment Fee"),
    ("intro_offers", "Intro Offers"),
    ("rewards_structure", "Rewards Structure"),
]


def render_report(results, primary_institution, subject):
    """Renders the comparison email body.

    results: {inst_key: {"name": str, "cards": [...], "warnings": [...]}}
    """
    rows = []
    all_warnings = []

    # Primary institution's cards first so they're easy to spot at the top too.
    ordered_keys = sorted(
        results.keys(),
        key=lambda k: results[k]["name"] != primary_institution,
    )

    for inst_key in ordered_keys:
        inst = results[inst_key]
        is_primary = inst["name"] == primary_institution
        for card in inst["cards"]:
            rows.append({
                "institution": inst["name"],
                "card_name": card.get("card_name", "Unknown Card"),
                "card": card,
                "is_primary": is_primary,
            })
        all_warnings.extend(inst.get("warnings", []))

    env = Environment(loader=FileSystemLoader("templates"))
    template = env.get_template("report_email.html.j2")

    return template.render(
        subject=subject,
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        primary_institution=primary_institution,
        fields=FIELD_LABELS,
        rows=rows,
        warnings=all_warnings,
    )
