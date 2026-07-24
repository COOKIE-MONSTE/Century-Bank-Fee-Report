"""Shared, cross-institution synonyms for canonical fee types.

config.yaml's per-institution `field_keywords` stays the source of truth
for that institution's exact phrasing, but any phrase confirmed (via
data/feedback_log.yaml -- see feedback.py) to mean the same fee at one
institution is learned here, so every other institution's scraper
recognizes it too, without a human having to re-teach it bank by bank.

BASE_SYNONYMS is curated/version-controlled (edit it directly for phrases
you already know about); learned_synonyms.yaml accumulates confirmed
additions from the feedback loop and is merged on top at scrape time.
"""

import os
import yaml

LEARNED_SYNONYMS_PATH = "data/learned_synonyms.yaml"

# Synonyms pooled from institutions already configured, kept centrally
# (not duplicated per-institution in config.yaml) so a new institution
# gets baseline recognition of common fee names for free.
BASE_SYNONYMS = {
    "annual_fee": ["annual fee", "yearly fee", "card membership fee"],
    "cash_advance_fee": [
        "cash advance fee", "cash advance charge", "cash advance:",
        "atm cash advance charge", "cash withdrawal fee",
    ],
    "balance_transfer_fee": ["balance transfer fee", "balance transfer:", "balance transfer charge"],
    "foreign_transaction_fee": [
        "foreign transaction", "foreign transaction fee",
        "international transaction fee", "foreign currency conversion fee",
    ],
    "late_payment_fee": ["late payment", "late payment fee", "late fee"],
    "returned_item_fee": [
        "returned payment", "returned item fee", "returned payment fee", "returned check fee",
    ],
    "overdraft_fee": ["overdraft fee", "overdraft charge", "nsf fee", "insufficient funds fee"],
    "returned_deposit_item_fee": ["returned deposit item", "returned deposit item fee"],
    "stop_payment_fee": ["stop payment", "stop payment fee"],
    "dormant_account_fee": ["dormant account", "dormant account fee", "inactivity fee"],
    "atm_out_of_network_fee": [
        "atm use of foreign machine", "out of network atm fee",
        "foreign atm fee", "non-network atm fee",
    ],
    "wire_transfer_outgoing_domestic_fee": [
        "wire transfer, domestic", "outgoing domestic wire fee", "domestic wire transfer fee",
    ],
    "wire_transfer_outgoing_international_fee": [
        "wire transfer, international", "outgoing international wire fee", "international wire transfer fee",
    ],
    "card_replacement_fee": ["atm card or replacement", "card replacement fee"],
}


def _load_learned_synonyms():
    if not os.path.exists(LEARNED_SYNONYMS_PATH):
        return {}
    with open(LEARNED_SYNONYMS_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def get_synonyms():
    """Returns {fee_type: [phrase, ...]}, merging the curated base list with
    any phrases promoted from confirmed human feedback."""
    merged = {k: list(v) for k, v in BASE_SYNONYMS.items()}
    for fee_type, phrases in _load_learned_synonyms().items():
        merged.setdefault(fee_type, [])
        for phrase in phrases:
            if phrase not in merged[fee_type]:
                merged[fee_type].append(phrase)
    return merged


def add_learned_synonym(fee_type, phrase):
    """Persists a confirmed phrase -> fee_type mapping so every institution's
    scraper recognizes it starting with the next run, instead of requiring
    the fix to be re-applied by hand for each bank that uses that phrasing.
    """
    phrase = phrase.strip().lower()
    learned = _load_learned_synonyms()
    learned.setdefault(fee_type, [])
    if phrase in learned[fee_type]:
        return learned, False
    learned[fee_type].append(phrase)
    os.makedirs(os.path.dirname(LEARNED_SYNONYMS_PATH), exist_ok=True)
    with open(LEARNED_SYNONYMS_PATH, "w", encoding="utf-8") as f:
        yaml.safe_dump(learned, f, sort_keys=True, allow_unicode=True)
    return learned, True
