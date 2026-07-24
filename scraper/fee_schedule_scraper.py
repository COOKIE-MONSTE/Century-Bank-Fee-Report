from .base import BaseScraper


class StaticFeeScraper(BaseScraper):
    """Returns a hand-transcribed fee schedule straight from config.yaml.

    Used for documents with no extractable text (e.g. a scanned/photographed
    PDF fee schedule) where regex-based parsing isn't possible. The values
    live in config.yaml's `fees` map and only change when someone edits them.
    """

    def __init__(self, name, url, config):
        super().__init__(name, url, config)

    def scrape(self):
        product_name = self.config.get("product_name", "General Fee Schedule")
        fees = dict(self.config.get("fees", {}))
        fees["card_name"] = product_name
        # This schedule applies bank-wide rather than to one specific
        # account tier, so it's tagged as general_account_fees unless
        # config.yaml overrides it with a more specific category.
        fees["category"] = self.config.get("category", "general_account_fees")
        return [self.finalize_card(fees)]
