"""Canonical product-category taxonomy used for cross-institution comparisons.

Fee comparisons are only meaningful between products of the same category --
config.yaml's field_keywords intentionally reuse field names (e.g.
`returned_item_fee`) for both a credit card's returned-payment fee and a
checking account's returned-deposit fee, since they're the same *shape* of
fee. Grouping by category before comparing keeps those semantically
different fees from being lined up against each other just because they
share a field name.
"""

CATEGORIES = {
    "credit_card_rewards": "Rewards Credit Card",
    "credit_card_standard": "Standard Credit Card",
    "credit_card_secured": "Secured Credit Card",
    "credit_card_premium": "Premium / Signature Credit Card",
    "checking_general": "Checking Account (tier unspecified)",
    "checking_basic": "Basic Checking Account",
    "checking_premium": "Premium / Dividend Checking Account",
    "savings_basic": "Basic Savings Account",
    "money_market": "Money Market Account",
    "certificate_of_deposit": "Certificate of Deposit",
    "ira": "IRA / Retirement Account",
    "general_account_fees": "General Account Fees (institution-wide, not tied to one product tier)",
    "uncategorized": "Uncategorized",
}

# Best-effort fallback used only when a scraper doesn't explicitly tag a
# card's category (e.g. a future institution added without updating its
# scraper). Order matters -- more specific keywords are checked first.
_KEYWORD_RULES = [
    ("secured", "credit_card_secured"),
    ("signature", "credit_card_premium"),
    ("rewards", "credit_card_rewards"),
    ("cash back", "credit_card_rewards"),
    ("cashback", "credit_card_rewards"),
    ("money market", "money_market"),
    ("dividend checking", "checking_premium"),
    ("premium checking", "checking_premium"),
    ("checking", "checking_general"),
    ("ira", "ira"),
    ("certificate", "certificate_of_deposit"),
    (" cd", "certificate_of_deposit"),
    ("savings", "savings_basic"),
    ("credit card", "credit_card_standard"),
    ("visa", "credit_card_standard"),
    ("general", "general_account_fees"),
]


def guess_category(card_name):
    """Best-effort category guess from a product name alone.

    Used only as a fallback -- scrapers that know their data's origin
    should set `category` explicitly instead of relying on this.
    """
    name_lower = (card_name or "").lower()
    for keyword, category in _KEYWORD_RULES:
        if keyword in name_lower:
            return category
    return "uncategorized"


def label_for(category):
    return CATEGORIES.get(category, CATEGORIES["uncategorized"])
