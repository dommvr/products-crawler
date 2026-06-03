"""
main.py — entry point for the Products Crawler.

Usage:
    python -m src.main
    python -m src.main --config path/to/config.yaml
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path

from src.config import Config
from src.crawler import Crawler
from src.extractor import Extractor
from src.resolver import resolve
from src.sheets import SheetsWriter
from src.utils import logger, read_names_file, setup_logging


async def run(cfg: Config) -> None:
    log = logging.getLogger("crawler")

    names = read_names_file(cfg.names_file)
    if not names:
        log.error(f"No entries found in {cfg.names_file}")
        sys.exit(1)

    log.info(f"Loaded {len(names)} company entries from {cfg.names_file}")
    log.info(f"Target categories: {cfg.categories}")
    log.info(f"Max pages per site: {cfg.max_pages or 'unlimited'}")

    extractor = Extractor(cfg)

    # Pre-translate categories once
    log.info("Translating categories to English...")
    await extractor.translate_categories()

    crawler = Crawler(cfg, extractor)
    writer = SheetsWriter(cfg)

    all_products = []
    failed = []

    for i, entry in enumerate(names, 1):
        entry = entry.strip()
        if not entry:
            continue

        # Use entry as company name (strip protocol/www for display)
        company_display = entry.replace("https://", "").replace("http://", "").replace("www.", "").split("/")[0]
        log.info(f"\n[{i}/{len(names)}] Processing: {entry!r} → company: {company_display!r}")

        # ── Step 1: Resolve to a working URL ──────────────────────────
        url = await resolve(entry, cfg)
        if not url:
            log.error(f"  Could not find a working URL for {entry!r} — skipping")
            failed.append(entry)
            continue

        # ── Step 2: Crawl the site ─────────────────────────────────────
        log.info(f"  Starting crawl from: {url}")
        t0 = time.time()
        try:
            products = await crawler.crawl_company(url, company_display)
        except Exception as e:
            log.error(f"  Crawl failed for {entry!r}: {e}")
            failed.append(entry)
            continue

        elapsed = time.time() - t0
        log.info(f"  Crawl done in {elapsed:.1f}s — {len(products)} product(s) found")
        all_products.extend(products)

    # ── Step 3: Write to Google Sheets ────────────────────────────────
    if all_products:
        log.info(f"\nWriting {len(all_products)} total products to Google Sheets...")
        try:
            sheet_url = writer.write(all_products)
            log.info(f"\nDone! Spreadsheet: {sheet_url}")
        except Exception as e:
            log.error(f"Failed to write to Google Sheets: {e}")
            sys.exit(1)
    else:
        log.warning("\nNo products found across all companies. Nothing written.")

    if failed:
        log.warning(f"\nFailed to resolve/crawl {len(failed)} entries:")
        for f in failed:
            log.warning(f"  - {f}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Products Crawler — extract product data from company websites into Google Sheets"
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to config file (default: config.yaml)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    if args.debug:
        setup_logging(logging.DEBUG)

    log = logging.getLogger("crawler")

    # Load and validate config
    try:
        cfg = Config(args.config)
        cfg.validate()
    except (FileNotFoundError, ValueError) as e:
        log.error(f"Configuration error: {e}")
        sys.exit(1)

    asyncio.run(run(cfg))


if __name__ == "__main__":
    main()
