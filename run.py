import json
import logging
import os
from datetime import datetime, timezone

import yaml

from scraper.html_scraper import HTMLScraper
from scraper.pdf_scraper import PDFScraper
from scraper.fee_schedule_scraper import StaticFeeScraper
from scraper.lkcs_widget_scraper import LKCSFeeScraper
from scraper.service_fee_table_scraper import ServiceFeeTableScraper
from scraper.labeled_features_scraper import LabeledFeaturesScraper
from scraper.multi_card_page_scraper import MultiCardPageScraper
from scraper.shared_credit_card_scraper import SharedCreditCardDisclosureScraper
from scraper.asserted_fee_scraper import AssertedFeeScraper
from scraper.tis_table_scraper import TisTableScraper
from scraper.tcm_issuer_scraper import TcmIssuerScraper
from scraper.schumer_box_scraper import SchumerBoxScraper
from scraper.comparison_table_scraper import ComparisonTableScraper
from scraper.regex_value_scraper import RegexValueScraper
from scraper.line_item_fee_scraper import LineItemFeeScraper
from scraper.product_column_table_scraper import ProductColumnTableScraper
from attribution import merge_institution_cards, build_fee_facts, flatten_fee_facts
from drift import load_previous_fee_facts, mark_drift
from feedback import load_feedback_log, compute_flag_track_record, promote_confirmed_synonyms
from credit_card_matrix import build_matrix
from report import render_report
from emailer import send_email

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
)
logger = logging.getLogger("FeeComparisonScraper")

SCRAPER_TYPES = {
    "html": HTMLScraper,
    "pdf": PDFScraper,
    "static": StaticFeeScraper,
    "lkcs_widget": LKCSFeeScraper,
    "service_fee_table": ServiceFeeTableScraper,
    "labeled_features": LabeledFeaturesScraper,
    "multi_card_page": MultiCardPageScraper,
    "shared_credit_card_disclosure": SharedCreditCardDisclosureScraper,
    "asserted_fee": AssertedFeeScraper,
    "tis_table": TisTableScraper,
    "tcm_issuer": TcmIssuerScraper,
    "schumer_box": SchumerBoxScraper,
    "comparison_table": ComparisonTableScraper,
    "regex_value": RegexValueScraper,
    "line_item_fee": LineItemFeeScraper,
    "product_column_table": ProductColumnTableScraper,
}


def scrape_all(config):
    results = {}
    for inst_key, inst_cfg in config["institutions"].items():
        name = inst_cfg["name"]
        scraper_cls = SCRAPER_TYPES.get(inst_cfg["type"])
        if not scraper_cls:
            logger.error(f"Unknown scraper type '{inst_cfg['type']}' for {name}, skipping.")
            results[inst_key] = {"name": name, "cards": [], "warnings": [f"Unknown scraper type: {inst_cfg['type']}"]}
            continue

        scraper = scraper_cls(name=name, url=inst_cfg.get("url", ""), config=inst_cfg)
        try:
            cards = scraper.scrape()
        except Exception as e:
            logger.exception(f"Scraper for {name} raised an unexpected error")
            cards = []
            scraper.warnings.append(f"Scraper crashed: {str(e)}")

        results[inst_key] = {"name": name, "cards": cards, "warnings": scraper.warnings}
        logger.info(f"{name}: scraped {len(cards)} card(s), {len(scraper.warnings)} warning(s).")

    return results


def write_history(results, facts_by_institution, credit_card_matrix_sources):
    os.makedirs("data", exist_ok=True)
    fee_facts = flatten_fee_facts(facts_by_institution)
    snapshot = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "institutions": results,
        # Flattened, attributed view of the same data -- each entry states
        # exactly which product(s) a fee was verified for (see
        # attribution.py), whether it changed since the last snapshot (see
        # drift.py), and how confident/what mechanism the extraction is,
        # instead of implying every fee applies institution-wide at equal
        # certainty.
        "fee_facts": fee_facts,
        # Per-field source URL for every cell in the Credit Card Comparison
        # matrix (see credit_card_matrix.py) -- not shown in the report
        # itself (kept clean per user preference), but recorded here so
        # sourcing is queryable and durable instead of living only in
        # someone's memory of how this was built. See also
        # memory/credit_card_matrix_sources.md for the human-readable index.
        "credit_card_matrix_sources": credit_card_matrix_sources,
    }
    with open("data/history.json", "w", encoding="utf-8") as f:
        json.dump(snapshot, f, indent=2)
    logger.info("Wrote data/history.json")


def main():
    with open("config.yaml", "r") as f:
        config = yaml.safe_load(f)

    settings = config["settings"]
    primary_institution = settings["primary_institution"]
    subject = f"{settings['email']['subject_prefix']} - {datetime.now().strftime('%Y-%m-%d')}"

    # Read yesterday's snapshot and any confirmed feedback *before* this
    # run touches either -- drift needs the pre-overwrite history.json, and
    # a confirmed synonym should apply to this very run's scraping, not
    # just to how the report renders.
    previous_fee_facts = load_previous_fee_facts()
    feedback_log = load_feedback_log()
    promoted = promote_confirmed_synonyms(feedback_log)
    if promoted:
        logger.info(f"Promoted {len(promoted)} confirmed synonym(s) into the shared taxonomy: {promoted}")

    results = scrape_all(config)

    institution_cards = merge_institution_cards(results)
    facts_by_institution = build_fee_facts(institution_cards)
    mark_drift(facts_by_institution, previous_fee_facts)
    track_records = compute_flag_track_record(feedback_log)

    # Credit cards get a single institution-level comparison matrix rather
    # than going through the per-product category pipeline above -- see
    # credit_card_matrix.py for why (none of these institutions' card data
    # supports a clean per-product comparison).
    credit_card_matrix, matrix_warnings, credit_card_matrix_sources = build_matrix(config)

    html_body = render_report(
        results, primary_institution, subject, facts_by_institution, track_records,
        credit_card_matrix, matrix_warnings,
    )

    write_history(results, facts_by_institution, credit_card_matrix_sources)

    if os.environ.get("DRY_RUN") == "1":
        os.makedirs("output", exist_ok=True)
        out_path = os.path.join("output", "dry_run_report.html")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(html_body)
        logger.info(f"DRY_RUN=1: wrote report to {out_path} instead of sending email.")
        return

    try:
        send_email(subject, html_body, config)
    except Exception as e:
        logger.error(f"Failed to send email: {e}")


if __name__ == "__main__":
    main()
