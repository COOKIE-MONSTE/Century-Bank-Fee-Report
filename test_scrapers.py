import yaml
import json
from scraper.html_scraper import HTMLScraper
from scraper.fee_schedule_scraper import StaticFeeScraper
from attribution import classify_mechanism

with open("config.yaml", "r") as f:
    config = yaml.safe_load(f)

# Test Nusenda HTMLScraper
nusenda_config = config["institutions"]["nusenda"]
print("Running Nusenda HTMLScraper...")
nusenda_scraper = HTMLScraper(
    name=nusenda_config["name"],
    url=nusenda_config["url"],
    config=nusenda_config
)
nusenda_cards = nusenda_scraper.scrape()
print(f"Scraped {len(nusenda_cards)} cards for Nusenda:")
print(json.dumps(nusenda_cards, indent=2))
print("Nusenda Scraper Warnings:", nusenda_scraper.warnings)

print("\n" + "="*50 + "\n")

# Test Century Bank StaticFeeScraper
century_config = config["institutions"]["century_bank"]
print("Running Century Bank StaticFeeScraper...")
century_scraper = StaticFeeScraper(
    name=century_config["name"],
    url=century_config.get("url", ""),
    config=century_config
)
century_cards = century_scraper.scrape()
print(f"Scraped {len(century_cards)} cards for Century Bank:")
print(json.dumps(century_cards, indent=2))
print("Century Bank Scraper Warnings:", century_scraper.warnings)

print("\n" + "="*50 + "\n")

# Test attribution.classify_mechanism()
print("Running classify_mechanism() tests...")
_MECHANISM_CASES = [
    ("None", "no_fee"),
    ("No Charge", "no_fee"),
    ("$0.00", "no_fee"),
    ("$35.00", "flat"),
    ("1%", "percentage"),
    ("1 percent", "percentage"),
    ("2.5 percent of transaction amount", "percentage"),
    ("$10.00 per item", "per_unit"),
    ("$5.00 per month", "recurring"),
    ("Actual cost", "variable"),
    ("Fees vary by location", "variable"),
    # Task B: a free allowance before a per-item charge is a distinct
    # mechanism from a bare per-item rate -- same fee name, different
    # real-world cost to a typical member (Nusenda's NSF tiers vs.
    # Century's flat $35.00 from the first item).
    ("No Charge for 1st-5th Items; $10.00 per item for 6th- or more items", "tiered_free"),
    ("Free for the first 3 transactions, then $2.00 per transaction", "tiered_free"),
    # Bare per_unit/no_fee must NOT be misclassified as tiered_free just
    # because one half of the pattern is present.
    ("$10.00 per item", "per_unit"),
    ("No Charge", "no_fee"),
]
_failures = []
for value, expected in _MECHANISM_CASES:
    actual = classify_mechanism(value)
    status = "PASS" if actual == expected else "FAIL"
    if actual != expected:
        _failures.append((value, expected, actual))
    print(f"  [{status}] classify_mechanism({value!r}) == {expected!r} (got {actual!r})")
if _failures:
    raise AssertionError(f"{len(_failures)} classify_mechanism() case(s) failed: {_failures}")
print(f"All {len(_MECHANISM_CASES)} classify_mechanism() cases passed.")
