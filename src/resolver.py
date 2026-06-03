"""
resolver.py — resolves a company name or bare URL to a confirmed working URL.

Flow:
  1. If the entry looks like a URL → verify it's reachable → return it.
  2. If not → search via Serper for "<name> <category_hint> official site"
     → try each result URL until one is reachable and not on the skip list.
"""

from __future__ import annotations

import asyncio
import json
import logging

import httpx

from src.config import Config
from src.utils import is_skippable, looks_like_url, logger, normalise_url

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}


async def _is_reachable(url: str, timeout: float = 10.0) -> bool:
    """HEAD request to check if URL responds (falls back to GET)."""
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=timeout,
            headers=HEADERS,
        ) as client:
            resp = await client.head(url)
            if resp.status_code < 400:
                return True
            # Some servers reject HEAD — try GET
            resp = await client.get(url)
            return resp.status_code < 400
    except Exception:
        return False


async def _search_serper(query: str, api_key: str, n: int = 5) -> list[str]:
    """Call Serper.dev and return a list of result URLs."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                "https://google.serper.dev/search",
                headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
                content=json.dumps({"q": query, "num": n, "gl": "pl", "hl": "pl"}),
            )
            data = resp.json()
            urls: list[str] = []
            for item in data.get("organic", []):
                link = item.get("link", "")
                if link:
                    urls.append(link)
            return urls
    except Exception as e:
        logger.warning(f"Serper search failed: {e}")
        return []


async def resolve(
    entry: str,
    cfg: Config,
) -> str | None:
    """
    Resolve one entry (name or URL) to a working URL.
    Returns the URL string or None if nothing could be found.
    """
    entry = entry.strip()
    log = logging.getLogger("crawler")

    # ── Case 1: looks like a URL ──────────────────────────────────────
    if looks_like_url(entry):
        url = normalise_url(entry)
        if is_skippable(url, cfg.skip_domains):
            log.warning(f"  Skipping (on skip list): {url}")
            return None
        log.info(f"  Checking URL directly: {url}")
        if await _is_reachable(url):
            log.info(f"  Confirmed reachable: {url}")
            return url
        log.warning(f"  URL not reachable, falling through to search: {url}")
        # Fall through and use the domain as a search hint
        name_for_search = entry

    else:
        name_for_search = entry

    # ── Case 2: search for the company website ────────────────────────
    hint = cfg.company_category_hint or ""
    query = f"{name_for_search} {hint} official site".strip()
    log.info(f"  Searching: {query!r}")

    urls = await _search_serper(query, cfg.search_api_key, n=cfg.search_results_to_try + 5)

    tried = 0
    for url in urls:
        if tried >= cfg.search_results_to_try:
            break
        if is_skippable(url, cfg.skip_domains):
            log.debug(f"  Skipping search result (skip list): {url}")
            continue
        tried += 1
        log.info(f"  Trying search result [{tried}]: {url}")
        if await _is_reachable(url):
            log.info(f"  Resolved to: {url}")
            return url
        log.warning(f"  Not reachable: {url}")

    log.error(f"  Could not resolve a working URL for: {entry!r}")
    return None
