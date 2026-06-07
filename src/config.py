"""
config.py — loads config.yaml and merges environment variable overrides.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

# Load .env from project root (no-op if file doesn't exist; never overwrites existing env vars)
load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def _deep_get(d: dict, *keys: str, default: Any = None) -> Any:
    """Safely walk nested dict keys."""
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k, default)
    return cur


class Config:
    """Single config object passed throughout the app."""

    def __init__(self, path: str | Path = "config.yaml") -> None:
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {path}")

        with path.open("r", encoding="utf-8") as f:
            raw: dict = yaml.safe_load(f) or {}

        # ── Input ──────────────────────────────────────────────────
        self.names_file: str = _deep_get(raw, "input", "names_file", default="names.txt")
        self.company_category_hint: str = _deep_get(raw, "input", "company_category_hint", default="")

        # ── Products ───────────────────────────────────────────────
        cats = _deep_get(raw, "products", "categories", default=["materace"])
        if isinstance(cats, str):
            cats = [cats]
        self.categories: list[str] = [c.strip() for c in cats if c.strip()]
        self.extract_size_variants: bool = _deep_get(raw, "products", "extract_size_variants", default=True)

        # ── Crawling ───────────────────────────────────────────────
        self.irrelevant_page_stop_threshold: int = _deep_get(
            raw, "crawling", "irrelevant_page_stop_threshold", default=5
        )
        self.max_pages: int = _deep_get(raw, "crawling", "max_pages", default=20)
        self.delay: float = float(_deep_get(raw, "crawling", "delay_between_requests", default=2.0))
        self.follow_subdomains: bool = _deep_get(raw, "crawling", "follow_subdomains", default=True)
        self.max_retries: int = _deep_get(raw, "crawling", "max_retries", default=3)
        self.search_results_to_try: int = _deep_get(raw, "crawling", "search_results_to_try", default=3)
        self.headless: bool = _deep_get(raw, "crawling", "headless", default=True)
        self.page_timeout: int = int(_deep_get(raw, "crawling", "page_timeout", default=30)) * 1000  # ms
        # How many product detail pages to load/extract concurrently in the fiber pass.
        # The fiber pass is the slowest phase; raising this speeds it up almost linearly.
        # 6 is a good balance of speed vs. politeness / OpenAI rate limits.
        self.fiber_concurrency: int = int(_deep_get(raw, "crawling", "fiber_pass_concurrency", default=6))
        # Seconds to wait after a page's DOM loads, to let client-side JS render content.
        # Lower = faster but risks missing late-rendered text. 0.8s works for these sites.
        self.render_wait: float = float(_deep_get(raw, "crawling", "render_wait", default=0.8))

        # ── LLM ────────────────────────────────────────────────────
        self.llm_model: str = _deep_get(raw, "llm", "model", default="gpt-4o-mini")
        self.llm_max_tokens: int = _deep_get(raw, "llm", "max_tokens", default=4000)
        self.llm_temperature: float = float(_deep_get(raw, "llm", "temperature", default=0.1))

        # ── Output ─────────────────────────────────────────────────
        self.spreadsheet_name: str = _deep_get(raw, "output", "spreadsheet_name", default="products_data")
        self.sheet_mode: str = _deep_get(raw, "output", "sheet_mode", default="new")
        self.sheet_tab: str = _deep_get(raw, "output", "sheet_tab", default="Products")
        self.sort_by: str = _deep_get(raw, "output", "sort_by", default="company_name")
        self.save_csv_backup: bool = _deep_get(raw, "output", "save_csv_backup", default=False)
        self.csv_output_dir: str = _deep_get(raw, "output", "csv_output_dir", default="outputs")

        # ── Google ─────────────────────────────────────────────────
        self.credentials_file: str = _deep_get(raw, "google", "credentials_file", default="credentials.json")

        # ── Search ─────────────────────────────────────────────────
        self.search_provider: str = _deep_get(raw, "search", "provider", default="serper")
        self.search_api_key: str = (
            os.environ.get("SERPER_API_KEY")
            or _deep_get(raw, "search", "api_key", default="")
        )

        # ── OpenAI ─────────────────────────────────────────────────
        self.openai_api_key: str = (
            os.environ.get("OPENAI_API_KEY")
            or _deep_get(raw, "openai", "api_key", default="")
        )

        # ── Skip list ──────────────────────────────────────────────
        default_skip = [
            "wikipedia.org", "linkedin.com", "facebook.com", "instagram.com",
            "twitter.com", "x.com", "youtube.com", "tiktok.com", "pinterest.com",
            "allegro.pl", "olx.pl", "ceneo.pl", "nokaut.pl", "skapiec.pl",
            "pricespy.pl", "opineo.pl", "google.com", "google.pl", "bing.com",
            "yahoo.com", "reddit.com", "quora.com", "amazon.com", "amazon.pl",
            "ebay.com",
        ]
        self.skip_domains: list[str] = _deep_get(raw, "skip", "domains", default=default_skip)

    def validate(self) -> None:
        """Raise ValueError for missing required values."""
        if not self.openai_api_key:
            raise ValueError(
                "OpenAI API key is missing. "
                "Set OPENAI_API_KEY env var or add it to config.yaml under openai.api_key"
            )
        if not self.search_api_key:
            raise ValueError(
                "Search API key is missing. "
                "Set SERPER_API_KEY env var or add it to config.yaml under search.api_key"
            )
        creds = Path(self.credentials_file)
        if not creds.exists():
            raise FileNotFoundError(
                f"Google credentials file not found: {creds}\n"
                "Follow the README to create a service account and download credentials.json"
            )
        names = Path(self.names_file)
        if not names.exists():
            raise FileNotFoundError(f"Names file not found: {names}")
