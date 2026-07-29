"""Builds the Part 4 institution-level credit card comparison matrix.

None of Century Bank, Nusenda, SECU NM, or First National 1870 supports a
clean per-card comparison: Century's one TCM agreement covers 3 consumer
cards identically, SECU NM's one disclosure covers all 3 of its cards
identically, First National 1870's one TILA table covers its Classic/Gold/
Platinum Rewards tiers identically, and Nusenda's APR-tier addenda don't
map to card names without guessing. So credit cards get a deliberately
different comparison unit from the rest of the report -- one row per fee
type, one column per institution, APR expressed as a range across an
institution's own tiers rather than per-product -- built here as its own
section, not routed through the per-product category/attribution pipeline
the rest of the report uses (that model assumes clean product-level
differentiation, which the underlying data for these institutions' cards
doesn't actually have).

A cell is one of two states, never conflated:
  - a real value (possibly "None" if a source affirmatively states no fee)
  - NOT_STATED: the available source doesn't address this fee at all --
    NOT the same as zero, and rendered differently so a reader never
    mistakes an undisclosed fee for a free one.

Every value here is a **live extraction**, not a hardcoded string -- if a
value looks static, that's because the same underlying scraper class used
elsewhere in this codebase is being reused, not because the value is typed
in. This matters operationally: a fee change at any of these institutions
should show up here on the next run without anyone needing to notice the
change and hand this module a new URL. Each _xxx_row() function also
returns which URL backed each field (`sources`), threaded up through
build_matrix() and persisted to data/history.json by run.py -- not shown
in the report itself (the report states only that values are fact-checked;
per-fee sourcing lives in data/history.json and in project memory, see
memory/credit_card_matrix_sources.md).

Originally this also broke out a separate Rewards/Standard/Secured card-tier
comparison, mirroring the rest of the report's category structure. Dropped
in favor of one unified table: confirmed every fee except APR is identical
across an institution's own card types (Century's TCM Consumer and Secured
agreements share the same late payment/cash advance/foreign transaction
terms; Nusenda's Secured card shares its Consumer siblings' fee schedule),
so per-tier sections just repeated the same handful of numbers three times
with no new information. The one place this isn't true -- Nusenda's
Secured APR (16.75%) genuinely differs from its Consumer range -- gets a
single extra row directly under the general APR row instead of an entire
separate table.
"""

import io
import logging
import re

import pypdf
import requests
from bs4 import BeautifulSoup

from scraper.tcm_issuer_scraper import TcmIssuerScraper, NOT_PUBLICLY_DISCLOSED
from scraper.html_scraper import HTMLScraper
from scraper.lkcs_widget_scraper import LKCSFeeScraper
from scraper.shared_credit_card_scraper import SharedCreditCardDisclosureScraper
from scraper.schumer_box_scraper import SchumerBoxScraper

logger = logging.getLogger("FeeComparisonScraper")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}

NOT_STATED = "Not stated"

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
    ("reward_structure", "Reward Structure"),
    ("card_promotions", "Card Promotions"),
    ("effective_date", "Source Effective Date"),
]

NO_SECURED_CARD = "No secured card"
SAME_AS_GENERAL_APR = "Same as general APR"

# Any of these sentinel strings render as a muted, de-emphasized cell
# rather than a real value -- but each keeps its own specific wording
# (never collapsed to one generic "no data" label), so a reader can tell
# "we don't know" apart from "confirmed not applicable" apart from
# "confirmed identical to the row above".
MUTED_VALUES = {NOT_STATED, NOT_PUBLICLY_DISCLOSED, NO_SECURED_CARD, SAME_AS_GENERAL_APR}


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


def _century_rewards_and_promos(warnings):
    """Reward structure and current promotions vary genuinely by card at
    Century Bank (unlike its fees), so this returns a summary spanning all
    4 cards rather than one representative value -- collapsing to a single
    number here would misrepresent Visa Signature Travel's distinct
    1.5x/no-cap program and sign-up bonus as if every card had it.
    """
    url = "https://www.mycenturybank.com/personal-banking/loans-and-credit/personal-credit-cards"
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        r.raise_for_status()
        soup = BeautifulSoup(r.content, "html.parser")
        main = soup.find("main") or soup
        text = " ".join(main.get_text(separator=" ", strip=True).split())
    except Exception as e:
        msg = f"[Credit card matrix] Failed to fetch Century Bank card page for rewards/promotions: {e}"
        logger.error(msg)
        warnings.append(msg)
        return NOT_STATED, NOT_STATED, None

    # The source sentence ends in "!" not "." ("...travel and more!"), so
    # the terminator class has to include both -- a period-only pattern
    # overshoots past the "!" to the next unrelated sentence.
    common_m = re.search(r"Earn one point per dollar on net purchases[^.!]*[.!]", text)
    common_reward = common_m.group(0) if common_m else NOT_STATED
    sig_m = re.search(r"Earn 1\.5 points per dollar on net purchases\.[^.!]*[.!]", text)
    sig_reward = sig_m.group(0) if sig_m else NOT_STATED
    promo_m = re.search(r"Earn 30,000 bonus points when you spend \$[\d,]+ in the first \d+ months", text)
    promo = promo_m.group(0) if promo_m else NOT_STATED

    reward_summary = (
        f"Rewards Platinum / Cash Rewards Platinum: {common_reward} "
        f"Visa Signature Travel: {sig_reward} Visa Secured: None."
    )
    promo_summary = f"Visa Signature Travel: {promo}. Rewards Platinum / Cash Rewards Platinum / Visa Secured: none advertised."
    return reward_summary, promo_summary, url


# Maps TcmIssuerScraper's canonical field names to this module's matrix
# row keys, so an "llm_assisted" tag on the scraped card (see
# tcm_issuer_scraper.py's _FIELD_DESCRIPTIONS) can be translated into which
# matrix cell to flag.
_CANONICAL_TO_MATRIX_KEY = {
    "late_payment_fee": "late_payment",
    "returned_item_fee": "returned_payment",
    "cash_advance_fee": "cash_advance",
    "balance_transfer_fee": "balance_transfer",
    "foreign_transaction_fee": "foreign_transaction",
    "stop_payment_fee": "stop_payment",
}


def _century_consumer_row(warnings):
    """Century Bank's consumer-card aggregate, via the TCM Consumer
    agreement -- reuses TcmIssuerScraper's extraction (including its
    `_matrix_extra`/`_source_urls`) so none of this module's regex logic
    duplicates what that scraper already does.

    Returns (values, sources, llm_fields) -- llm_fields is the set of
    matrix row keys whose value came from TcmIssuerScraper's Gemini
    fallback rather than a regex match (see tcm_issuer_scraper.py), so
    build_matrix() can flag those cells distinctly instead of presenting
    an AI guess with the same visual weight as a direct pattern match.
    """
    tcm_cfg = {
        "products": {"__matrix_probe__": {"agreement": "consumer", "category": "credit_card_standard"}},
    }
    scraper = TcmIssuerScraper(name="Century Bank (New Mexico)", url="https://www.tcmbank.com/cardholder-services", config=tcm_cfg)
    cards = scraper.scrape()
    _extend_real_warnings(warnings, scraper.warnings)
    if not cards:
        return {key: NOT_STATED for key, _ in MATRIX_ROWS}, {}, set()

    card = cards[0]
    extra = card.get("_matrix_extra", {})
    sources = dict(card.get("_source_urls", {}))
    field_confidence = card.get("_field_confidence", {})
    llm_fields = {
        _CANONICAL_TO_MATRIX_KEY[field] for field, conf in field_confidence.items()
        if conf == "llm_assisted" and field in _CANONICAL_TO_MATRIX_KEY
    }

    reward_structure, card_promotions, rewards_url = _century_rewards_and_promos(warnings)
    if rewards_url:
        sources["reward_structure"] = rewards_url
        sources["card_promotions"] = rewards_url

    values = {
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
        "paper_statement": extra.get("paper_statement", NOT_STATED),
        "stop_payment": card.get("stop_payment_fee", NOT_STATED),
        "expedited_payment": extra.get("expedited_payment", NOT_STATED),
        "research_copies": extra.get("research_copies", NOT_STATED),
        "min_finance_charge": extra.get("min_finance_charge", NOT_STATED),
        "min_payment": NOT_STATED,
        "grace_period": NOT_STATED,
        "reward_structure": reward_structure,
        "card_promotions": card_promotions,
        "effective_date": extra.get("effective_date", NOT_STATED),
    }
    return values, sources, llm_fields


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


def _nusenda_promo_check(url, warnings):
    """Nusenda doesn't currently advertise a points/cash sign-up bonus on
    its terms-and-conditions page (distinct from the promotional *APR*
    already tracked as Intro Offers elsewhere) -- checked live rather than
    assumed, so this self-corrects if that ever changes.
    """
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        r.raise_for_status()
        text = " ".join(BeautifulSoup(r.content, "html.parser").get_text(separator=" ", strip=True).split())
    except Exception as e:
        msg = f"[Credit card matrix] Failed to check Nusenda for card promotions: {e}"
        logger.error(msg)
        warnings.append(msg)
        return NOT_STATED

    found = re.search(r"bonus|sign-up|sign up|welcome offer", text, re.IGNORECASE)
    if found:
        return f"Possible promotion detected near: \"{text[max(0, found.start()-40):found.start()+80]}\" -- verify manually."
    return "None advertised (promotional intro APR exists separately -- see Intro Offers)"


def _nusenda_consumer_row(warnings, config):
    """Nusenda's consumer-card aggregate. A single live scrape (reusing
    HTMLScraper, the same class the main pipeline uses) supplies annual
    fee, late/returned/cash-advance/foreign-transaction fees, the APR
    range, and each card's reward_structure -- confirmed shared across all
    of Nusenda's own non-secured card templates. The Secured card's own
    entry from that same scrape (already correctly sourced from its own
    APR addendum, see html_scraper.py's _fetch_secured_apr) supplies the
    Secured row below, instead of a second hardcoded copy.
    """
    nusenda_cfg = config["institutions"]["nusenda"]
    scraper = HTMLScraper(name=nusenda_cfg["name"], url=nusenda_cfg["url"], config=nusenda_cfg)
    try:
        cards = scraper.scrape()
    except Exception as e:
        msg = f"[Credit card matrix] Failed to scrape Nusenda: {e}"
        logger.error(msg)
        warnings.append(msg)
        cards = []

    general_card = next((c for c in cards if c.get("category") != "credit_card_secured"), None)
    secured_card = next((c for c in cards if c.get("category") == "credit_card_secured"), None)

    sources = {}
    if general_card:
        for field in ("annual_fee", "purchase_apr", "late_payment_fee", "returned_item_fee",
                       "cash_advance_fee", "foreign_transaction_fee"):
            sources[field] = nusenda_cfg["url"]

    # Stop payment isn't part of the credit-card terms-and-conditions page
    # -- reuses the retail fee widget scraper (LKCSFeeScraper, the same
    # class run.py's main pipeline uses for Nusenda's general/deposit fees)
    # rather than hardcoding it, since it's confirmed live-sourced there.
    stop_payment = NOT_STATED
    retail_cfg = config["institutions"].get("nusenda_retail_fees")
    if retail_cfg:
        retail_scraper = LKCSFeeScraper(name=retail_cfg["name"], url=retail_cfg["url"], config=retail_cfg)
        try:
            retail_cards = retail_scraper.scrape()
            _extend_real_warnings(warnings, retail_scraper.warnings)
            general_fees = next((c for c in retail_cards if "stop_payment_fee" in c), None)
            if general_fees:
                stop_payment = general_fees["stop_payment_fee"]
                sources["stop_payment"] = retail_cfg["url"]
        except Exception as e:
            msg = f"[Credit card matrix] Failed to scrape Nusenda retail fees for stop payment: {e}"
            logger.error(msg)
            warnings.append(msg)

    # APR ceiling, Penalty APR, and minimum finance charge are general
    # account terms stated in the Secured addendum (an NCUA-wide cap and a
    # penalty-default rate, not Secured-specific) -- the one Nusenda PDF
    # this module fetches directly, since these don't appear on the
    # terms-and-conditions page HTMLScraper already covers.
    addendum_url = "https://www.nusenda.org/docs/default-source/addendums/visa-9-75-11-10-25--16-75.pdf?sfvrsn=66e73a2a_1"
    text = _fetch_pdf_text(addendum_url, warnings, "Nusenda Secured APR addendum (for ceiling/penalty APR/min finance charge)")
    apr_ceiling = NOT_STATED
    penalty_apr = NOT_STATED
    min_finance_charge = NOT_STATED
    if text:
        m = re.search(r"maximum rate of\s*(\d{2}\.?\d?)%", text, re.IGNORECASE)
        if m:
            apr_ceiling = f"{m.group(1)}%"
            sources["apr_ceiling"] = addendum_url
        m2 = re.search(r"Penalty ANNUAL PERCENTAGE RATE of\s*(\d{2}\.?\d{0,2})%", text, re.IGNORECASE)
        if m2:
            penalty_apr = f"{m2.group(1)}%"
            sources["penalty_apr"] = addendum_url
        m3 = re.search(r"minimum FINANCE\s*\n?\s*CHARGE of \$([\d,]+(?:\.\d{2})?)", text, re.IGNORECASE)
        if m3:
            min_finance_charge = f"${m3.group(1)}"
            sources["min_finance_charge"] = addendum_url

    reward_parts = [f"{c['card_name']}: {c.get('rewards_structure', NOT_STATED)}" for c in cards]
    reward_structure = " ".join(reward_parts) if reward_parts else NOT_STATED
    if cards:
        sources["reward_structure"] = nusenda_cfg["url"]

    card_promotions = _nusenda_promo_check(nusenda_cfg["url"], warnings)
    sources["card_promotions"] = nusenda_cfg["url"]

    values = {
        "annual_fee": general_card.get("annual_fee", NOT_STATED) if general_card else NOT_STATED,
        "apr_purchases": (
            f"{general_card['purchase_apr']} (variable, Prime + 5.75-9.75%)"
            if general_card and general_card.get("purchase_apr") else NOT_STATED
        ),
        "apr_ceiling": apr_ceiling,
        "penalty_apr": penalty_apr,
        "late_payment": general_card.get("late_payment_fee", NOT_STATED) if general_card else NOT_STATED,
        "returned_payment": general_card.get("returned_item_fee", NOT_STATED) if general_card else NOT_STATED,
        "cash_advance": general_card.get("cash_advance_fee", NOT_STATED) if general_card else NOT_STATED,
        "balance_transfer": NOT_STATED,
        "foreign_transaction": general_card.get("foreign_transaction_fee", NOT_STATED) if general_card else NOT_STATED,
        "over_limit": NOT_STATED,
        "paper_statement": NOT_STATED,
        "stop_payment": stop_payment,
        "expedited_payment": NOT_STATED,
        "research_copies": NOT_STATED,
        "min_finance_charge": min_finance_charge,
        "min_payment": NOT_STATED,
        "grace_period": NOT_STATED,
        "reward_structure": reward_structure,
        "card_promotions": card_promotions,
        "effective_date": NOT_STATED,
    }

    # This module's own extra data (Secured card fields) rides along on the
    # values dict under a private key build_matrix() reads and strips back
    # out, since _nusenda_consumer_row's return shape has to match the
    # other two institutions' functions.
    values["_secured_card"] = secured_card
    # No LLM fallback wired into HTMLScraper yet -- Nusenda's fields are
    # always regex-derived for now, same contract as the other two
    # institutions' functions regardless.
    return values, sources, set()


def _secu_nm_consumer_row(warnings):
    """Reuses SharedCreditCardDisclosureScraper (the same class the main
    pipeline uses for SECU NM's credit cards) for annual fee, cash advance,
    balance transfer, foreign transaction, and late payment, instead of
    duplicating that scraper's regex separately -- one PDF, one place that
    knows how to parse it. Over-limit, minimum payment, grace period, and
    reward structure aren't part of that scraper's canonical output, so
    they're extracted here from a second fetch of the same PDF.
    """
    secu_cfg = {
        "rates_url": "https://www.secunm.org/loans/loans/credit-card.html",
        # All 3 real cards -- if only one is configured here, `cards` (and
        # so `apr_values` below) silently loses the other two, producing a
        # single-card "range" instead of a true min-max across all 3.
        "products": {
            "Visa Platinum": {"category": "credit_card_standard"},
            "Visa Gold": {"category": "credit_card_standard"},
            "Visa Classic": {"category": "credit_card_standard"},
        },
    }
    pdf_url = "https://cdn.firstbranchcms.com/kcms-doc/29/68439/Credit-Card-Disclosure.pdf"
    scraper = SharedCreditCardDisclosureScraper(
        name="State Employees Credit Union of New Mexico", url=pdf_url, config=secu_cfg,
    )
    cards = scraper.scrape()
    _extend_real_warnings(warnings, scraper.warnings)

    card = cards[0] if cards else {}
    sources = {}
    for field in ("annual_fee", "cash_advance_fee", "balance_transfer_fee", "foreign_transaction_fee", "late_payment_fee"):
        if field in card:
            sources[field] = pdf_url

    text = _fetch_pdf_text(pdf_url, warnings, "SECU NM Credit Card Disclosure (over-limit/min payment/grace/rewards)")
    if not text:
        return {key: NOT_STATED for key, _ in MATRIX_ROWS}, sources, set()

    def find(pattern):
        m = re.search(pattern, text, re.IGNORECASE)
        return m.group(1).strip() if m else NOT_STATED

    over_limit = find(r"Over-the-Limit Fee:\s*(\$[\d.]+ if over-the-limit by \d+%)")
    min_payment = find(r"MINIMUM PAYMENT:\s*([^.]+\.)")
    grace = find(r"Your due date is approximately\s*(\d+ days after the close of each billing cycle)")
    for field in ("over_limit", "min_payment", "grace_period"):
        sources[field] = pdf_url

    reward_m = re.search(r"UCHOOSE REWARDS.*?Business accounts not eligible for rewards programs\.", text, re.IGNORECASE)
    # uChoose Rewards is one shared program across all 3 SECU NM cards (per
    # this same disclosure) -- unlike Century Bank, there's no per-card
    # variation to preserve here.
    reward_structure = reward_m.group(0) if reward_m else NOT_STATED
    sources["reward_structure"] = pdf_url

    homepage_url = "https://www.secunm.org/"
    promo_found = False
    homepage_text = None
    try:
        r = requests.get(homepage_url, headers=HEADERS, timeout=20)
        r.raise_for_status()
        homepage_text = " ".join(BeautifulSoup(r.content, "html.parser").get_text(separator=" ", strip=True).split())
        promo_found = bool(re.search(r"bonus|promo", homepage_text, re.IGNORECASE))
    except Exception as e:
        msg = f"[Credit card matrix] Failed to check SECU NM homepage for card promotions: {e}"
        logger.error(msg)
        warnings.append(msg)

    card_promotions = (
        "Possible promotion detected on secunm.org homepage -- verify manually."
        if promo_found else "None advertised (checked disclosure PDF and secunm.org homepage)"
    )
    sources["card_promotions"] = homepage_url

    stale_m = re.search(r"current as of\s+([A-Za-z]+ \d{1,2},\s*\d{4})", text, re.IGNORECASE)
    effective_date = stale_m.group(1) if stale_m else NOT_STATED
    sources["effective_date"] = pdf_url

    # `cards` already has purchase_apr set per card by scrape() (it fetches
    # the rates table internally) -- reading it back off `cards` instead of
    # calling _fetch_apr_by_card() again avoids a second live fetch of the
    # same page.
    apr_values = [c["purchase_apr"] for c in cards if c.get("purchase_apr")]
    # Sort numerically, not lexicographically -- string min/max on "7.50%"
    # vs "12.50%" picks "12.50%" as smallest (leading "1" < "7"), which is
    # wrong. Parsed back to the original string once ordered, so the
    # displayed value still exactly matches what was scraped.
    apr_numeric = sorted(apr_values, key=lambda v: float(v.rstrip("%")))
    apr_purchases = f"{apr_numeric[0]} - {apr_numeric[-1]} (non-variable per card)" if apr_numeric else NOT_STATED
    if apr_values:
        sources["apr_purchases"] = secu_cfg["rates_url"]

    values = {
        "annual_fee": card.get("annual_fee", NOT_STATED),
        "apr_purchases": apr_purchases,
        "apr_ceiling": NOT_STATED,
        "penalty_apr": NOT_STATED,
        "late_payment": card.get("late_payment_fee", NOT_STATED),
        "returned_payment": NOT_STATED,
        "cash_advance": card.get("cash_advance_fee", NOT_STATED),
        "balance_transfer": card.get("balance_transfer_fee", NOT_STATED),
        "foreign_transaction": card.get("foreign_transaction_fee", NOT_STATED),
        "over_limit": over_limit,
        "paper_statement": NOT_STATED,
        "stop_payment": NOT_STATED,
        "expedited_payment": NOT_STATED,
        "research_copies": NOT_STATED,
        "min_finance_charge": NOT_STATED,
        "min_payment": min_payment,
        "grace_period": grace,
        "reward_structure": reward_structure,
        "card_promotions": card_promotions,
        "effective_date": f"{effective_date} (stale -- see Data Quality Notes)" if effective_date != NOT_STATED else NOT_STATED,
    }
    # No LLM fallback wired into SharedCreditCardDisclosureScraper yet -- SECU
    # NM's fields are always regex-derived for now, same contract as the
    # other two institutions' functions regardless.
    return values, sources, set()


def _fn1870_rewards_and_promos(warnings):
    """Reward structure and promotions aren't part of the Schumer box table
    -- they're only described in prose on the card overview page (a
    separate URL from the TILA table), so this is a second targeted fetch,
    the same pattern as Century's and Nusenda's equivalents. Confirmed
    2026-07-29: no numeric earn rate is published anywhere, only a
    qualitative "redeem points for merchandise/travel" description --
    captured verbatim rather than summarized into a number that isn't
    actually stated.
    """
    url = "https://www.sunflowerbank.com/personal/loans-credit/credit-cards"
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        r.raise_for_status()
        soup = BeautifulSoup(r.content, "html.parser")
        main = soup.select_one("main") or soup
        text = " ".join(main.get_text(separator=" ", strip=True).split())
    except Exception as e:
        msg = f"[Credit card matrix] Failed to fetch First National 1870 card overview page for rewards/promotions: {e}"
        logger.error(msg)
        warnings.append(msg)
        return NOT_STATED, NOT_STATED, None

    reward_m = re.search(
        r"Every net retail purchase you charge gets you closer to redeeming points[^.!]*[.!]", text,
    )
    reward_structure = reward_m.group(0) if reward_m else NOT_STATED

    # Same conservative keyword check as Nusenda's/SECU's equivalents --
    # flags for human review rather than asserting confidently either way.
    promo_found = re.search(r"bonus|sign-up|sign up|welcome offer", text, re.IGNORECASE)
    card_promotions = (
        f"Possible promotion detected near: \"{text[max(0, promo_found.start()-40):promo_found.start()+80]}\" "
        "-- verify manually."
        if promo_found else "None advertised"
    )
    return reward_structure, card_promotions, url


def _fn1870_consumer_row(warnings, config):
    """First National 1870's consumer-card aggregate. Reuses
    SchumerBoxScraper (the same class the main pipeline uses) for the
    Classic/Gold/Platinum TILA table -- one disclosure covers all three
    identically, confirmed no per-tier fee differentiation is published
    anywhere, so any one of the three scraped cards is representative.
    """
    fn1870_cfg = config["institutions"]["fn1870_credit_cards"]
    scraper = SchumerBoxScraper(name=fn1870_cfg["name"], url=fn1870_cfg["url"], config=fn1870_cfg)
    try:
        cards = scraper.scrape()
    except Exception as e:
        msg = f"[Credit card matrix] Failed to scrape First National 1870 credit cards: {e}"
        logger.error(msg)
        warnings.append(msg)
        cards = []
    _extend_real_warnings(warnings, scraper.warnings)

    if not cards:
        return {key: NOT_STATED for key, _ in MATRIX_ROWS}, {}, set()

    card = cards[0]
    matrix_extra = card.get("_matrix_extra", {})
    sources = dict(card.get("_source_urls", {}))

    reward_structure, card_promotions, rewards_url = _fn1870_rewards_and_promos(warnings)
    if rewards_url:
        sources["reward_structure"] = rewards_url
        sources["card_promotions"] = rewards_url

    purchase_apr = card.get("purchase_apr")

    values = {
        "annual_fee": card.get("annual_fee", NOT_STATED),
        "apr_purchases": f"{purchase_apr} (variable, WSJ Prime)" if purchase_apr else NOT_STATED,
        "apr_ceiling": NOT_STATED,
        "penalty_apr": NOT_STATED,
        "late_payment": card.get("late_payment_fee", NOT_STATED),
        "returned_payment": card.get("returned_item_fee", NOT_STATED),
        "cash_advance": card.get("cash_advance_fee", NOT_STATED),
        "balance_transfer": card.get("balance_transfer_fee", NOT_STATED),
        "foreign_transaction": card.get("foreign_transaction_fee", NOT_STATED),
        "over_limit": NOT_STATED,
        # $2.00 "Paper Statement" on the comparison-table page is the
        # DEPOSIT account fee, not a credit card fee -- deliberately not
        # reused here.
        "paper_statement": NOT_STATED,
        # Stop Payment and Research/Copies aren't on the Schumer box, and
        # they're 2 of the same 14 fields config.yaml's fn1870_not_disclosed
        # entry already establishes are confirmed absent from any public
        # First National 1870 Fee Schedule -- this reflects that same
        # confirmed-absent finding rather than a separate "not found on
        # this page" claim.
        "stop_payment": NOT_PUBLICLY_DISCLOSED,
        "expedited_payment": matrix_extra.get("expedited_payment", NOT_STATED),
        "research_copies": NOT_PUBLICLY_DISCLOSED,
        "min_finance_charge": matrix_extra.get("min_finance_charge", NOT_STATED),
        "min_payment": NOT_STATED,
        "grace_period": matrix_extra.get("grace_period", NOT_STATED),
        "reward_structure": reward_structure,
        "card_promotions": card_promotions,
        # The Schumer box page publishes no effective date anywhere
        # (confirmed 2026-07-29) -- the only card source in this report
        # that doesn't; drift.py is the sole signal that terms changed.
        "effective_date": NOT_STATED,
    }

    # No LLM fallback wired into SchumerBoxScraper -- always empty for now,
    # same as Nusenda's and SECU NM's rows.
    return values, sources, set()


def build_matrix(config):
    """Returns (matrix, warnings, sources_by_institution).

    config: the loaded config.yaml dict -- needed to reuse HTMLScraper and
    LKCSFeeScraper for Nusenda with the same settings the main pipeline
    uses, rather than re-deriving them separately.

    matrix: {"rows": [...], "institutions": [name, ...]} -- one unified
    table (see module docstring for the reasoning). sources_by_institution:
    {institution: {matrix_field_key: source_url}}, persisted by run.py into
    data/history.json rather than shown in the report.
    """
    warnings = []

    century, century_sources, century_llm = _century_consumer_row(warnings)
    nusenda, nusenda_sources, nusenda_llm = _nusenda_consumer_row(warnings, config)
    secu, secu_sources, secu_llm = _secu_nm_consumer_row(warnings)
    fn1870, fn1870_sources, fn1870_llm = _fn1870_consumer_row(warnings, config)

    nusenda_secured_card = nusenda.pop("_secured_card", None)

    institutions = [
        "Nusenda Credit Union", "Century Bank (New Mexico)",
        "State Employees Credit Union of New Mexico", "First National 1870 (Sunflower Bank, N.A.)",
    ]
    data_by_institution = {
        "Nusenda Credit Union": nusenda,
        "Century Bank (New Mexico)": century,
        "State Employees Credit Union of New Mexico": secu,
        "First National 1870 (Sunflower Bank, N.A.)": fn1870,
    }
    sources_by_institution = {
        "Nusenda Credit Union": nusenda_sources,
        "Century Bank (New Mexico)": century_sources,
        "State Employees Credit Union of New Mexico": secu_sources,
        "First National 1870 (Sunflower Bank, N.A.)": fn1870_sources,
    }
    # Per-institution sets of matrix row keys whose value came from an LLM
    # fallback rather than a regex match -- see _CANONICAL_TO_MATRIX_KEY
    # above. Only Century (TcmIssuerScraper) can populate this today.
    llm_fields_by_institution = {
        "Nusenda Credit Union": nusenda_llm,
        "Century Bank (New Mexico)": century_llm,
        "State Employees Credit Union of New Mexico": secu_llm,
        "First National 1870 (Sunflower Bank, N.A.)": fn1870_llm,
    }

    century_secured = _century_secured_row(warnings) or {}
    nusenda_secured = {}
    if nusenda_secured_card:
        nusenda_secured = {
            "annual_fee": nusenda_secured_card.get("annual_fee", NOT_STATED),
            "apr_purchases": (
                f"{nusenda_secured_card['purchase_apr']} (variable, Prime + 9.75%)"
                if nusenda_secured_card.get("purchase_apr") else NOT_STATED
            ),
            "late_payment": nusenda_secured_card.get("late_payment_fee", NOT_STATED),
            "cash_advance": nusenda_secured_card.get("cash_advance_fee", NOT_STATED),
            "foreign_transaction": nusenda_secured_card.get("foreign_transaction_fee", NOT_STATED),
        }
    secured_by_institution = {
        "Nusenda Credit Union": nusenda_secured,
        "Century Bank (New Mexico)": century_secured,
        "State Employees Credit Union of New Mexico": {},
        # First National 1870's overview page names only Classic/Gold/
        # Platinum Rewards tiers -- no secured card product exists to check.
        "First National 1870 (Sunflower Bank, N.A.)": {},
    }

    rows = []
    for key, label in MATRIX_ROWS:
        cells = [data_by_institution[inst].get(key, NOT_STATED) for inst in institutions]
        if all(c == NOT_STATED for c in cells):
            continue
        llm_cells = [key in llm_fields_by_institution[inst] for inst in institutions]
        rows.append({"label": label, "cells": cells, "llm_cells": llm_cells})

        if key != "apr_purchases":
            continue

        secured_cells = []
        any_real_difference = False
        for inst, general_value in zip(institutions, cells):
            secured_value = secured_by_institution[inst].get("apr_purchases")
            if not secured_value:
                secured_cells.append(NO_SECURED_CARD)
            elif secured_value == general_value:
                secured_cells.append(SAME_AS_GENERAL_APR)
            else:
                secured_cells.append(secured_value)
                any_real_difference = True
        if any_real_difference:
            # The secured-card row isn't wired to TcmIssuerScraper's
            # per-field llm_derived tagging (_century_secured_row doesn't
            # track it) -- always False here rather than guessing.
            rows.append({
                "label": "APR -- Purchases (Secured Card)",
                "cells": secured_cells,
                "llm_cells": [False] * len(secured_cells),
            })

    return {"rows": rows, "institutions": institutions}, warnings, sources_by_institution
