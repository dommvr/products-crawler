"""
crawler.py — Playwright-based website crawler.

Key behaviours:
- PRIORITY QUEUE: product/category URLs go to the front, irrelevant ones to the back.
- SMART START: if start URL is a subcategory (e.g. /materace-piankowe), crawl the
  parent (/materace) first to discover all sibling categories, then queue them.
- EARLY STOP: if the last N consecutive non-product pages exceeds a threshold, stop.
- PDF/DOWNLOAD SKIP: skip .pdf, .zip, .docx etc. without retrying.
- PAGINATION: follows page=2, page=3 etc. automatically.
- SIZE VARIANTS: clicks size selectors and re-extracts when content changes.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import deque
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from playwright.async_api import (
    async_playwright,
    Page,
    BrowserContext,
    TimeoutError as PWTimeout,
)

from src.config import Config
from src.extractor import Extractor, Product
from src.utils import get_root_domain, is_skippable, logger, same_site

# ── Constants ────────────────────────────────────────────────────────────────

# File extensions to skip entirely — don't even attempt loading
SKIP_EXTENSIONS = re.compile(
    r"\.(pdf|zip|docx|xlsx|pptx|rar|7z|tar|gz|mp4|mp3|avi|mov|exe|dmg|apk)(\?.*)?$",
    re.IGNORECASE,
)

# URL patterns that strongly suggest product listings or category pages (HIGH priority).
# Intentionally limited to mattress-specific terms + generic shop/product/category words.
# Bedroom furniture (beds, pillows, duvets) is excluded so we don't waste page budget on it.
HIGH_PRIORITY_PATTERNS = re.compile(
    r"/(kategori|category|kolekcj|collection|produkt|product|sklep|shop"
    r"|materac|mattress|piank|sprezy|spręż|topper|topmat)",
    re.IGNORECASE,
)

# URL patterns that strongly suggest NON-product pages (LOW priority / skip).
# Includes common bedroom non-mattress sections to avoid crawling beds, pillows, duvets, sheets.
LOW_PRIORITY_PATTERNS = re.compile(
    r"/(login|logowanie|konto|account|basket|koszyk|checkout|platnosc|payment"
    r"|customer|register|rejestracja|newsletter|gazetki|wyprzedaz(?!/.*materac)"
    r"|znajdz-sklep|find-store|careers|kariera|b2b"
    r"|customer-service|kontakt|contact|about|o-nas|regulamin|privacy"
    r"|faq|news|aktualnosci|press|media|sitemap|vr-|do-pobrania"
    r"|wp-content|wp-admin|feed|rss|projekt-unijny"
    r"|lozka|lozko|lozek|ramy-lozek|stelaz|zaglowek|nogi-do-lozek|akcesoria-do-lozek"
    r"|lozka-goscinne|lozka-dzieciece|lozka-kontynentalne|lozka-pietrowe"
    r"|koldry|koldra|poduszki|poduszka|posciel|przescieradl|ochraniacze"
    r"|fotel|sofa|kanapa|sypialnia(?!/.*materac)"
    r"|ogrod|garden|kemping|dmuchane"
    r"|wyrejestrowac|zmienic-moj-adres|porady|aktualnosci|inspiration|guide|blog)",
    re.IGNORECASE,
)

# URL path must contain one of these for a product to be relevant to the mattress fiber pass
MATTRESS_PATH_RE = re.compile(
    r"/(materac|mattress|piank|sprezy|spręż|topper|topmat)",
    re.IGNORECASE,
)

# Pagination link patterns
PAGINATION_PATTERNS = re.compile(
    r"[?&](page|strona|p|pg)=\d+|/page/\d+|/strona/\d+",
    re.IGNORECASE,
)

# Resource types and hostnames to block at the network level (speeds up page loads)
BLOCKED_RESOURCE_TYPES = {"image", "media", "font", "stylesheet"}
BLOCKED_HOSTNAMES = re.compile(
    r"(google-analytics|googletagmanager|doubleclick|facebook\.net"
    r"|hotjar|segment\.io|mixpanel|amplitude|clarity\.ms"
    r"|cookielaw|cookiebot)",
    re.IGNORECASE,
)

# CSS selectors for size/variant controls
VARIANT_SELECTORS = [
    "select[name*='size']", "select[name*='Size']", "select[name*='variant']",
    ".size-selector option", ".variant-selector option",
    "[data-size]", "[data-variant]",
    ".product-size button", ".product-variant button",
    ".size-option", ".variant-option",
    "button[data-size]", "button.size",
    ".sizes a", ".sizes button",
    ".wymiary a", ".rozmiary a", ".rozmiary button",
]

# How many consecutive irrelevant pages before we stop crawling a site

class Crawler:
    def __init__(self, cfg: Config, extractor: Extractor) -> None:
        self.cfg = cfg
        self.extractor = extractor

    async def crawl_company(self, start_url: str, company_name: str) -> list[Product]:
        log = logging.getLogger("crawler")
        all_products: list[Product] = []
        visited: set[str] = set()
        base_domain = get_root_domain(start_url)

        # ── Priority queues ──────────────────────────────────────────
        # high_q: product/category URLs — processed first
        # low_q:  everything else — processed only when high_q is empty
        high_q: deque[str] = deque()
        low_q: deque[str] = deque()

        # ── Smart start: crawl parent category first ─────────────────
        # If start_url is a deep subcategory like /materace/materace-piankowe,
        # first queue the parent /materace to discover all sibling categories.
        parent_url = _parent_url(start_url)
        if parent_url and parent_url != start_url:
            log.info(f"  Smart start: queuing parent category first: {parent_url}")
            high_q.append(parent_url)
        high_q.append(start_url)

        pages_visited = 0
        consecutive_irrelevant = 0
        # Maps base_slug → first detail URL seen for that product.
        # Detail pages are NOT added to the main crawl queue — they are visited
        # in a separate fiber detection pass after the main crawl finishes.
        pending_fiber_urls: dict[str, str] = {}

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=self.cfg.headless)
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 900},
            )

            while (high_q or low_q) and (
                self.cfg.max_pages == 0 or pages_visited < self.cfg.max_pages
            ):
                # Always drain high priority queue first
                url = high_q.popleft() if high_q else low_q.popleft()
                norm_url = _strip_fragment(url)

                if norm_url in visited:
                    continue
                if _is_download(url):
                    log.debug(f"  Skipping download URL: {url}")
                    continue
                if is_skippable(url, self.cfg.skip_domains):
                    log.debug(f"  Skipping (skip list): {url}")
                    continue
                if not same_site(url, base_domain, self.cfg.follow_subdomains):
                    continue

                visited.add(norm_url)
                pages_visited += 1

                queue_status = f"H:{len(high_q)} L:{len(low_q)}"
                log.info(f"  [{pages_visited}/{self.cfg.max_pages or '∞'}] [{queue_status}] {url}")

                html, text = await self._load_page(context, url)
                if not html:
                    continue

                # ── Collect and classify all links from this page ─────
                new_links = _extract_links(html, url, base_domain, self.cfg.follow_subdomains)
                for link in new_links:
                    lnorm = _strip_fragment(link)
                    if lnorm in visited or _is_download(link):
                        continue
                    # Product detail pages are NOT added to the main crawl queue.
                    # We record the first URL per base slug (only for mattress-relevant
                    # paths) and visit them all in the dedicated fiber detection pass.
                    if _is_product_detail_url(link):
                        if MATTRESS_PATH_RE.search(urlparse(lnorm).path):
                            base = _base_product_slug(lnorm)
                            if base not in pending_fiber_urls:
                                pending_fiber_urls[base] = lnorm
                                log.debug(f"  Queued for fiber pass: {lnorm}")
                        continue  # keep out of main queue regardless of path
                    if _is_high_priority(link):
                        high_q.append(link)
                    elif not _is_low_priority(link):
                        low_q.append(link)
                    # Low priority links are just discarded — saves pages for real content

                # ── Extract products (always — no pre-check LLM call) ─
                # skip_fiber=True: category/listing pages are not reliable for fiber info.
                # Fiber detection only happens in the dedicated fiber pass below,
                # where we visit a single product detail page per product.
                products = await self.extractor.extract_products(
                    text, html, url, company_name, skip_fiber=True
                )

                if not products:
                    consecutive_irrelevant += 1
                    log.debug(
                        f"  No products extracted [{consecutive_irrelevant}/{self.cfg.irrelevant_page_stop_threshold} consecutive]"
                    )
                    # Early stop only once high priority queue is exhausted
                    if (
                        not high_q
                        and consecutive_irrelevant >= self.cfg.irrelevant_page_stop_threshold
                    ):
                        log.info(
                            f"  {self.cfg.irrelevant_page_stop_threshold} consecutive pages with no products "
                            f"and no high-priority URLs left — stopping crawl."
                        )
                        break
                else:
                    consecutive_irrelevant = 0
                    # ── Click size/variant selectors ──────────────────
                    if self.cfg.extract_size_variants:
                        variant_products = await self._extract_variants(
                            context, url, company_name
                        )
                        existing_names = {p.product_name for p in products}
                        for vp in variant_products:
                            if vp.product_name not in existing_names:
                                products.append(vp)
                                existing_names.add(vp.product_name)
                    log.info(f"  ✓ Found {len(products)} product(s)")
                    all_products.extend(products)

                if self.cfg.delay > 0:
                    await asyncio.sleep(self.cfg.delay)

            # ── Fiber detection pass ─────────────────────────────────
            # Visit one detail page per unique product to extract full material
            # descriptions. This pass runs after the main crawl and is not
            # subject to the max_pages limit — every product gets checked.
            if pending_fiber_urls:
                log.info(
                    f"  Fiber pass: checking {len(pending_fiber_urls)} product page(s) "
                    f"for coconut fiber / sisal..."
                )
                for fiber_idx, (base_slug, detail_url) in enumerate(pending_fiber_urls.items(), 1):
                    log.info(f"  [Fiber {fiber_idx}/{len(pending_fiber_urls)}] {detail_url}")
                    fhtml, ftext = await self._load_page(context, detail_url)
                    if not fhtml:
                        continue
                    fproducts = await self.extractor.extract_products(
                        ftext, fhtml, detail_url, company_name
                    )
                    if fproducts:
                        fiber_summary = [
                            f"{p.product_name}: {p.has_natural_fiber or '?'}"
                            for p in fproducts
                        ]
                        log.info(f"    → {', '.join(fiber_summary)}")
                        all_products.extend(fproducts)
                    if self.cfg.delay > 0:
                        await asyncio.sleep(self.cfg.delay)

            await browser.close()

        # Deduplicate by normalized product name (size + type-prefix stripped, lowercased).
        # Priority: records with actual fiber determination ("yes"/"no") beat unknown ("").
        # Among records with the same fiber-determined status, higher fiber_confidence wins.
        def _fiber_key(p: Product) -> tuple:
            has_info = 1 if p.has_natural_fiber in ("yes", "no") else 0
            return (has_info, p.fiber_confidence)

        seen_names: dict[str, Product] = {}
        for p in all_products:
            key = _normalize_product_name(p.product_name)
            if not key:
                continue
            if key not in seen_names or _fiber_key(p) > _fiber_key(seen_names[key]):
                seen_names[key] = p
        deduped = list(seen_names.values())

        log.info(f"  Total unique products for {company_name}: {len(deduped)}")
        return deduped

    async def _load_page(self, context: BrowserContext, url: str) -> tuple[str, str]:
        """Load page, return (html, text). Returns ('','') on failure. No retries for downloads."""

        async def _handle_route(route) -> None:
            req = route.request
            if (
                req.resource_type in BLOCKED_RESOURCE_TYPES
                or bool(BLOCKED_HOSTNAMES.search(req.url))
                or _is_download(req.url)
            ):
                await route.abort()
            else:
                await route.continue_()

        for attempt in range(1, self.cfg.max_retries + 1):
            page: Page | None = None
            try:
                page = await context.new_page()
                # Block images, fonts, CSS, media, and tracking scripts for speed
                await page.route("**/*", _handle_route)
                await page.goto(
                    url, wait_until="domcontentloaded", timeout=self.cfg.page_timeout
                )
                await asyncio.sleep(1.5)
                html = await page.content()
                text = _html_to_text(html)
                return html, text
            except PWTimeout:
                logger.warning(f"  Timeout [{attempt}/{self.cfg.max_retries}]: {url}")
            except Exception as e:
                err = str(e)
                # Don't retry download-triggered errors
                if "Download is starting" in err or "net::ERR_ABORTED" in err:
                    logger.debug(f"  Skipping download: {url}")
                    return "", ""
                logger.warning(f"  Error [{attempt}/{self.cfg.max_retries}]: {err[:120]}")
            finally:
                if page:
                    try:
                        await page.close()
                    except Exception:
                        pass
            if attempt < self.cfg.max_retries:
                await asyncio.sleep(2)
        return "", ""

    async def _extract_variants(
        self,
        context: BrowserContext,
        url: str,
        company_name: str,
    ) -> list[Product]:
        """Click each size/variant option and extract products from changed states."""
        variant_products: list[Product] = []
        page: Page | None = None
        try:
            page = await context.new_page()
            await page.goto(
                url, wait_until="domcontentloaded", timeout=self.cfg.page_timeout
            )
            await asyncio.sleep(1.5)

            for selector in VARIANT_SELECTORS:
                try:
                    elements = await page.query_selector_all(selector)
                    if not elements:
                        continue
                    for el in elements:
                        try:
                            before_html = await page.content()
                            tag = await el.evaluate("e => e.tagName.toLowerCase()")
                            if tag == "option":
                                parent = await el.evaluate_handle("e => e.closest('select')")
                                value = await el.get_attribute("value")
                                if value:
                                    await parent.select_option(value=value)
                            else:
                                await el.click()
                            await asyncio.sleep(0.8)
                            after_html = await page.content()
                            if _content_changed(before_html, after_html):
                                after_text = _html_to_text(after_html)
                                new_prods = await self.extractor.extract_products(
                                    after_text, after_html, url, company_name
                                )
                                variant_products.extend(new_prods)
                        except Exception:
                            continue
                    if variant_products:
                        break
                except Exception:
                    continue
        except Exception as e:
            logger.debug(f"  Variant extraction error: {e}")
        finally:
            if page:
                try:
                    await page.close()
                except Exception:
                    pass
        return variant_products


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parent_url(url: str) -> str | None:
    """
    Return the parent path URL, e.g.:
    https://jysk.pl/sypialnia/materace/materace-piankowe → https://jysk.pl/sypialnia/materace
    Returns None if already at root.
    """
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    if not path or path == "/":
        return None
    parent_path = "/".join(path.split("/")[:-1]) or "/"
    return parsed._replace(path=parent_path, query="", fragment="").geturl()


def _is_download(url: str) -> bool:
    """True if URL points to a downloadable file."""
    return bool(SKIP_EXTENSIONS.search(url.split("?")[0]))


def _is_high_priority(url: str) -> bool:
    """True if URL looks like a product or category page."""
    path = urlparse(url).path
    return bool(HIGH_PRIORITY_PATTERNS.search(path)) or bool(
        PAGINATION_PATTERNS.search(url)
    )


def _is_low_priority(url: str) -> bool:
    """True if URL is likely a non-product utility page."""
    path = urlparse(url).path
    return bool(LOW_PRIORITY_PATTERNS.search(path))


def _is_product_detail_url(url: str) -> bool:
    """True if URL looks like an individual product page rather than a category listing.

    Two detection methods:
    1. /produkty/slug or /products/slug path pattern (janpol-style short slugs)
    2. Long last segment (>20 chars, ≥2 dashes) — jysk-style e.g.
       /materace-piankowe/materac-piankowy-80x200cm-hulda-twardy
       /topmaterace/topmaterac-80x200cm-marren  (only 2 dashes!)

    Pagination URLs are always excluded.
    """
    if PAGINATION_PATTERNS.search(url):
        return False
    path = urlparse(url).path.rstrip("/")
    segments = [s for s in path.split("/") if s]
    if len(segments) < 2:
        return False
    # Method 1: explicit product-namespace segment
    if segments[-2].lower() in ("produkty", "products", "product", "produkt", "produkts"):
        return True
    # Method 2: long, dash-rich slug
    last = segments[-1]
    return len(last) > 20 and last.count("-") >= 2


# Matches dimension patterns inside product name / URL slug
_SIZE_RE = re.compile(
    r"\b\d{2,3}\s*[xX×]\s*\d{2,3}\s*(?:cm)?\b"   # 80x200, 80x200cm
    r"|\b\d{2,3}\s*cm\b",                           # 180cm
    re.IGNORECASE,
)


def _base_product_slug(url: str) -> str:
    """Return a size-agnostic key for a product detail URL.

    Strips dimension patterns from the last path segment so that different
    size variants of the same product collapse to the same key, e.g.:
        /materace/materac-hulda-80x200cm-twardy
        /materace/materac-hulda-160x200cm-twardy
    Both return: "materace/materac-hulda-twardy"
    """
    path = urlparse(url).path.rstrip("/")
    segments = [s for s in path.split("/") if s]
    if not segments:
        return url
    last = segments[-1]
    base = _SIZE_RE.sub("", last)
    base = re.sub(r"-{2,}", "-", base).strip("-")
    parent = "/".join(segments[:-1])
    return f"{parent}/{base}" if parent else base


# Known type-descriptor words that can follow "Materac" in a product name.
# These are stripped AFTER the "Materac/Topmaterac" prefix so that e.g.
# "Materac piankowy HULDA" → "HULDA" (same key as "Materac HULDA").
# Only explicitly listed words are removed — arbitrary brand words (WELLPUR, etc.) are kept.
_TYPE_DESCRIPTOR_RE = re.compile(
    r"^\s*(?:piankow[yi]|sprezynow[yi]|sprężynow[yi]|rehabilitacyjn[yi]"
    r"|kieszeniow[yi]|lateksow[yi]|skladan[yi]|szpitaln[yi]|hybrydow[yi]"
    r"|wielostrefow[yi]|nawierzchniow[yi]|poslaniow[yi]|posłaniow[yi]"
    r"|dziecięc[yi]|niemowlęc[yi]|termoelastyczn[yi])\s+",
    re.IGNORECASE | re.UNICODE,
)


def _normalize_product_name(name: str) -> str:
    """Strip size patterns and product-type prefixes, then lowercase, for deduplication.

    Uses a two-step approach to avoid incorrectly stripping brand words:
      Step 1 — strip the bare "Materac " / "Topmaterac " prefix only.
               OLD bug: the previous regex matched 0–2 words after the prefix with
               re.IGNORECASE, which also matched uppercase brand names like "WELLPUR",
               causing "Materac WELLPUR GLOMMA" → "glomma" but "WELLPUR GLOMMA" →
               "wellpur glomma" (different keys → both survived dedup).
      Step 2 — strip a single known type-descriptor word (piankowy, sprężynowy, …)
               so "Materac piankowy HULDA" and "Materac HULDA" collapse to "hulda".

    Examples:
      "MARREN"                  → "marren"
      "Topmaterac MARREN"       → "marren"
      "Materac HULDA"           → "hulda"
      "Materac piankowy HULDA"  → "hulda"
      "WELLPUR GULEN"           → "wellpur gulen"
      "Topmaterac WELLPUR GULEN"→ "wellpur gulen"   (WELLPUR kept — not a type word)
      "WELLPUR GLOMMA"          → "wellpur glomma"
      "Materac WELLPUR GLOMMA"  → "wellpur glomma"  (matches previous)
    """
    normalized = _SIZE_RE.sub("", name)
    # Step 1: strip leading "Materac " or "Topmaterac " — just the prefix word, nothing else
    normalized = re.sub(
        r"^\s*(?:topmaterac|materac)\s+",
        "",
        normalized,
        flags=re.IGNORECASE | re.UNICODE,
    )
    # Step 2: strip one optional known type-descriptor word (piankowy, sprężynowy, etc.)
    normalized = _TYPE_DESCRIPTOR_RE.sub("", normalized)
    return re.sub(r"\s+", " ", normalized).strip().lower()


def _extract_links(
    html: str, base_url: str, base_domain: str, follow_subdomains: bool
) -> list[str]:
    """Extract all href links from HTML, returning same-site URLs only."""
    soup = BeautifulSoup(html, "lxml")
    links: list[str] = []
    for tag in soup.find_all("a", href=True):
        href = tag["href"].strip()
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        full_url = urljoin(base_url, href)
        full_url = _strip_fragment(full_url)
        if not full_url.startswith(("http://", "https://")):
            continue
        if same_site(full_url, base_domain, follow_subdomains):
            links.append(full_url)
    return links


def _strip_fragment(url: str) -> str:
    return url.split("#")[0].rstrip("/") or url


def _html_to_text(html: str) -> str:
    """Convert HTML to clean readable text."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "header", "footer", "nav", "aside"]):
        tag.decompose()
    text = soup.get_text(separator="\n", strip=True)
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    return "\n".join(lines)


def _content_changed(before: str, after: str, threshold: int = 50) -> bool:
    """True if page content changed meaningfully (product description updated)."""
    before_text = _html_to_text(before)
    after_text = _html_to_text(after)
    if abs(len(before_text) - len(after_text)) > threshold:
        return True
    dim_re = re.compile(r"\d{2,3}\s*[xX×]\s*\d{2,3}")
    return set(dim_re.findall(before_text)) != set(dim_re.findall(after_text))
