import logging
import re
from collections import defaultdict
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
        field_confidence = {}
        keywords = self.get_keywords()

        tables = soup.find_all('table')
        logger.info(f"Found {len(tables)} tables on page for {self.name}")

        parsed_fields = set()

        # A field can legitimately be mentioned in more than one table (e.g.
        # a summary table plus a detailed disclosure table further down the
        # page) -- collect every match instead of overwriting in place, so a
        # genuine disagreement between two mentions is caught and flagged
        # rather than silently resolved to whichever occurrence happened to
        # come last in the document.
        table_candidates = defaultdict(list)
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
                                table_candidates[field].append(cleaned_val)
                                break

        for field, values in table_candidates.items():
            distinct = list(dict.fromkeys(values))
            base_data[field] = distinct[-1]
            parsed_fields.add(field)
            if len(distinct) == 1:
                field_confidence[field] = "high"
            else:
                field_confidence[field] = "low"
                msg = (
                    f"[{self.name}] Field '{field}' had {len(distinct)} conflicting values "
                    f"across the page's tables: {distinct} -- using the last one seen "
                    f"({distinct[-1]!r}); verify manually."
                )
                logger.warning(msg)
                self.warnings.append(msg)

        # 2. Check for missing fields and try to extract from raw body text as a fallback
        body_text = soup.get_text(separator="\n")
        body_text_lower = body_text.lower()
        
        lines = body_text.split('\n')
        for field, kw_list in keywords.items():
            if base_data[field] == "Not disclosed":
                # Look for matching line in body text
                for idx, line in enumerate(lines):
                    line_lower = line.lower()
                    for kw in kw_list:
                        if kw in line_lower:
                            # Try to extract the rest of the line or adjacent text
                            parts = line.split(':')
                            if len(parts) > 1 and kw in parts[0].lower():
                                value = parts[1].strip()
                            else:
                                value = line.strip()
                            # get_text(separator="\n") breaks a single prose
                            # sentence onto multiple lines whenever it crosses
                            # an inline tag (e.g. a bolded rate in its own
                            # <span>), so pull in following lines until we
                            # reach real sentence-ending punctuation.
                            lookahead = idx + 1
                            joined = 0
                            while (
                                not re.search(r"[.!?]\s*$", value)
                                and lookahead < len(lines)
                                and joined < 6
                            ):
                                nxt = lines[lookahead].strip()
                                lookahead += 1
                                if not nxt:
                                    continue
                                value = f"{value} {nxt}".strip()
                                joined += 1
                            base_data[field] = self.clean_value(value)
                            field_confidence[field] = "medium"
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
                "category": "credit_card_standard",
                "rewards_structure": "None",
                "intro_offers": base_data.get("intro_offers", "Not disclosed")
            },
            {
                "card_name": "Visa Platinum Rewards",
                "category": "credit_card_rewards",
                "rewards_structure": "1 bonus point for every dollar spent on purchases, and 3 bonus points per dollar in quarterly rotating categories.",
                "intro_offers": base_data.get("intro_offers", "Not disclosed")
            },
            {
                "card_name": "Visa Platinum Cash Rewards",
                "category": "credit_card_rewards",
                "rewards_structure": "1% cash back on all purchases and 5% cash back on purchases in quarterly rotating categories.",
                "intro_offers": base_data.get("intro_offers", "Not disclosed")
            },
            {
                "card_name": "Visa Secured Credit Card",
                "category": "credit_card_secured",
                "rewards_structure": "None",
                "intro_offers": "None"
            }
        ]

        for template in card_templates:
            card = base_data.copy()
            card["card_name"] = template["card_name"]
            card["category"] = template["category"]
            card["rewards_structure"] = template["rewards_structure"]
            if template["intro_offers"] != "Not disclosed":
                card["intro_offers"] = template["intro_offers"]
            # rewards_structure/card_name/category are curated per-card
            # values, not extracted from ambiguous text, so they're always
            # high confidence regardless of how the shared page fields scored.
            card["_field_confidence"] = {**field_confidence, "rewards_structure": "high"}
            cards.append(self.finalize_card(card))

        return cards
