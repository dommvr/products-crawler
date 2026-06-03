"""
sheets.py — writes extracted products to Google Sheets (and optionally CSV).

Modes:
  new       → creates a brand-new spreadsheet each run (name gets a timestamp suffix)
  append    → finds existing sheet by name and appends rows
  overwrite → finds existing sheet by name, clears it, rewrites from scratch
"""

from __future__ import annotations

import csv
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import gspread
from google.oauth2.service_account import Credentials

from src.config import Config
from src.extractor import Product

logger = logging.getLogger("crawler")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

SORT_FIELD_MAP = {
    "company_name": 0,
    "category": 1,
    "product_name": 2,
    "confidence": 6,
}


class SheetsWriter:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._gc: gspread.Client | None = None

    def _client(self) -> gspread.Client:
        if self._gc is None:
            creds = Credentials.from_service_account_file(
                self.cfg.credentials_file, scopes=SCOPES
            )
            self._gc = gspread.authorize(creds)
        return self._gc

    def write(self, products: list[Product]) -> str:
        """
        Write products to Google Sheets.
        Returns the URL of the spreadsheet.
        """
        if not products:
            logger.warning("No products to write.")
            return ""

        rows = _prepare_rows(products, self.cfg.sort_by)
        header = Product.headers()
        all_rows = [header] + rows

        gc = self._client()
        mode = self.cfg.sheet_mode
        tab = self.cfg.sheet_tab

        if mode == "new":
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            doc_name = f"{self.cfg.spreadsheet_name}_{ts}"
            sh = gc.create(doc_name)
            logger.info(f"  Created new spreadsheet: {doc_name}")
            ws = sh.get_worksheet(0)
            ws.update_title(tab)
            ws.update(all_rows, value_input_option="RAW")

        elif mode == "append":
            sh = _find_or_create(gc, self.cfg.spreadsheet_name)
            ws = _get_or_create_tab(sh, tab)
            existing = ws.get_all_values()
            if not existing:
                ws.update(all_rows, value_input_option="RAW")
            else:
                ws.append_rows(rows, value_input_option="RAW")
            logger.info(f"  Appended {len(rows)} rows to {self.cfg.spreadsheet_name}")

        elif mode == "overwrite":
            sh = _find_or_create(gc, self.cfg.spreadsheet_name)
            ws = _get_or_create_tab(sh, tab)
            ws.clear()
            ws.update(all_rows, value_input_option="RAW")
            logger.info(f"  Overwrote {self.cfg.spreadsheet_name} with {len(rows)} rows")

        else:
            raise ValueError(f"Unknown sheet_mode: {mode!r} — use 'new', 'append', or 'overwrite'")

        # Make sheet accessible to anyone with the link (view only)
        try:
            sh.share(None, perm_type="anyone", role="reader")
        except Exception:
            pass  # Not critical

        url = f"https://docs.google.com/spreadsheets/d/{sh.id}"
        logger.info(f"  Spreadsheet URL: {url}")

        if self.cfg.save_csv_backup:
            self._write_csv(rows, header)

        return url

    def _write_csv(self, rows: list[list[Any]], header: list[str]) -> None:
        out_dir = Path(self.cfg.csv_output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        csv_path = out_dir / f"{self.cfg.spreadsheet_name}_{ts}.csv"
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(header)
            writer.writerows(rows)
        logger.info(f"  CSV backup saved: {csv_path}")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _prepare_rows(products: list[Product], sort_by: str) -> list[list[Any]]:
    """Convert products to rows and sort."""
    rows = [p.to_row() for p in products]
    sort_idx = SORT_FIELD_MAP.get(sort_by, 0)
    rows.sort(key=lambda r: str(r[sort_idx]).lower())
    return rows


def _find_or_create(gc: gspread.Client, name: str) -> gspread.Spreadsheet:
    """Find an existing spreadsheet by name, or create it."""
    try:
        return gc.open(name)
    except gspread.SpreadsheetNotFound:
        sh = gc.create(name)
        logger.info(f"  Created new spreadsheet: {name}")
        return sh


def _get_or_create_tab(sh: gspread.Spreadsheet, tab_name: str) -> gspread.Worksheet:
    """Return the named worksheet, creating it if it doesn't exist."""
    try:
        return sh.worksheet(tab_name)
    except gspread.WorksheetNotFound:
        return sh.add_worksheet(title=tab_name, rows=1000, cols=20)
