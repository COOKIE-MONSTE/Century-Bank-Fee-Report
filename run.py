import json
import logging
import os
from datetime import datetime, timezone

import yaml

from scraper.html_scraper import HTMLScraper
from scraper.pdf_scraper import PDFScraper
from scraper.fee_schedule_scraper import StaticFeeScraper
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


def write_history(results):
    os.makedirs("data", exist_ok=True)
    snapshot = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "institutions": results,
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

    results = scrape_all(config)
    html_body = render_report(results, primary_institution, subject)

    write_history(results)

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
