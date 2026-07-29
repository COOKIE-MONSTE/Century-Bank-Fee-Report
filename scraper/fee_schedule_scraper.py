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
        # Optional single institution-level note (e.g. "this institution
        # doesn't publish a general Fee Schedule online, these values are
        # all confirmed-absent rather than found") -- deliberately one
        # warning for the whole entry, not one per field, since every field
        # here shares the same underlying reason.
        warning = self.config.get("warning")
        if warning:
            self.warnings.append(warning)
        return [self.finalize_card(fees)]
