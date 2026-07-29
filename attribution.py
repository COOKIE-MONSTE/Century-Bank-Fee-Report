"""Attributes each scraped fee to the specific product(s) it was verified for.

A scraper can produce several cards (products) per institution -- e.g.
Nusenda's Visa Platinum and Visa Platinum Rewards. A field value showing up
on one card is a fact about that one product, not the whole institution, so
this module never assumes a fee applies more broadly than what was actually
scraped. It only ever widens a fee's stated scope to multiple/all products
when every product in that group discloses the *same* value for that field.

It also classifies *how* each fee is charged (its "mechanism") purely from
the value text, so a comparison can tell "$2.50 flat ATM fee" apart from
"1% of withdrawal amount" even when both sit under the same fee_type/label --
same name, different mechanism, not actually apples-to-apples.
"""

import re
from collections import defaultdict, OrderedDict

from scraper.categories import label_for

EMPTY_VALUES = {None, "", "Not disclosed"}

# Keys on a card that describe the product/extraction itself rather than a fee.
NON_FEE_KEYS = {
    "card_name", "category", "_field_confidence", "_asserted_universal", "_issuer",
    "_matrix_extra", "_source_urls",
}

# "llm_assisted" ranks below "low": it means deterministic extraction found
# nothing at all and an LLM read the page instead (see
# scraper/llm_fallback.py) -- a fundamentally different, weaker kind of
# uncertainty than "low" (which means multiple deterministic matches
# disagreed). Ranked lowest so the weakest-link combination in
# _combine_confidence() below never lets an LLM-derived value hide behind
# a more confident one.
CONFIDENCE_RANK = {"llm_assisted": -1, "low": 0, "medium": 1, "high": 2}

MECHANISM_LABELS = {
    "no_fee": "No fee",
    "flat": "Flat fee",
    "percentage": "Percentage-based",
    "recurring": "Recurring (periodic)",
    "per_unit": "Per-unit / rate-based",
    "variable": "Variable / cost-based",
    "unknown": "Unclear",
}


def merge_institution_cards(results):
    """Groups scraped cards by institution display name.

    `results` is keyed by config entry (e.g. "nusenda", "nusenda_retail_fees")
    -- one institution can be backed by multiple config entries/scrapers, so
    this merges those into a single {institution_name: [cards...]} mapping
    while preserving first-seen institution order.
    """
    institution_cards = OrderedDict()
    for inst in results.values():
        name = inst["name"]
        institution_cards.setdefault(name, [])
        institution_cards[name].extend(inst["cards"])
    return institution_cards


def classify_mechanism(value):
    """Best-effort classification of *how* a fee is charged, from its value text.

    This is deliberately coarse (regex over the already-extracted string, no
    new scraping) but is enough to catch the comparisons that matter: a flat
    dollar fee is not the same kind of number as a percentage, a recurring
    monthly charge, or a per-unit rate, even when they share a fee_type.
    """
    if value in EMPTY_VALUES:
        return None
    text = value.strip().lower()
    if text in ("none", "no charge", "n/a") or re.match(r"^\$?0(\.00)?%?$", text):
        return "no_fee"
    if "actual cost" in text or "at cost" in text:
        return "variable"
    # Allows one adjective between "per" and the period noun (e.g. "per
    # quarterly statement cycle", "per monthly statement cycle") -- a bare
    # `per statement` pattern missed these entirely, which mislabeled a
    # genuinely recurring fee as "flat" just because its period was
    # qualified rather than named directly.
    if re.search(r"\bper\s+(?:\w+\s+)?(month|year|annum|statement)\b", text):
        return "recurring"
    if re.search(r"\bper\s+(hour|item|occurrence|incident|transaction)\b", text):
        return "per_unit"
    if "%" in text:
        return "percentage"
    if re.search(r"\$\s?\d", text):
        return "flat"
    return "unknown"


def _combine_confidence(levels):
    """Combines several products' confidence for the same value.

    Uses the weakest link: claiming a fee is "verified across all products"
    is only as trustworthy as the least-certain extraction that fed it, so
    one low-confidence source pulls the whole fact down rather than being
    averaged away by more confident ones.
    """
    if not levels:
        return "high"
    return min(levels, key=lambda lvl: CONFIDENCE_RANK.get(lvl, CONFIDENCE_RANK["high"]))


def build_fee_facts(institution_cards):
    """Returns {institution_name: [fact, ...]}.

    Each fact represents one distinct (category, fee_type, value) combo and
    records exactly which product(s) it was confirmed for, plus how
    confident the extraction was and what mechanism the fee uses:
        {
            "fee_type": "annual_fee",
            "category": "credit_card_rewards",
            "category_label": "Rewards Credit Card",
            "value": "None",
            "scope": "verified across all products" | "verified across: A, B" | "Visa Platinum"
                     | "asserted universal (per X disclosure)",
            "scope_products": ["Visa Platinum Rewards", "Visa Platinum Cash Rewards"],
            "confidence": "high" | "medium" | "low",
            "mechanism": "flat" | "percentage" | "recurring" | "per_unit" | "variable" | "no_fee" | "unknown",
            "mechanism_label": "Flat fee",
            "verification": "empirical" | "asserted",
            "source_quote": None | str,
            "source_locator": None | str,
        }

    Scope is computed within (institution, category) -- not institution-wide
    -- so a coincidental value match between e.g. a credit card's fee and an
    unrelated checking account's fee never gets reported as "verified"
    across both; they're different products in different categories.

    A field a scraper set via BaseScraper.assert_universal_fee() (a written
    catch-all statement, e.g. "no maintenance fees on this account") is
    tracked in its own bucket, entirely separate from the empirical
    "N products independently agree" convergence below -- it never
    contributes to a verified-across-all count, and never gets silently
    upgraded to that same confidence. It's always its own fact, visibly
    labeled "asserted universal" with the source quote attached.
    """
    facts_by_institution = OrderedDict()

    for inst_name, cards in institution_cards.items():
        by_category = defaultdict(list)
        for card in cards:
            by_category[card.get("category", "uncategorized")].append(card)

        facts = []
        for category, cat_cards in by_category.items():
            total_products = len(cat_cards)

            fee_types = OrderedDict()
            for card in cat_cards:
                for key in card:
                    if key not in NON_FEE_KEYS:
                        fee_types[key] = True

            for fee_type in fee_types:
                # value -> [(product, confidence), ...] -- independently scraped
                value_to_entries = defaultdict(list)
                # (value, quote) -> [(product, confidence, locator), ...] -- asserted
                asserted_to_entries = defaultdict(list)

                for card in cat_cards:
                    value = card.get(fee_type)
                    if value in EMPTY_VALUES:
                        continue
                    product = card.get("card_name", "Unknown product")
                    confidence = card.get("_field_confidence", {}).get(fee_type, "high")
                    issuer = card.get("_issuer")
                    asserted_meta = card.get("_asserted_universal", {}).get(fee_type)

                    if asserted_meta:
                        akey = (value, asserted_meta["quote"])
                        if not any(p == product for p, _, _, _ in asserted_to_entries[akey]):
                            asserted_to_entries[akey].append((product, confidence, asserted_meta.get("locator"), issuer))
                    else:
                        if not any(p == product for p, _, _ in value_to_entries[value]):
                            value_to_entries[value].append((product, confidence, issuer))

                for value, entries in value_to_entries.items():
                    products = [p for p, _, _ in entries]
                    confidence = _combine_confidence([c for _, c, _ in entries])
                    issuers = sorted({i for _, _, i in entries if i})

                    if total_products > 1 and len(products) == total_products:
                        scope = "verified across all products"
                    elif len(products) > 1:
                        scope = f"verified across: {', '.join(products)}"
                    else:
                        scope = products[0]

                    mechanism = classify_mechanism(value)
                    facts.append({
                        "fee_type": fee_type,
                        "category": category,
                        "category_label": label_for(category),
                        "value": value,
                        "scope": scope,
                        "scope_products": products,
                        "confidence": confidence,
                        "mechanism": mechanism,
                        "mechanism_label": MECHANISM_LABELS.get(mechanism, "Unclear"),
                        "verification": "empirical",
                        "source_quote": None,
                        "source_locator": None,
                        "issuers": issuers,
                    })

                for (value, quote), entries in asserted_to_entries.items():
                    products = [p for p, _, _, _ in entries]
                    confidence = _combine_confidence([c for _, c, _, _ in entries])
                    locator = entries[0][2]
                    issuers = sorted({i for _, _, _, i in entries if i})
                    scope = f"asserted universal (per {', '.join(products)} disclosure{'s' if len(products) > 1 else ''})"

                    mechanism = classify_mechanism(value)
                    facts.append({
                        "fee_type": fee_type,
                        "category": category,
                        "category_label": label_for(category),
                        "value": value,
                        "scope": scope,
                        "scope_products": products,
                        "confidence": confidence,
                        "mechanism": mechanism,
                        "mechanism_label": MECHANISM_LABELS.get(mechanism, "Unclear"),
                        "verification": "asserted",
                        "source_quote": quote,
                        "source_locator": locator,
                        "issuers": issuers,
                    })

        facts_by_institution[inst_name] = facts

    return facts_by_institution


def flatten_fee_facts(facts_by_institution):
    """Flattens facts into one list, each entry self-describing:
    institution, product_name, fee_type, category, value, scope, confidence,
    and mechanism -- suitable for a JSON history record. `product_name` is
    only set when the fact resolved to a single product; multi-product facts
    rely on `scope` / `scope_products` instead of picking one name to
    represent them all.

    `changed_since_last_snapshot` / `previous_value` are only present once
    drift.mark_drift() has annotated the facts; they default to unset here
    so this function works before or after that step runs.
    """
    flat = []
    for inst_name, facts in facts_by_institution.items():
        for fact in facts:
            flat.append({
                "institution": inst_name,
                "product_name": fact["scope_products"][0] if len(fact["scope_products"]) == 1 else None,
                "fee_type": fact["fee_type"],
                "category": fact["category"],
                "category_label": fact["category_label"],
                "value": fact["value"],
                "scope": fact["scope"],
                "scope_products": fact["scope_products"],
                "confidence": fact["confidence"],
                "mechanism": fact["mechanism"],
                "mechanism_label": fact["mechanism_label"],
                "verification": fact.get("verification", "empirical"),
                "source_quote": fact.get("source_quote"),
                "source_locator": fact.get("source_locator"),
                "issuers": fact.get("issuers", []),
                "changed_since_last_snapshot": fact.get("changed_since_last_snapshot", False),
                "previous_value": fact.get("previous_value"),
            })
    return flat
