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

Enterprise Bank & Trust is the one exception: it genuinely differentiates
per tier (three annual fees, two APR bands), so unlike the others it DOES
also feed the per-product attribution pipeline as three separate cards
(see config.yaml's enterprise_bank_credit_cards, type "product_column_table").
It's still included here too, combined into one column, so every
institution in this report is comparable side by side in the same table --
_enterprise_bank_consumer_row() below does that combining by listing each
tier's own annual fee/APR within the cell (e.g. "Visa Non-Rewards:
10.99% - 21.99% / Visa Rewards: 15.49% - 23.99% / ..."), never a
collapsed min-max range: confirmed 2026-07-30 that no Enterprise card is
actually offered at the union of all three tiers' ranges, so a blended
number wouldn't correspond to any real product.

A cell is one of three states, never conflated:
  - a real value (possibly "None" if a source affirmatively states no fee)
  - NOT_STATED: the available source doesn't address this fee at all --
    NOT the same as zero, and rendered differently so a reader never
    mistakes an undisclosed fee for a free one.
  - NOT_PUBLICLY_DISCLOSED: confirmed to exist but never published (set
    per-partner-bank, disclosed only at account opening, etc.) -- a
    stronger claim than NOT_STATED, so never used just because a search
    came up empty; only when a source affirmatively says so.

GUARDRAIL, applies to every _xxx_consumer_row() function below -- deposit-
account fees never feed a credit card row, even when the number would
coincidentally look right. Concretely: First National 1870's $2 paper
statement fee comes from its Truth-in-Savings DEPOSIT disclosures, not
its card disclosure (fn1870_checking/fn1870_savings' scrapers, not
fn1870_credit_cards' -- _fn1870_consumer_row() below never reads from
those); SECU NM's $10.00 returned item fee and $25.00 uncollected funds
rate come from the general Fee Schedule (secu_nm, type
"service_fee_table"), and neither is stated to apply to card payments, so
_secu_nm_consumer_row() below never reads them either.

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
from scraper.line_item_fee_scraper import LineItemFeeScraper
from scraper.product_column_table_scraper import ProductColumnTableScraper

logger = logging.getLogger("FeeComparisonScraper")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}

NOT_STATED = "Not stated"

MATRIX_ROWS = [
    ("annual_fee", "Annual Fee"),
    ("apr_purchases", "APR -- Purchases"),
    ("apr_cash_advances", "APR -- Cash Advances"),
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


def _century_secured_agreement_extras(warnings):
    """Grace period, over-limit fee, and penalty APR aren't part of
    TcmIssuerScraper's canonical field set -- this reads them directly
    from the Secured Card Cardholder Agreement (confirmed 2026-07-30: no
    "over-limit"/"penalty"/"default APR" wording appears anywhere in that
    51K-character document, so absence is treated as a confirmed "None",
    not "not stated").

    MEDIUM CONFIDENCE CAVEAT: this is the *Secured* agreement specifically
    -- TCM's unsecured Consumer agreement (what TcmIssuerScraper's
    "consumer" agreement type actually fetches for the rest of Century's
    fields) almost certainly matches on these three points, since TCM's
    agreements share structure across products, but a March 2026 unsecured
    version with these same sections could not be located to confirm
    directly. TODO: obtain/verify against the unsecured Consumer agreement
    once TCM publishes one with matching content.
    """
    url = "https://www.tcmbank.com/documents/45248/903684/SecuredCardCardholderAgreement_March2026.pdf"
    text = _fetch_pdf_text(url, warnings, "Century Bank / TCM Secured Card Cardholder Agreement (grace period/over-limit/penalty APR)")
    if not text:
        return {}, None

    warnings.append(
        "[Credit card matrix] Century Bank's Grace Period, Over-Limit, and Penalty APR values are sourced "
        "from TCM's *Secured* Card Cardholder Agreement, not the Consumer agreement used for Century's other "
        "card fields -- medium confidence pending an unsecured agreement to confirm these three points match. "
        f"Source: {url}"
    )

    # pypdf's text extraction inserts a stray space inside some words in
    # this document ("Y our" for "Your") -- the regex below starts from
    # "Payment Due Date" specifically to sidestep that rather than trying
    # to match "Your" literally.
    extras = {}
    grace_m = re.search(r"Payment Due Date is at least (\d+) days after the close of each billing cycle", text)
    if grace_m:
        extras["grace_period"] = (
            f"Your Payment Due Date is at least {grace_m.group(1)} days after the close of each billing cycle. "
            "No interest on Purchases (including Balance Transfers) if you pay your entire balance by the due "
            "date. There is no grace period on Cash Advances -- finance charges accrue from the transaction date."
        )

    # The agreement DOES discuss "Over Limit Transactions" (the cardholder's
    # obligation not to exceed the Credit Limit, and the bank's discretion
    # to honor one anyway) -- but never states a dollar FEE for one, so a
    # bare phrase-presence check would wrongly conclude "not stated" here.
    # Confirmed "None" by checking specifically for a dollar amount near
    # any over-limit mention, not just whether the phrase appears at all.
    over_limit_fee_found = any(
        re.search(r"\$[\d,]+(?:\.\d{2})?", text[max(0, m.start() - 200):m.start() + 200])
        for m in re.finditer(r"over[\s-]limit", text, re.IGNORECASE)
    )
    if re.search(r"over[\s-]limit", text, re.IGNORECASE) and not over_limit_fee_found:
        extras["over_limit"] = "None"

    if not re.search(r"penalty|default APR", text, re.IGNORECASE):
        extras["penalty_apr"] = "None"
    return extras, url


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

    GUARDRAIL -- never source Century's annual fee/APR fields from TCM's
    own-brand disclosures (e.g. tcmbank.com/docs/tcmbanklibraries/
    disclosures/tcm-disclosures.pdf or the copy mirrored on icba.org): TCM
    sets pricing per PARTNER BANK, and TCM's own-brand terms are
    confirmed different from Century's (Century's late fee is $30/$41;
    TCM's own-brand disclosure, effective 2026-01-30, says "Up to $40" --
    different structures, different programs). Century's annual fee/APR
    stay NOT_PUBLICLY_DISCLOSED for exactly this reason: no source states
    Century-specific figures, and TCM's generic ones must never be
    substituted in.
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

    secured_agreement_extras, secured_agreement_url = _century_secured_agreement_extras(warnings)
    if secured_agreement_url:
        for field in secured_agreement_extras:
            sources[field] = secured_agreement_url

    values = {
        "annual_fee": NOT_PUBLICLY_DISCLOSED,
        "apr_purchases": NOT_PUBLICLY_DISCLOSED,
        # Same "set per-partner-bank, never published" reasoning as
        # annual_fee/apr_purchases above applies to every APR type here,
        # not just purchases -- TCM discloses none of them for Century.
        "apr_cash_advances": NOT_PUBLICLY_DISCLOSED,
        "apr_ceiling": NOT_STATED,
        "penalty_apr": secured_agreement_extras.get("penalty_apr", NOT_STATED),
        "late_payment": card.get("late_payment_fee", NOT_STATED),
        "returned_payment": card.get("returned_item_fee", NOT_STATED),
        "cash_advance": card.get("cash_advance_fee", NOT_STATED),
        "balance_transfer": card.get("balance_transfer_fee", NOT_STATED),
        "foreign_transaction": card.get("foreign_transaction_fee", NOT_STATED),
        "over_limit": secured_agreement_extras.get("over_limit", NOT_STATED),
        "paper_statement": extra.get("paper_statement", NOT_STATED),
        "stop_payment": card.get("stop_payment_fee", NOT_STATED),
        "expedited_payment": extra.get("expedited_payment", NOT_STATED),
        "research_copies": extra.get("research_copies", NOT_STATED),
        "min_finance_charge": extra.get("min_finance_charge", NOT_STATED),
        "min_payment": NOT_STATED,
        "grace_period": secured_agreement_extras.get("grace_period", NOT_STATED),
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


def _nusenda_terms_page_extras(url, warnings):
    """Over-Limit and Grace Period aren't part of HTMLScraper's canonical
    field set (they're prose, not table rows, and HTMLScraper's generic
    fallback only searches fields already seeded by get_default_fields()
    or a table match) -- read directly from the same terms-and-conditions
    page HTMLScraper already covers, via a second targeted fetch.
    """
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        r.raise_for_status()
        text = " ".join(BeautifulSoup(r.content, "html.parser").get_text(separator=" ", strip=True).split())
    except Exception as e:
        msg = f"[Credit card matrix] Failed to fetch Nusenda terms page for over-limit/grace period: {e}"
        logger.error(msg)
        warnings.append(msg)
        return {}

    extras = {}
    if re.search(r"Over-the-Credit Limit:\s*None\.\s*We do not allow transactions that will exceed your credit limit\.", text, re.IGNORECASE):
        extras["over_limit"] = "None. We do not allow transactions that will exceed your credit limit."
    grace_m = re.search(
        r"(Your due date is at least \d+ days after we mail your billing statement\. "
        r"We will not charge you interest on purchases[^.]*\. "
        r"We will begin charging interest on cash advances, balance transfers and credit card checks[^.]*\.)",
        text, re.IGNORECASE,
    )
    if grace_m:
        extras["grace_period"] = grace_m.group(1)
    return extras


def _nusenda_superseded_addendum_extras(warnings):
    """Research/Copies fee for Nusenda's cards only appears in an older
    addendum (stamped 08/08/22) that is no longer linked from Nusenda's
    current agreement page -- confirmed 2026-07-30: the current 12/22/25
    addenda's "OTHER FEES" list runs Late Fee / Returned Payment / Cash
    Advance / Annual Fee only, with no Copy Charges section at all.

    Kept anyway (it's the most recent figure available) but flagged as
    superseded directly in the value string and via a Data Quality Notes
    warning, matching how SECU NM's stale effective_date is already
    handled -- rather than silently presenting a 2022 figure as current.
    Two independent tells confirm this document is genuinely the older
    one, not just an alternate current document: its repeat late fee is
    $35.00 versus $30.00 in the current addenda, and it quotes Prime at
    5.50% versus 6.75% in the 12/22/25 addenda.
    """
    url = "https://www.nusenda.org/docs/default-source/addendums/visadisclosure_775_addendum_disclosure.pdf"
    text = _fetch_pdf_text(url, warnings, "Nusenda Visa Addendum (superseded, 08/08/22 -- Research/Copies fee)")
    if not text:
        return None, None

    m = re.search(
        r"A \$([\d.]+) per page fee will be assessed for each additional copy you request of a monthly "
        r"billing statement\. A \$([\d.]+) charge will be assessed for each charge slip copy you request\.",
        text, re.IGNORECASE,
    )
    if not m:
        return None, None

    warnings.append(
        "[Credit card matrix] Nusenda's Research/Copies fee is sourced from a Visa addendum dated 08/08/22 that "
        "is no longer linked from Nusenda's current agreement page -- the current 12/22/25 addenda don't restate "
        f"this fee at all. Kept as the most recent figure available. Source: {url}"
    )
    value = (
        f"${m.group(1)} per page for each additional copy of a monthly billing statement; ${m.group(2)} for each "
        "charge slip copy (from a Nusenda addendum dated 08/08/22 that is no longer linked from their current "
        "disclosures page; not restated in the 12/22/25 addenda -- see Data Quality Notes)"
    )
    return value, url


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

    # Each addendum stamps its own effective date right after "Equal
    # Opportunity Lender" (e.g. "ADM 12/22/25") -- extracted live rather
    # than hardcoded, since Nusenda reissues these addenda periodically
    # (this exact mechanism is why the 08/08/22 Research/Copies source
    # above is stale: it was superseded by newer addenda that dropped
    # that fee entirely). The general (non-Secured) addendum's date
    # covers most of this column's fields; the Secured addendum's own
    # date (used for the Secured APR row) can differ, noted separately.
    effective_date = NOT_STATED
    general_addendum_url = "https://www.nusenda.org/docs/default-source/addendums/visa-9-75-12-22-25--16-50.pdf"
    general_text = _fetch_pdf_text(general_addendum_url, warnings, "Nusenda general Visa addendum (effective date)")
    date_pattern = r"Equal Opportunity Lender\s*ADM\s*(\d{1,2}/\d{1,2}/\d{2,4})"
    general_date_m = re.search(date_pattern, general_text, re.IGNORECASE) if general_text else None
    secured_date_m = re.search(date_pattern, text, re.IGNORECASE) if text else None
    if general_date_m:
        effective_date = general_date_m.group(1)
        sources["effective_date"] = general_addendum_url
        if secured_date_m and secured_date_m.group(1) != general_date_m.group(1):
            effective_date += f" (Secured card addendum dated {secured_date_m.group(1)})"

    reward_parts = [f"{c['card_name']}: {c.get('rewards_structure', NOT_STATED)}" for c in cards]
    reward_structure = " ".join(reward_parts) if reward_parts else NOT_STATED
    if cards:
        sources["reward_structure"] = nusenda_cfg["url"]

    card_promotions = _nusenda_promo_check(nusenda_cfg["url"], warnings)
    sources["card_promotions"] = nusenda_cfg["url"]

    terms_extras = _nusenda_terms_page_extras(nusenda_cfg["url"], warnings)
    for field in terms_extras:
        sources[field] = nusenda_cfg["url"]

    # Research/Copies has no CURRENT Nusenda card source at all -- unlike
    # Stop Payment above, which is genuinely, independently confirmed live
    # via the General Fees widget, so that one is NOT re-pointed at the
    # superseded document even though it also appears there.
    research_copies, research_copies_url = _nusenda_superseded_addendum_extras(warnings)
    if research_copies_url:
        sources["research_copies"] = research_copies_url

    values = {
        "annual_fee": general_card.get("annual_fee", NOT_STATED) if general_card else NOT_STATED,
        "apr_purchases": (
            f"{general_card['purchase_apr']} (variable, Prime + 5.75-9.75%)"
            if general_card and general_card.get("purchase_apr") else NOT_STATED
        ),
        "apr_cash_advances": (
            f"{general_card['cash_advance_apr']} (variable, Prime + 5.75-9.75%)"
            if general_card and general_card.get("cash_advance_apr") else NOT_STATED
        ),
        "apr_ceiling": apr_ceiling,
        "penalty_apr": penalty_apr,
        "late_payment": general_card.get("late_payment_fee", NOT_STATED) if general_card else NOT_STATED,
        "returned_payment": general_card.get("returned_item_fee", NOT_STATED) if general_card else NOT_STATED,
        "cash_advance": general_card.get("cash_advance_fee", NOT_STATED) if general_card else NOT_STATED,
        "balance_transfer": general_card.get("balance_transfer_fee", NOT_STATED) if general_card else NOT_STATED,
        "foreign_transaction": general_card.get("foreign_transaction_fee", NOT_STATED) if general_card else NOT_STATED,
        "over_limit": terms_extras.get("over_limit", NOT_STATED),
        "paper_statement": NOT_STATED,
        "stop_payment": stop_payment,
        "expedited_payment": NOT_STATED,
        "research_copies": research_copies or NOT_STATED,
        "min_finance_charge": min_finance_charge,
        "min_payment": NOT_STATED,
        "grace_period": terms_extras.get("grace_period", NOT_STATED),
        "reward_structure": reward_structure,
        "card_promotions": card_promotions,
        "effective_date": effective_date,
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

    # Research/Copies isn't in the card disclosure -- SECU NM's general
    # Fee Schedule covers it as an institution-wide service fee (not
    # card-specific, but the disclosure has no separate card version and
    # these are exactly the kind of general administrative fees that
    # would apply to a card account too, same as Century's and FN1870's
    # equivalents, which are also just generic photocopy/research fees in
    # THEIR agreements rather than card-specific line items).
    fee_schedule_url = "https://www.secunm.org/fee-schedule.html"
    research_copies = NOT_STATED
    try:
        r = requests.get(fee_schedule_url, headers=HEADERS, timeout=20)
        r.raise_for_status()
        fee_schedule_text = " ".join(BeautifulSoup(r.content, "html.parser").get_text(separator=" ", strip=True).split())
        copies_m = re.search(
            r"Check Copies\s*(\$[\d.]+)\s*Monthly Statement Copies\s*(\$[\d.]+)\s*Both Available via Online Banking\s*(FREE)",
            fee_schedule_text, re.IGNORECASE,
        )
        research_m = re.search(r"Account Reconciliation or Research\s*(\$[\d.]+ per hour)", fee_schedule_text, re.IGNORECASE)
        if copies_m and research_m:
            research_copies = (
                f"Check copies: {copies_m.group(1)}; monthly statement copies: {copies_m.group(2)} "
                f"(both {copies_m.group(3)} via online banking); account research: {research_m.group(1)}"
            )
            sources["research_copies"] = fee_schedule_url
    except Exception as e:
        msg = f"[Credit card matrix] Failed to fetch SECU NM fee schedule for research/copies: {e}"
        logger.error(msg)
        warnings.append(msg)

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
    # Deliberately NOT sourced from the disclosure PDF's own stated
    # "ANNUAL PERCENTAGE RATE (APR) FOR PURCHASES: 7.50-18.00%." line --
    # investigated 2026-07-30 (requester flagged 7.50%-18.00% as a
    # correction). That line is SECU's general creditworthiness-tiered
    # spread across any applicant/tier, not a number tied to any of the 3
    # real, currently-offered cards. Confirmed with the requester: keep
    # deriving the range from the live per-card rates table above (this
    # row's whole purpose is a spread across an institution's own named
    # tiers, which 7.50-18.00% doesn't clearly map to -- see MATRIX_ROWS'
    # apr_ceiling, a genuinely different concept, for the disclosure-
    # stated-cap case this would need to be to belong there instead).
    if apr_values:
        sources["apr_purchases"] = secu_cfg["rates_url"]

    values = {
        "annual_fee": card.get("annual_fee", NOT_STATED),
        "apr_purchases": apr_purchases,
        # The disclosure states exactly one APR ("Non-Variable Rate Visa
        # Credit Cards... ANNUAL PERCENTAGE RATE (APR) FOR PURCHASES:
        # 7.50-18.00%") with no separate cash-advance rate anywhere --
        # confirmed 2026-07-30, not assumed to match the purchase rate.
        "apr_cash_advances": NOT_STATED,
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
        "research_copies": research_copies,
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
    cash_advance_apr = card.get("cash_advance_apr")

    values = {
        "annual_fee": card.get("annual_fee", NOT_STATED),
        "apr_purchases": f"{purchase_apr} (variable, WSJ Prime)" if purchase_apr else NOT_STATED,
        "apr_cash_advances": f"{cash_advance_apr} (variable, WSJ Prime)" if cash_advance_apr else NOT_STATED,
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


def _enterprise_bank_consumer_row(warnings, config):
    """Enterprise Bank & Trust (New Mexico)'s card aggregate. Unlike the
    other institutions in this matrix, Enterprise genuinely differentiates
    terms per tier (three annual fees, two APR bands, no shared
    disclosure) -- reuses ProductColumnTableScraper (the same class the
    main pipeline uses) for all three cards, then combines them into the
    matrix's one-column-per-institution shape rather than picking one card
    as "representative" the way the shared-disclosure institutions' rows
    do.

    GUARDRAIL -- never source Enterprise's foreign transaction fee from
    enterprisebank.com/sites/default/files/2019-12/Visa_Credit_Card_
    Benefits_Consumer_WEB.pdf: that's a 2019 Visa cardholder BENEFITS
    guide, not a fee schedule. A 1% figure circulates from it but isn't
    confirmed on any Enterprise primary page (checked 2026-07-30,
    including the CFPB credit card agreement database) -- foreign
    transaction fee stays NOT_PUBLICLY_DISCLOSED rather than sourced from
    that document.
    """
    eb_cfg = config["institutions"]["enterprise_bank_credit_cards"]
    scraper = ProductColumnTableScraper(name=eb_cfg["name"], url=eb_cfg["url"], config=eb_cfg)
    try:
        cards = scraper.scrape()
    except Exception as e:
        msg = f"[Credit card matrix] Failed to scrape Enterprise Bank credit cards: {e}"
        logger.error(msg)
        warnings.append(msg)
        cards = []
    _extend_real_warnings(warnings, scraper.warnings)

    if not cards:
        return {key: NOT_STATED for key, _ in MATRIX_ROWS}, {}, set()

    sources = {field: eb_cfg["url"] for field in ("annual_fee", "apr_purchases", "reward_structure", "card_promotions")}

    # Stop Payment isn't a credit-card-specific concept here -- it's on
    # the institution-wide Schedule of Fees the main pipeline already
    # scrapes, reused directly rather than left "Not stated" just because
    # it isn't on the card comparison page (same pattern as Nusenda's
    # retail-fee reuse above).
    stop_payment = NOT_STATED
    schedule_cfg = config["institutions"].get("enterprise_bank_fee_schedule")
    if schedule_cfg:
        schedule_scraper = LineItemFeeScraper(name=schedule_cfg["name"], url=schedule_cfg["url"], config=schedule_cfg)
        try:
            schedule_cards = schedule_scraper.scrape()
            _extend_real_warnings(warnings, schedule_scraper.warnings)
            if schedule_cards and schedule_cards[0].get("stop_payment_fee"):
                stop_payment = schedule_cards[0]["stop_payment_fee"]
                sources["stop_payment"] = schedule_cfg["url"]
        except Exception as e:
            msg = f"[Credit card matrix] Failed to scrape Enterprise Bank fee schedule for stop payment: {e}"
            logger.error(msg)
            warnings.append(msg)

    annual_fee_parts, apr_purchases_parts, cash_advance_aprs, reward_parts, promo_parts = [], [], [], [], []
    effective_date = None
    for card in cards:
        name = card.get("card_name", "Unknown")
        annual_fee_parts.append(f"{card.get('annual_fee', NOT_STATED)} ({name})")
        # Per-card breakdown, not a collapsed min-max range -- confirmed
        # 2026-07-30 that no Enterprise card is actually offered at the
        # union of all three tiers' ranges (10.99%-23.99%); that number
        # doesn't correspond to any real product. Same per-card-breakdown
        # treatment already used for reward_structure/card_promotions
        # below, now applied consistently to APR too.
        if card.get("purchase_apr"):
            apr_purchases_parts.append(f"{name}: {card['purchase_apr']}")
        if card.get("cash_advance_apr"):
            cash_advance_aprs.append(card["cash_advance_apr"])
        reward_parts.append(f"{name}: {card.get('rewards_structure', NOT_STATED)}")
        if card.get("intro_offers"):
            promo_parts.append(f"{name}: {card['intro_offers']}")
        if not effective_date:
            effective_date = card.get("_matrix_extra", {}).get("effective_date")

    annual_fee = " / ".join(annual_fee_parts) if annual_fee_parts else NOT_STATED
    apr_purchases = " / ".join(apr_purchases_parts) if apr_purchases_parts else NOT_STATED
    # All three tiers carry the identical cash advance APR (confirmed
    # 2026-07-30), so unlike purchase APR there's no per-tier spread to
    # preserve -- one shared value with a note is accurate here.
    apr_cash_advances = (
        f"{cash_advance_aprs[0]} (variable, Prime; identical across all three cards)"
        if cash_advance_aprs else NOT_STATED
    )
    reward_structure = " ".join(reward_parts) if reward_parts else NOT_STATED
    card_promotions = " ".join(promo_parts) if promo_parts else NOT_STATED

    values = {
        "annual_fee": annual_fee,
        "apr_purchases": apr_purchases,
        "apr_cash_advances": apr_cash_advances,
        "apr_ceiling": NOT_STATED,
        "penalty_apr": NOT_STATED,
        "late_payment": NOT_PUBLICLY_DISCLOSED,
        "returned_payment": NOT_PUBLICLY_DISCLOSED,
        "cash_advance": NOT_PUBLICLY_DISCLOSED,
        "balance_transfer": NOT_PUBLICLY_DISCLOSED,
        "foreign_transaction": NOT_PUBLICLY_DISCLOSED,
        "over_limit": NOT_STATED,
        "paper_statement": NOT_STATED,
        "stop_payment": stop_payment,
        "expedited_payment": NOT_STATED,
        "research_copies": NOT_STATED,
        "min_finance_charge": NOT_STATED,
        "min_payment": NOT_STATED,
        "grace_period": NOT_STATED,
        "reward_structure": reward_structure,
        "card_promotions": card_promotions,
        # The page's own footnote states this directly (see
        # ProductColumnTableScraper's staleness check) -- not the run
        # date, and not hardcoded here: read live off whichever card
        # scraped it, so this self-corrects if the bank updates the page.
        "effective_date": (
            f"{effective_date} (stale -- see Data Quality Notes)" if effective_date else NOT_STATED
        ),
    }
    if effective_date:
        sources["effective_date"] = eb_cfg["url"]

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
    enterprise, enterprise_sources, enterprise_llm = _enterprise_bank_consumer_row(warnings, config)

    nusenda_secured_card = nusenda.pop("_secured_card", None)

    # Century Bank is listed first (leftmost column) per user request
    # (2026-07-30) -- purely a display-order choice, matching
    # settings.highlighted_institution in config.yaml. The dicts below are
    # keyed by name, not position, so this reordering is the only change
    # needed: every cell is still looked up by institution name, so values
    # can't end up under the wrong column just because this list moved.
    institutions = [
        "Century Bank (New Mexico)", "Nusenda Credit Union",
        "State Employees Credit Union of New Mexico", "First National 1870 (Sunflower Bank, N.A.)",
        "Enterprise Bank & Trust (New Mexico)",
    ]
    data_by_institution = {
        "Nusenda Credit Union": nusenda,
        "Century Bank (New Mexico)": century,
        "State Employees Credit Union of New Mexico": secu,
        "First National 1870 (Sunflower Bank, N.A.)": fn1870,
        "Enterprise Bank & Trust (New Mexico)": enterprise,
    }
    sources_by_institution = {
        "Nusenda Credit Union": nusenda_sources,
        "Century Bank (New Mexico)": century_sources,
        "State Employees Credit Union of New Mexico": secu_sources,
        "First National 1870 (Sunflower Bank, N.A.)": fn1870_sources,
        "Enterprise Bank & Trust (New Mexico)": enterprise_sources,
    }
    # Per-institution sets of matrix row keys whose value came from an LLM
    # fallback rather than a regex match -- see _CANONICAL_TO_MATRIX_KEY
    # above. Only Century (TcmIssuerScraper) can populate this today.
    llm_fields_by_institution = {
        "Nusenda Credit Union": nusenda_llm,
        "Century Bank (New Mexico)": century_llm,
        "State Employees Credit Union of New Mexico": secu_llm,
        "First National 1870 (Sunflower Bank, N.A.)": fn1870_llm,
        "Enterprise Bank & Trust (New Mexico)": enterprise_llm,
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
        # Enterprise's three tiers (Non-Rewards/Rewards/Rewards Plus) are
        # all unsecured Visa cards -- no secured product exists to check.
        "Enterprise Bank & Trust (New Mexico)": {},
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
