import logging
from bs4 import BeautifulSoup
from .base import BaseScraper

logger = logging.getLogger("FeeComparisonScraper")

class HTMLScraper(BaseScraper):
    def __init__(self, name, url, config):
        super().__init__(name, url, config)

    def scrape(self):
        """Scrapes card data from the HTML page."""
        response = self.fetch_url()
        if not response:
            return []

        soup = BeautifulSoup(response.content, 'html.parser')
        
        # 1. Parse all tables for key-value pairs
        base_data = self.get_default_fields()
        keywords = self.config.get("field_keywords", {})
        
        tables = soup.find_all('table')
        logger.info(f"Found {len(tables)} tables on page for {self.name}")

        parsed_fields = set()

        for idx, table in enumerate(tables):
            for row in table.find_all('tr'):
                cells = row.find_all(['td', 'th'])
                if len(cells) >= 2:
                    # Clean key and value text
                    key_text = cells[0].get_text(separator=" ").lower().strip()
                    val_text = cells[1].get_text(separator=" ").strip()
                    
                    # Try to match key_text against our configured keywords
                    for field, kw_list in keywords.items():
                        for kw in kw_list:
                            if kw in key_text:
                                cleaned_val = self.clean_value(val_text)
                                base_data[field] = cleaned_val
                                parsed_fields.add(field)
                                break

        # 2. Check for missing fields and try to extract from raw body text as a fallback
        body_text = soup.get_text(separator="\n")
        body_text_lower = body_text.lower()
        
        for field, kw_list in keywords.items():
            if base_data[field] == "Not disclosed":
                # Look for matching line in body text
                for line in body_text.split('\n'):
                    line_lower = line.lower()
                    for kw in kw_list:
                        if kw in line_lower:
                            # Try to extract the rest of the line or adjacent text
                            # For simplicity, we just use the line itself as the fallback value
                            parts = line.split(':')
                            if len(parts) > 1 and kw in parts[0].lower():
                                base_data[field] = self.clean_value(parts[1])
                            else:
                                base_data[field] = self.clean_value(line)
                            parsed_fields.add(field)
                            break
                    if base_data[field] != "Not disclosed":
                        break

        # 3. Clean up fields that tend to capture whole disclosure paragraphs
        # instead of a short comparable value (only replace when a match is
        # found, so we never lose data if the pattern doesn't apply).
        range_fields = ["purchase_apr", "balance_transfer_apr", "cash_advance_apr"]
        for field in range_fields:
            cleaned = self.extract_last_percentage_range(base_data.get(field))
            if cleaned:
                base_data[field] = cleaned

        fx_cleaned = self.extract_first_percentage(base_data.get("foreign_transaction_fee"))
        if fx_cleaned:
            base_data["foreign_transaction_fee"] = f"{fx_cleaned} of transaction amount"

        # 4. Log warning for any fields that are still "Not disclosed"
        for field in base_data.keys():
            if base_data[field] == "Not disclosed":
                self.log_field_warning("General", field)

        # 5. Generate card list from page details
        # We define a standard set of cards for Nusenda:
        # - Visa Platinum
        # - Visa Platinum Rewards
        # - Visa Platinum Cash Rewards
        # - Visa Secured
        # We will map specific rewards structures to them
        cards = []
        
        # Hardcoded definitions for card-specific fields that are static, 
        # or we scan page text to see if we can find them
        card_templates = [
            {
                "card_name": "Visa Platinum",
                "rewards_structure": "None",
                "intro_offers": base_data.get("intro_offers", "Not disclosed")
            },
            {
                "card_name": "Visa Platinum Rewards",
                "rewards_structure": "1 bonus point for every dollar spent on purchases, and 3 bonus points per dollar in quarterly rotating categories.",
                "intro_offers": base_data.get("intro_offers", "Not disclosed")
            },
            {
                "card_name": "Visa Platinum Cash Rewards",
                "rewards_structure": "1% cash back on all purchases and 5% cash back on purchases in quarterly rotating categories.",
                "intro_offers": base_data.get("intro_offers", "Not disclosed")
            },
            {
                "card_name": "Visa Secured Credit Card",
                "rewards_structure": "None",
                "intro_offers": "None"
            }
        ]

        for template in card_templates:
            card = base_data.copy()
            card["card_name"] = template["card_name"]
            card["rewards_structure"] = template["rewards_structure"]
            if template["intro_offers"] != "Not disclosed":
                card["intro_offers"] = template["intro_offers"]
            cards.append(card)

        return cards
