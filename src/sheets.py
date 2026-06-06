"""
sheets.py — writes extracted products to Google Sheets and/or CSV.

Modes:
  new       → creates a brand-new spreadsheet each run (name gets a timestamp suffix)
  append    → finds existing sheet by name and appends rows
  overwrite → finds existing sheet by name, clears it, rewrites from scratch

If Google Sheets write fails (e.g. quota exceeded), automatically falls back
to saving a local CSV so your data is never lost.
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
    "confidence": 5,
}

# Column index (0-based) of "Contains Natural Fiber" in Product.to_row()
_FIBER_COL_IDX = 6

# How many rows to write per API batch (Sheets API limit is 2MB per request)
BATCH_SIZE = 500


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
        Returns the spreadsheet URL, or empty string on failure (CSV fallback triggered).
        """
        if not products:
            logger.warning("No products to write.")
            return ""

        rows = _prepare_rows(products, self.cfg.sort_by)
        header = Product.headers()

        # Always save CSV backup if configured, or as fallback
        csv_path = self._write_csv(rows, header) if self.cfg.save_csv_backup else None

        try:
            url = self._write_sheets(rows, header)
            return url
        except gspread.exceptions.APIError as e:
            error_str = str(e)
            if "storageQuotaExceeded" in error_str or "403" in error_str:
                logger.error(
                    "Google Sheets write failed: Drive storage quota exceeded.\n"
                    "  → Your Google account's 15GB free storage is full.\n"
                    "  → Fix: Go to drive.google.com and free up space, or upgrade storage.\n"
                    "  → Saving results to local CSV instead..."
                )
            else:
                logger.error(f"Google Sheets API error: {e}\n  → Saving to CSV instead...")
            # Force CSV save as fallback even if not configured
            if csv_path is None:
                csv_path = self._write_csv(rows, header)
            logger.info(f"  ✓ Data saved to CSV: {csv_path}")
            return ""
        except Exception as e:
            logger.error(f"Google Sheets write failed: {e}\n  → Saving to CSV instead...")
            if csv_path is None:
                csv_path = self._write_csv(rows, header)
            logger.info(f"  ✓ Data saved to CSV: {csv_path}")
            return ""

    def _write_sheets(self, rows: list[list[Any]], header: list[str]) -> str:
        """Internal: write to Google Sheets, returns URL."""
        gc = self._client()
        mode = self.cfg.sheet_mode
        tab = self.cfg.sheet_tab
        all_rows = [header] + rows

        if mode == "new":
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            doc_name = f"{self.cfg.spreadsheet_name}_{ts}"
            sh = gc.create(doc_name)
            logger.info(f"  Created new spreadsheet: {doc_name}")
            ws = sh.get_worksheet(0)
            ws.update_title(tab)
            _batch_write(ws, all_rows)
            _apply_fiber_highlighting(ws, rows)

        elif mode == "append":
            sh = _find_or_create(gc, self.cfg.spreadsheet_name)
            ws = _get_or_create_tab(sh, tab)
            existing = ws.get_all_values()
            if not existing:
                _batch_write(ws, all_rows)
                _apply_fiber_highlighting(ws, rows)
            else:
                _batch_append(ws, rows)
                # Highlight only the newly-appended rows
                offset = len(existing)  # existing rows including header
                _apply_fiber_highlighting(ws, rows, row_offset=offset)
            logger.info(f"  Appended {len(rows)} rows to {self.cfg.spreadsheet_name}")

        elif mode == "overwrite":
            sh = _find_or_create(gc, self.cfg.spreadsheet_name)
            ws = _get_or_create_tab(sh, tab)
            ws.clear()
            _clear_all_formatting(ws)  # remove old green highlights before rewriting
            _batch_write(ws, all_rows)
            _apply_fiber_highlighting(ws, rows)
            logger.info(f"  Overwrote {self.cfg.spreadsheet_name} with {len(rows)} rows")

        else:
            raise ValueError(f"Unknown sheet_mode: {mode!r} — use 'new', 'append', or 'overwrite'")

        # Make readable by anyone with the link
        try:
            sh.share(None, perm_type="anyone", role="reader")
        except Exception:
            pass

        url = f"https://docs.google.com/spreadsheets/d/{sh.id}"
        logger.info(f"  Spreadsheet URL: {url}")
        return url

    def _write_csv(self, rows: list[list[Any]], header: list[str]) -> Path:
        """Write a local CSV file. Returns the path."""
        out_dir = Path(self.cfg.csv_output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        csv_path = out_dir / f"{self.cfg.spreadsheet_name}_{ts}.csv"
        with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(header)
            writer.writerows(rows)
        logger.info(f"  CSV saved: {csv_path}")
        return csv_path


# ── Helpers ───────────────────────────────────────────────────────────────────

def _prepare_rows(products: list[Product], sort_by: str) -> list[list[Any]]:
    rows = [p.to_row() for p in products]
    sort_idx = SORT_FIELD_MAP.get(sort_by, 0)
    rows.sort(key=lambda r: str(r[sort_idx]).lower())
    return rows


def _batch_write(ws: gspread.Worksheet, rows: list[list[Any]]) -> None:
    """Write rows in batches to avoid Sheets API payload limits."""
    for i in range(0, len(rows), BATCH_SIZE):
        batch = rows[i : i + BATCH_SIZE]
        start_row = i + 1
        ws.update(
            f"A{start_row}",
            batch,
            value_input_option="RAW",
        )


def _batch_append(ws: gspread.Worksheet, rows: list[list[Any]]) -> None:
    for i in range(0, len(rows), BATCH_SIZE):
        ws.append_rows(rows[i : i + BATCH_SIZE], value_input_option="RAW")


def _find_or_create(gc: gspread.Client, name: str) -> gspread.Spreadsheet:
    try:
        return gc.open(name)
    except gspread.SpreadsheetNotFound:
        sh = gc.create(name)
        logger.info(f"  Created new spreadsheet: {name}")
        return sh


def _get_or_create_tab(sh: gspread.Spreadsheet, tab_name: str) -> gspread.Worksheet:
    try:
        return sh.worksheet(tab_name)
    except gspread.WorksheetNotFound:
        return sh.add_worksheet(title=tab_name, rows=1000, cols=20)


def _clear_all_formatting(ws: gspread.Worksheet) -> None:
    """Remove all user-applied cell formatting from the worksheet (values are untouched).

    gspread's ws.clear() wipes values but leaves formatting intact, so old green
    highlights survive an overwrite. This call resets formatting to the sheet default.
    """
    try:
        ws.spreadsheet.batch_update({
            "requests": [{
                "updateCells": {
                    "range": {"sheetId": ws.id},
                    "fields": "userEnteredFormat",
                }
            }]
        })
    except Exception as e:
        logger.warning(f"  Could not clear old cell formatting: {e}")


def _apply_fiber_highlighting(
    ws: gspread.Worksheet,
    rows: list[list[Any]],
    row_offset: int = 1,  # 1 = skip the header row (default for new/overwrite)
) -> None:
    """Apply a light-green background to every row where Contains Natural Fiber == 'yes'.

    row_offset: the 0-based sheet row index where `rows[0]` starts.
                Default 1 = directly after the header.
                For append mode pass len(existing_rows) so we target only new rows.
    """
    if not rows:
        return

    green = {"red": 0.714, "green": 0.882, "blue": 0.714}  # #B6E1B6
    n_cols = len(rows[0])

    requests = []
    for i, row in enumerate(rows):
        if len(row) > _FIBER_COL_IDX and str(row[_FIBER_COL_IDX]).lower() == "yes":
            sheet_row = row_offset + i  # 0-based sheet row index
            requests.append({
                "repeatCell": {
                    "range": {
                        "sheetId": ws.id,
                        "startRowIndex": sheet_row,
                        "endRowIndex": sheet_row + 1,
                        "startColumnIndex": 0,
                        "endColumnIndex": n_cols,
                    },
                    "cell": {
                        "userEnteredFormat": {"backgroundColor": green}
                    },
                    "fields": "userEnteredFormat.backgroundColor",
                }
            })

    if not requests:
        return

    try:
        ws.spreadsheet.batch_update({"requests": requests})
        logger.info(f"  Highlighted {len(requests)} row(s) with natural fiber content (green)")
    except Exception as e:
        logger.warning(f"  Could not apply row highlighting: {e}")
