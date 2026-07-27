"""Smoke test for the Century Bank + SECU NM per-product expansion.

Compares a fresh scrape against the values observed when each scraper was
built (2026-07-27). A mismatch is NOT necessarily a bug -- a bank can
change its own fees -- so a differing value is reported as **drift** to
verify, not a failure. Only a missing/unparseable field (the scraper found
nothing at all where a value was expected) is treated as a likely scraper
bug, since that's a parsing regression, not a legitimate fee change.

Run directly: `python smoke_test_new_institutions.py`
"""

import sys

import yaml

from scraper.labeled_features_scraper import LabeledFeaturesScraper
from scraper.multi_card_page_scraper import MultiCardPageScraper
from scraper.shared_credit_card_scraper import SharedCreditCardDisclosureScraper
from scraper.asserted_fee_scraper import AssertedFeeScraper
from scraper.tis_table_scraper import TisTableScraper

# (institution_key, card_name, field, expected_value) as observed 2026-07-27.
EXPECTATIONS = [
    ("century_bank_checking", "Century Checking", "monthly_maintenance_fee", "None"),
    ("century_bank_interest_checking", "Century Interest Checking", "monthly_maintenance_fee",
     "$5/month, waived with a $500 minimum daily balance"),
    ("century_bank_platinum_checking", "Century Platinum Checking", "monthly_maintenance_fee",
     "$25/month, waived with a $10,000 minimum daily balance"),
    ("century_bank_savings_cds_retirement", "High-Performance Savings", "monthly_maintenance_fee",
     "$2/month, waived with a $100 minimum daily balance"),
    ("century_bank_savings_cds_retirement", "Minor Savings Account", "monthly_maintenance_fee",
     "$2/month, waived with a $25 minimum daily balance"),
    ("century_bank_savings_cds_retirement", "High Performance MMDA", "monthly_maintenance_fee",
     "$12/month, waived with a $5,000 minimum daily balance"),
    ("century_bank_savings_cds_retirement", "Liberty Money Market", "monthly_maintenance_fee",
     "$15/month, waived with a $15,000 minimum daily balance"),
    ("century_bank_savings_cds_retirement", "Personal Certificate of Deposit", "monthly_maintenance_fee", "None"),
    ("century_bank_savings_cds_retirement", "Market Rate IRA", "monthly_maintenance_fee",
     "$2/month, waived with a $100 minimum daily balance"),
    ("century_bank_savings_cds_retirement", "Fixed Term IRA CD", "monthly_maintenance_fee",
     "$2/month, waived with a $100 minimum daily balance"),
    ("secu_nm_credit_cards", "Visa Platinum", "purchase_apr", "7.50%"),
    ("secu_nm_credit_cards", "Visa Gold", "purchase_apr", "9.25%"),
    ("secu_nm_credit_cards", "Visa Classic", "purchase_apr", "12.50%"),
    ("secu_nm_credit_cards", "Visa Platinum", "annual_fee", "None"),
    ("secu_nm_credit_cards", "Visa Platinum", "cash_advance_fee", "3% of advance or $25.00 max."),
    ("secu_nm_kasasa_cash", "Kasasa Cash", "monthly_maintenance_fee", "None"),
    ("secu_nm_kasasa_cash_back", "Kasasa Cash Back", "monthly_maintenance_fee", "None"),
    ("secu_nm_kasasa_tunes", "Kasasa Tunes", "monthly_maintenance_fee", "None"),
    ("secu_nm_regular_checking", "Regular Checking", "monthly_maintenance_fee", "None"),
    ("secu_nm_tis_savings", "Share Savings", "monthly_maintenance_fee", "None"),
    ("secu_nm_tis_savings", "IRA Savings (Roth/Traditional)", "monthly_maintenance_fee", "None"),
]

SCRAPER_TYPES = {
    "labeled_features": LabeledFeaturesScraper,
    "multi_card_page": MultiCardPageScraper,
    "shared_credit_card_disclosure": SharedCreditCardDisclosureScraper,
    "asserted_fee": AssertedFeeScraper,
    "tis_table": TisTableScraper,
}


def main():
    with open("config.yaml", "r") as f:
        config = yaml.safe_load(f)

    scraped_by_institution = {}
    drift = []
    missing = []

    for inst_key, card_name, field, expected in EXPECTATIONS:
        if inst_key not in scraped_by_institution:
            cfg = config["institutions"][inst_key]
            scraper_cls = SCRAPER_TYPES[cfg["type"]]
            scraper = scraper_cls(name=cfg["name"], url=cfg.get("url", ""), config=cfg)
            scraped_by_institution[inst_key] = {c["card_name"]: c for c in scraper.scrape()}

        card = scraped_by_institution[inst_key].get(card_name)
        actual = card.get(field) if card else None

        if actual is None:
            missing.append((inst_key, card_name, field, expected))
        elif actual != expected:
            drift.append((inst_key, card_name, field, expected, actual))

    print(f"Checked {len(EXPECTATIONS)} expectations across {len(scraped_by_institution)} scrapers.\n")

    if missing:
        print(f"LIKELY SCRAPER BUG -- {len(missing)} field(s) not found at all (expected a value):")
        for inst_key, card_name, field, expected in missing:
            print(f"  [{inst_key} / {card_name}] {field}: expected {expected!r}, got nothing")
        print()

    if drift:
        print(f"DRIFT -- {len(drift)} field(s) changed since 2026-07-27 (verify before assuming a bug):")
        for inst_key, card_name, field, expected, actual in drift:
            print(f"  [{inst_key} / {card_name}] {field}: was {expected!r}, now {actual!r}")
        print()

    if not missing and not drift:
        print("All expectations matched. No drift, no missing fields.")

    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
