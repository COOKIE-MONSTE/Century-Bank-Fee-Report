import logging
import io
import re
import pypdf
from .base import BaseScraper

logger = logging.getLogger("FeeComparisonScraper")

class PDFScraper(BaseScraper):
    def __init__(self, name, url, config):
        super().__init__(name, url, config)

    def scrape(self):
        """Downloads the PDF and extracts card disclosures using regexes and block parsing."""
        response = self.fetch_url()
        if not response:
            return []

        # Load PDF using pypdf
        try:
            pdf_file = io.BytesIO(response.content)
            reader = pypdf.PdfReader(pdf_file)
            logger.info(f"Loaded {self.name} PDF. Total Pages: {len(reader.pages)}")
        except Exception as e:
            msg = f"Failed to parse PDF content for {self.name}: {str(e)}"
            logger.error(msg)
            self.warnings.append(msg)
            return []

        # Cards we want to extract from the PDF disclosures
        target_cards = {
            "Rewards Platinum Credit Card": "Rewards Platinum",
            "Cash Rewards Platinum Credit Card": "Cash Rewards Platinum",
            "Platinum Edition Credit Card": "Platinum Edition",
            "Secured Platinum Credit Card": "Secured Platinum",
            "Visa® Signature Credit Card": "Visa Signature"
        }

        cards = []

        # Default rewards mapping (since rewards structures are not in Schumer Box disclosures)
        rewards_mapping = {
            "Rewards Platinum": "1 point per dollar on net purchases.",
            "Cash Rewards Platinum": "1 point per dollar on net purchases (redeemable for cash back/statement credit).",
            "Platinum Edition": "None",
            "Secured Platinum": "None",
            "Visa Signature": "1.5 points per dollar on purchases. 30,000 bonus points after spending $2,500 in first 3 months."
        }

        category_mapping = {
            "Rewards Platinum": "credit_card_rewards",
            "Cash Rewards Platinum": "credit_card_rewards",
            "Platinum Edition": "credit_card_standard",
            "Secured Platinum": "credit_card_secured",
            "Visa Signature": "credit_card_premium",
        }

        # Scan each page for target card names
        for page_idx in range(len(reader.pages)):
            try:
                text = reader.pages[page_idx].extract_text()
            except Exception as e:
                logger.error(f"Failed to extract text from page {page_idx+1}: {e}")
                continue

            if not text:
                continue

            # Check if this page is the start of a target card's disclosures
            matched_card_key = None
            matched_card_name = None
            
            lines = text.split('\n')
            if not lines:
                continue
            
            # Check the very first line of the page
            first_line = lines[0].strip()

            clean_first_line = re.sub(r'[^a-zA-Z]', '', first_line).lower()
            # Check longest names first so e.g. "Cash Rewards Platinum Credit Card"
            # isn't misidentified as "Rewards Platinum Credit Card" (a substring of it).
            for key, val in sorted(target_cards.items(), key=lambda kv: len(kv[0]), reverse=True):
                clean_key = re.sub(r'[^a-zA-Z]', '', key).lower()
                # If first line exactly matches or starts with the card name (e.g. key)
                if clean_key == clean_first_line or clean_key in clean_first_line:
                    matched_card_key = key
                    matched_card_name = val
                    break

            if matched_card_name:
                logger.info(f"Parsing disclosures page {page_idx+1} for: {matched_card_name}")
                card_data = self.get_default_fields()
                card_data["card_name"] = matched_card_name
                card_data["category"] = category_mapping.get(matched_card_name, "uncategorized")
                card_data["rewards_structure"] = rewards_mapping.get(matched_card_name, "None")

                # --- 1. Annual Fee ---
                fee_match = re.search(r"Annual\s+Fee\s+(None|\$[0-9,.]+)", text, re.IGNORECASE)
                if fee_match:
                    card_data["annual_fee"] = self.clean_value(fee_match.group(1))
                
                # --- 2. Purchase APR ---
                intro_match = re.search(r"(\b0%\s+Introductory\s+APR\s+applies\s+for\s+[^.\n]+)", text, re.IGNORECASE)
                if intro_match:
                    card_data["intro_offers"] = self.clean_value(intro_match.group(1))
                
                # Standard APR is after "After that, your APR will be" or is a single rate
                apr_match = re.search(r"After\s+that,\s+your\s+APR\s+will\s+be\s+([0-9.]+(?:\s*to\s*[0-9.]+)?\s*%)", text, re.IGNORECASE)
                if apr_match:
                    card_data["purchase_apr"] = self.clean_value(apr_match.group(1))
                else:
                    single_apr_match = re.search(r"Rate\s*\(APR\)\s*for\s*Purchases\s*\n\s*([0-9.]+\s*%)", text, re.IGNORECASE)
                    if single_apr_match:
                        card_data["purchase_apr"] = self.clean_value(single_apr_match.group(1))
                    else:
                        # Backup for secured cards
                        sec_apr_match = re.search(r"Purchases\s*\n\s*([0-9.]+\s*%)", text, re.IGNORECASE)
                        if sec_apr_match:
                            card_data["purchase_apr"] = self.clean_value(sec_apr_match.group(1))

                # --- 3. Balance Transfer APR ---
                bt_text_match = re.search(r"APR\s+for\s+Balance\s+Transfers\s*[\n\s]*(?:0%[^.\n]*[\n\s]*)?After\s+that,\s+your\s+APR\s+will\s+be\s+([0-9.]+(?:\s*to\s*[0-9.]+)?\s*%)", text, re.IGNORECASE)
                if bt_text_match:
                    card_data["balance_transfer_apr"] = self.clean_value(bt_text_match.group(1))
                elif apr_match:
                    card_data["balance_transfer_apr"] = self.clean_value(apr_match.group(1))
                else:
                    single_bt_match = re.search(r"Transfers\s*\n\s*([0-9.]+\s*%)", text, re.IGNORECASE)
                    if single_bt_match:
                        card_data["balance_transfer_apr"] = self.clean_value(single_bt_match.group(1))

                # --- 4. Cash Advance APR ---
                ca_apr_match = re.search(r"Advances\s*\n\s*([0-9.]+(?:\s*to\s*[0-9.]+)?\s*%)", text, re.IGNORECASE)
                if ca_apr_match:
                    card_data["cash_advance_apr"] = self.clean_value(ca_apr_match.group(1))

                # --- 5. Block-based parsing for Transaction Fees ---
                tx_start = text.find("Transaction Fees")
                penalty_start = text.find("Penalty Fees")
                paper_start = text.find("Paper Statement")

                if tx_start != -1 and penalty_start != -1:
                    tx_block = text[tx_start:penalty_start]
                    lines_tx = tx_block.split('\n')
                    
                    # Match by line contents
                    for line in lines_tx:
                        line_lower = line.lower()
                        if "transfer" in line_lower and ("either" in line_lower or "percent" in line_lower or "%" in line_lower or "none" in line_lower):
                            card_data["balance_transfer_fee"] = self.clean_value(line)
                        elif "advance" in line_lower and ("either" in line_lower or "percent" in line_lower or "%" in line_lower or "none" in line_lower):
                            card_data["cash_advance_fee"] = self.clean_value(line)
                        elif ("foreign" in line_lower or "transaction" in line_lower) and ("1%" in line_lower or "percent" in line_lower or "%" in line_lower or "none" in line_lower):
                            card_data["foreign_transaction_fee"] = self.clean_value(line)

                # --- 6. Block-based parsing for Penalty Fees ---
                if penalty_start != -1:
                    end_idx = paper_start if paper_start != -1 else len(text)
                    penalty_block = text[penalty_start:end_idx]
                    
                    value_lines = []
                    for line in penalty_block.split('\n'):
                        line_stripped = line.strip()
                        if not line_stripped:
                            continue
                        if any(lbl in line_stripped for lbl in ["Penalty Fees", "Late", "Payment", "Over-the", "Credit", "Returned"]):
                            continue
                        value_lines.append(line_stripped)

                    if len(value_lines) >= 3:
                        card_data["late_payment_fee"] = self.clean_value(value_lines[0])
                        card_data["returned_payment_fee"] = self.clean_value(value_lines[2])

                # Log warnings for fields that failed to resolve
                for field in ["annual_fee", "purchase_apr", "late_payment_fee", "returned_payment_fee", "balance_transfer_fee", "cash_advance_fee", "foreign_transaction_fee"]:
                    if card_data[field] == "Not disclosed":
                        self.log_field_warning(matched_card_name, field)

                cards.append(self.finalize_card(card_data))

        # Remove duplicate entries
        unique_cards = []
        seen = set()
        for c in cards:
            if c["card_name"] not in seen:
                seen.add(c["card_name"])
                unique_cards.append(c)

        return unique_cards
