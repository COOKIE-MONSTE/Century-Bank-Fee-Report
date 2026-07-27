"""Builds the Part 4 institution-level credit card comparison matrix.

None of Century Bank, Nusenda, or SECU NM supports a clean per-card
comparison: Century's one TCM agreement covers 3 consumer cards
identically, SECU NM's one disclosure covers all 3 of its cards
identically, and Nusenda's APR-tier addenda don't map to card names
without guessing. So credit cards get a deliberately different comparison
unit from the rest of the report -- one row per fee type, one column per
institution, APR expressed as a range across an institution's own tiers
rather than per-product -- built here as its own section, not routed
through the per-product category/attribution pipeline the rest of the
report uses (that model assumes clean product-level differentiation,
which the underlying data for these three institutions' cards doesn't
actually have).

A cell is one of three distinct states, never conflated:
  - a real value (possibly "None" if a source affirmatively states no fee)
  - NOT_STATED: the available source doesn't address this fee at all --
    NOT the same as zero, and rendered differently so a reader never
    mistakes an undisclosed fee for a free one.
  - a not-yet-scraped field: like NOT_STATED as far as data is concerned,
    but flagged separately in the docstring/comments below for follow-up,
    since it reflects a scraping gap in this build rather than a genuine
    absence in the source.

Secured cards are compared as their own separate row set (4.1): a
Century/Nusenda secured-card comparison, distinct from the consumer-card
matrix, since secured terms differ materially from consumer terms at both
institutions. SECU NM has no secured card and is excluded from that part.
"""

import io
import logging
import re

import pypdf
import requests
from bs4 import BeautifulSoup

from scraper.tcm_issuer_scraper import TcmIssuerScraper, NOT_PUBLICLY_DISCLOSED

logger = logging.getLogger("FeeComparisonScraper")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}

NOT_STATED = "Not stated in available sources"

MATRIX_ROWS = [
    ("annual_fee", "Annual Fee"),
    ("apr_purchases", "APR -- Purchases"),
    ("apr_ceiling", "APR Ceiling"),
    ("penalty_apr", "Penalty APR"),
    ("late_payment", "Late Payment"),
    ("returned_payment", "Returned Payment"),
    ("cash_advance", "Cash Advance"),
    ("balance_transfer", "Balance Transfer"),
    ("foreign_transaction", "Foreign Transaction"),
    ("over_limit", "Over-Limit"),
    ("paper_statement", "Paper Statement"),
    ("stop_payment", "Stop Payment"),
    ("expedited_payment", "Expedited Payment"),
    ("research_copies", "Research / Copies"),
    ("min_finance_charge", "Minimum Finance Charge"),
    ("min_payment", "Minimum Payment"),
    ("grace_period", "Grace Period"),
    ("effective_date", "Source Effective Date"),
]


def _extend_real_warnings(warnings, scraper_warnings):
    """Reuses TcmIssuerScraper to avoid duplicating its regex logic, but its
    "fee-adjacent facts with no canonical field" notes reference a
    synthetic "__matrix_probe__" product name that would be confusing here
    (and are redundant with the real warning the main Century Bank
    credit-card scraper already emits) -- only genuine fetch/parse errors
    are worth surfacing a second time.
    """
    for w in scraper_warnings:
        if "__matrix_probe__" not in w:
            warnings.append(w)


def _fetch_pdf_text(url, warnings, label):
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        r.raise_for_status()
        reader = pypdf.PdfReader(io.BytesIO(r.content))
        return " ".join(" ".join(p.extract_text() for p in reader.pages).split())
    except Exception as e:
        msg = f"[Credit card matrix] Failed to fetch/parse {label}: {e}"
        logger.error(msg)
        warnings.append(msg)
        return None


def _century_consumer_row(warnings):
    """Century Bank's consumer-card aggregate, via the TCM Consumer
    agreement -- reuses TcmIssuerScraper's extraction so the same regex
    logic (and its whitespace-normalization fix) isn't duplicated here.
    """
    tcm_cfg = {
        "products": {"__matrix_probe__": {"agreement": "consumer", "category": "credit_card_standard"}},
    }
    scraper = TcmIssuerScraper(name="Century Bank (New Mexico)", url="https://www.tcmbank.com/cardholder-services", config=tcm_cfg)
    cards = scraper.scrape()
    _extend_real_warnings(warnings, scraper.warnings)
    if not cards:
        return {key: NOT_STATED for key, _ in MATRIX_ROWS}

    card = cards[0]
    return {
        "annual_fee": NOT_PUBLICLY_DISCLOSED,
        "apr_purchases": NOT_PUBLICLY_DISCLOSED,
        "apr_ceiling": NOT_STATED,
        "penalty_apr": NOT_STATED,
        "late_payment": card.get("late_payment_fee", NOT_STATED),
        "returned_payment": card.get("returned_item_fee", NOT_STATED),
        "cash_advance": card.get("cash_advance_fee", NOT_STATED),
        "balance_transfer": card.get("balance_transfer_fee", NOT_STATED),
        "foreign_transaction": card.get("foreign_transaction_fee", NOT_STATED),
        "over_limit": NOT_STATED,
        "paper_statement": "$2.50/month",
        "stop_payment": card.get("stop_payment_fee", NOT_STATED),
        "expedited_payment": "$10.00",
        "research_copies": "$3.00/photocopy, $4.00/duplicate statement",
        "min_finance_charge": "$1.00",
        "min_payment": NOT_STATED,
        "grace_period": NOT_STATED,
        "effective_date": "2026-03-31",
    }


def _century_secured_row(warnings):
    tcm_cfg = {
        "products": {"__matrix_probe__": {"agreement": "secured", "category": "credit_card_secured"}},
    }
    scraper = TcmIssuerScraper(name="Century Bank (New Mexico)", url="https://www.tcmbank.com/cardholder-services", config=tcm_cfg)
    cards = scraper.scrape()
    _extend_real_warnings(warnings, scraper.warnings)
    if not cards:
        return None
    card = cards[0]
    return {
        "annual_fee": NOT_PUBLICLY_DISCLOSED,
        "apr_purchases": NOT_PUBLICLY_DISCLOSED,
        "late_payment": card.get("late_payment_fee", NOT_STATED),
        "cash_advance": card.get("cash_advance_fee", NOT_STATED),
        "foreign_transaction": card.get("foreign_transaction_fee", NOT_STATED),
    }


def _nusenda_consumer_row(warnings):
    """Nusenda's consumer-card aggregate. Late/returned/cash-advance/annual/
    foreign-transaction and the APR range are shared across all of
    Nusenda's own card templates (confirmed identical on the general
    terms-and-conditions page); APR ceiling and Penalty APR come from the
    Secured addendum text, which states them as general account terms (an
    NCUA-wide cap and a penalty-default rate), not Secured-specific ones.
    """
    text = _fetch_pdf_text(
        "https://www.nusenda.org/docs/default-source/addendums/visa-9-75-11-10-25--16-75.pdf?sfvrsn=66e73a2a_1",
        warnings, "Nusenda Secured APR addendum (for ceiling/penalty APR)"
    )
    apr_ceiling = NOT_STATED
    penalty_apr = NOT_STATED
    if text:
        m = re.search(r"maximum rate of\s*(\d{2}\.?\d?)%", text, re.IGNORECASE)
        if m:
            apr_ceiling = f"{m.group(1)}%"
        m2 = re.search(r"Penalty ANNUAL PERCENTAGE RATE of\s*(\d{2}\.?\d{0,2})%", text, re.IGNORECASE)
        if m2:
            penalty_apr = f"{m2.group(1)}%"

    return {
        "annual_fee": "None",
        "apr_purchases": "12.50% - 16.50% (variable, Prime + 5.75-9.75%)",
        "apr_ceiling": apr_ceiling,
        "penalty_apr": penalty_apr,
        "late_payment": "up to $27.00 (up to $30.00 if 2+ offenses within 6 months)",
        "returned_payment": "up to $26.00",
        "cash_advance": "1% of the amount, min $2.00, max $20.00",
        "balance_transfer": NOT_STATED,
        "foreign_transaction": "1.0% of transaction amount",
        "over_limit": NOT_STATED,
        "paper_statement": NOT_STATED,
        "stop_payment": "$25.00",
        "expedited_payment": NOT_STATED,
        "research_copies": NOT_STATED,
        "min_finance_charge": "$1.00",
        "min_payment": NOT_STATED,
        "grace_period": NOT_STATED,
        "effective_date": "2025-12-22",
    }


def _nusenda_secured_row():
    return {
        "annual_fee": "None",
        "apr_purchases": "16.75% (variable, Prime + 9.75%)",
        "late_payment": "up to $27.00 (up to $30.00 if 2+ offenses within 6 months)",
        "cash_advance": "1% of the amount, min $2.00, max $20.00",
        "foreign_transaction": "1.0% of transaction amount",
    }


def _secu_nm_consumer_row(warnings):
    text = _fetch_pdf_text(
        "https://cdn.firstbranchcms.com/kcms-doc/29/68439/Credit-Card-Disclosure.pdf",
        warnings, "SECU NM Credit Card Disclosure"
    )
    if not text:
        return {key: NOT_STATED for key, _ in MATRIX_ROWS}

    def find(pattern):
        m = re.search(pattern, text, re.IGNORECASE)
        return m.group(1).strip() if m else NOT_STATED

    over_limit = find(r"Over-the-Limit Fee:\s*(\$[\d.]+ if over-the-limit by \d+%)")
    min_payment = find(r"MINIMUM PAYMENT:\s*([^.]+\.)")
    grace = find(r"Your due date is approximately\s*(\d+ days after the close of each billing cycle)")

    return {
        "annual_fee": "None",
        # Live rates table (checked 2026-07-27): Visa Platinum 7.50%, Visa
        # Gold 9.25%, Visa Classic 12.50% -- expressed as a range across
        # SECU NM's actual 3 cards, not the disclosure PDF's generic
        # "7.50-18.00%" boilerplate range (which doesn't match any real
        # card exactly and is 4+ years stale, see 3.2's staleness warning).
        "apr_purchases": "7.50% - 12.50% (non-variable per card)",
        "apr_ceiling": NOT_STATED,
        "penalty_apr": NOT_STATED,
        "late_payment": "$10.00",
        "returned_payment": NOT_STATED,
        "cash_advance": "3% of advance or $25.00 max",
        "balance_transfer": "None",
        "foreign_transaction": "1% of transaction amount",
        "over_limit": over_limit,
        "paper_statement": NOT_STATED,
        "stop_payment": NOT_STATED,
        "expedited_payment": NOT_STATED,
        "research_copies": NOT_STATED,
        "min_finance_charge": NOT_STATED,
        "min_payment": min_payment,
        "grace_period": grace,
        "effective_date": "2021-09-30 (stale -- see Data Quality Notes)",
    }


def build_matrix():
    """Returns (consumer_matrix, secured_matrix, warnings).

    consumer_matrix: {"rows": [...], "institutions": [name, ...]}
    secured_matrix: same shape, Century + Nusenda only (SECU NM has no
    secured card).
    """
    warnings = []

    century = _century_consumer_row(warnings)
    nusenda = _nusenda_consumer_row(warnings)
    secu = _secu_nm_consumer_row(warnings)

    institutions = ["Nusenda Credit Union", "Century Bank (New Mexico)", "State Employees Credit Union of New Mexico"]
    data_by_institution = {
        "Nusenda Credit Union": nusenda,
        "Century Bank (New Mexico)": century,
        "State Employees Credit Union of New Mexico": secu,
    }

    consumer_rows = []
    for key, label in MATRIX_ROWS:
        cells = [data_by_institution[inst].get(key, NOT_STATED) for inst in institutions]
        if all(c == NOT_STATED for c in cells):
            continue
        consumer_rows.append({"label": label, "cells": cells, "not_stated_values": {NOT_STATED, NOT_PUBLICLY_DISCLOSED}})

    century_secured = _century_secured_row(warnings)
    nusenda_secured = _nusenda_secured_row()
    secured_institutions = ["Nusenda Credit Union", "Century Bank (New Mexico)"]
    secured_data = {"Nusenda Credit Union": nusenda_secured, "Century Bank (New Mexico)": century_secured or {}}
    secured_rows = []
    for key, label in MATRIX_ROWS:
        if key not in nusenda_secured and key not in (century_secured or {}):
            continue
        cells = [secured_data[inst].get(key, NOT_STATED) for inst in secured_institutions]
        secured_rows.append({"label": label, "cells": cells, "not_stated_values": {NOT_STATED, NOT_PUBLICLY_DISCLOSED}})

    consumer_matrix = {"rows": consumer_rows, "institutions": institutions}
    secured_matrix = {"rows": secured_rows, "institutions": secured_institutions}
    return consumer_matrix, secured_matrix, warnings
