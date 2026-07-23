import yaml
import json
from scraper.html_scraper import HTMLScraper
from scraper.fee_schedule_scraper import StaticFeeScraper

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
