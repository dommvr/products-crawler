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
from src.crawler import Crawler, dedup_across_companies
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

    # Crawl several companies at once (each gets its own browser). A semaphore caps
    # how many run simultaneously so we don't overwhelm OpenAI rate limits / RAM.
    entries = [(i, e.strip()) for i, e in enumerate(names, 1) if e.strip()]
    concurrency = max(1, cfg.company_concurrency)
    company_sem = asyncio.Semaphore(concurrency)
    log.info(f"Crawling up to {concurrency} compan{'y' if concurrency == 1 else 'ies'} concurrently")

    async def process_company(i: int, entry: str) -> tuple[list, str | None]:
        # Use entry as company name (strip protocol/www for display)
        company_display = (
            entry.replace("https://", "").replace("http://", "").replace("www.", "").split("/")[0]
        )
        async with company_sem:
            log.info(f"[{i}/{len(entries)}] Processing: {entry!r} → company: {company_display!r}")

            # ── Step 1: Resolve to a working URL ──────────────────────────
            try:
                url = await resolve(entry, cfg)
            except Exception as e:
                log.error(f"  [{company_display}] URL resolution failed: {e}")
                return [], entry
            if not url:
                log.error(f"  [{company_display}] Could not find a working URL — skipping")
                return [], entry

            # ── Step 2: Crawl the site ─────────────────────────────────────
            log.info(f"  [{company_display}] Starting crawl from: {url}")
            t0 = time.time()
            try:
                products = await crawler.crawl_company(url, company_display)
            except Exception as e:
                log.error(f"  [{company_display}] Crawl failed: {e}")
                return [], entry

            elapsed = time.time() - t0
            log.info(f"  [{company_display}] Crawl done in {elapsed:.1f}s — {len(products)} product(s) found")
            return products, None

    results = await asyncio.gather(*(process_company(i, e) for i, e in entries))
    for products, failed_entry in results:
        all_products.extend(products)
        if failed_entry:
            failed.append(failed_entry)

    # Global dedup: when names.txt lists several sub-sections of the SAME site, each pass
    # crawls the whole domain and rediscovers the same products. Collapse those cross-pass
    # duplicates (per company + product name) so each product appears once in the output.
    before = len(all_products)
    all_products = dedup_across_companies(all_products)
    if before != len(all_products):
        log.info(f"Global dedup: {before} → {len(all_products)} rows (collapsed cross-entry duplicates)")

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
