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
import json
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
    r"|znajdz-sklep|find-store|salon|do-domu|do-biura|careers|kariera|b2b"
    r"|customer-service|kontakt|contact|about|o-nas|regulamin|privacy"
    r"|faq|news|aktualnosci|press|media|sitemap|vr-|do-pobrania"
    r"|impressum|datenschutz|haendlersuche|handlersuche|materialkunde"  # DE info pages
    r"|gesund-schlafen|allergien|hygiene|widerruf|versandkosten"
    r"|wp-content|wp-admin|feed|rss|projekt-unijny"
    r"|lozka|lozko|lozek|ramy-lozek|stelaz|zaglowek|nogi-do-lozek|akcesoria-do-lozek"
    r"|lozka-goscinne|lozka-dzieciece|lozka-kontynentalne|lozka-pietrowe"
    r"|koldry|koldra|poduszki|poduszka|posciel|przescieradl|ochraniacze"
    r"|fotel|sofa|kanapa|sypialnia(?!/.*materac)"
    r"|moebel|mobel|meble|furniture"          # furniture sections (DE/PL/EN) — not mattresses
    r"|rekomendacj|realizacj|referencj"         # reference / showcase pages, not product lists
    r"|ogrod|garden|kemping|dmuchane"
    r"|wyrejestrowac|zmienic-moj-adres|porady|aktualnosci|inspiration|guide|blog)",
    re.IGNORECASE,
)

# URL path must contain one of these for a product to be relevant to the mattress fiber pass.
# No leading "/" anchor: German shops use compounds like "naturmatratzen" / "kindermatratze"
# where the mattress word is mid-slug, so we match the term anywhere in the path.
MATTRESS_PATH_RE = re.compile(
    r"(materac|matratze|mattress|piank|sprezy|spręż|topper|topmat)",
    re.IGNORECASE,
)

# Non-mattress accessory sections. Matched anywhere in the path (not just after a "/"),
# because some sites embed them mid-slug, e.g. "/karta-produktowa-stelazy". These are
# discarded BEFORE the high/low-priority test, so they win even though the slug also
# contains "produkt". NOTE: "topper" here drops accessory toppers (e.g. optimum's
# "karta-produktowa-topperow"); it does NOT match jysk's "topmaterace" mattress toppers.
ACCESSORY_EXCLUDE_PATTERNS = re.compile(
    r"(stelaz|stela[zż]|topper|nakladk|nakładk|nozki|nóżki|akcesori"
    r"|pokrowiec|pokrowc|poduszk|koldr|kołdr|posciel|pościel"
    r"|przescieradl|prześcieradł|ochraniacz|podkladk|podkładk"
    r"|zaglowek|zagłówek|wezglowie|wezgłowie|ramy-lozek|rama-lozka)",
    re.IGNORECASE,
)

# Path segments that mark a product-namespace (the product itself lives in a deeper
# segment). Includes German "produkte". Used by _is_product_detail_url Method 2.
_PRODUCT_NS_SEGMENTS = frozenset(
    {"produkty", "products", "product", "produkt", "produkts", "produkte", "p"}
)

# Two-letter language path prefixes used by site language switchers.
KNOWN_LANG_CODES = frozenset({
    "en", "de", "fr", "ru", "uk", "cs", "sk", "it", "es", "nl", "pt", "sv",
    "no", "da", "fi", "hu", "ro", "lt", "lv", "et", "bg", "hr", "sl", "el", "tr",
})

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


class _CompanyLogAdapter(logging.LoggerAdapter):
    """Prefixes log lines with the company name, e.g. "[jysk]  [1/20] ...".

    Keeps concurrent-company output traceable since their log lines interleave.
    """

    def process(self, msg, kwargs):
        return f"[{self.extra['company']}] {msg}", kwargs


class Crawler:
    def __init__(self, cfg: Config, extractor: Extractor) -> None:
        self.cfg = cfg
        self.extractor = extractor

    async def crawl_company(self, start_url: str, company_name: str) -> list[Product]:
        # Prefix every line with the company name so logs stay readable when several
        # companies are crawled concurrently and their output interleaves.
        log = _CompanyLogAdapter(logging.getLogger("crawler"), {"company": company_name})
        all_products: list[Product] = []
        visited: set[str] = set()
        base_domain = get_root_domain(start_url)
        # Language of the start URL (None = site default / no /xx/ prefix). We stay within
        # this language so a site's language switcher (/en/, /de/, …) doesn't make us crawl
        # every product several times in different languages.
        start_lang = _path_lang(start_url)

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
                # If THIS page is itself a mattress listing, its product-detail links are
                # mattresses even when their own slug has no mattress keyword (e.g.
                # grueneerde's /de-at/p/himmlische-wolke-premium/ge-p-50670).
                source_is_mattress = bool(MATTRESS_PATH_RE.search(urlparse(url).path))
                anchor_links, card_links = _extract_links(
                    html, url, base_domain, self.cfg.follow_subdomains
                )
                for link in anchor_links:
                    lnorm = _strip_fragment(link)
                    if lnorm in visited or _is_download(link):
                        continue
                    lpath = urlparse(lnorm).path
                    # Stay within the start URL's language — skip /en/, /de/, … duplicates.
                    if _path_lang(lnorm) not in (None, start_lang):
                        continue
                    # Skip non-mattress accessory sections (toppers, bed frames/slats,
                    # pillows, duvets, covers…) even when the URL also contains "produkt".
                    # Checked BEFORE priority so these exclusions win over generic matches.
                    if ACCESSORY_EXCLUDE_PATTERNS.search(lpath):
                        continue
                    # Product detail pages are NOT added to the main crawl queue.
                    # We record the first URL per base slug (only for mattress-relevant
                    # paths) and visit them all in the dedicated fiber detection pass.
                    # Skip detail-looking URLs that are really info/utility pages nested
                    # under a mattress-named folder (e.g. /produkte/<family>/kontakt) —
                    # they'd waste a fiber call and yield no product.
                    if _is_product_detail_url(link) and not _is_low_priority(link):
                        if source_is_mattress or MATTRESS_PATH_RE.search(urlparse(lnorm).path):
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

                # JS product-card links (data-* attributes) are clickable product cards,
                # not <a href>. Treat each as a product detail page: skip categories /
                # accessories / other-language / homepage, then queue for the fiber pass.
                # No MATTRESS_PATH_RE gate here — the card sits on a mattress page, so the
                # product is a mattress even if its slug is a bare name like "/rubin".
                for link in card_links:
                    lnorm = _strip_fragment(link)
                    if lnorm in visited:
                        continue
                    lpath = urlparse(lnorm).path
                    if not lpath.strip("/"):
                        continue  # homepage / root card (e.g. logo)
                    if _path_lang(lnorm) not in (None, start_lang):
                        continue
                    if ACCESSORY_EXCLUDE_PATTERNS.search(lpath):
                        continue
                    if _is_listing_url(lnorm) or _is_low_priority(lnorm):
                        continue  # category / index / info card, not a single product
                    base = _base_product_slug(lnorm)
                    if base not in pending_fiber_urls:
                        pending_fiber_urls[base] = lnorm
                        log.debug(f"  Queued card-link for fiber pass: {lnorm}")

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
                total = len(pending_fiber_urls)
                concurrency = max(1, self.cfg.fiber_concurrency)
                log.info(
                    f"  Fiber pass: checking {total} product page(s) for coconut fiber / sisal "
                    f"(concurrency={concurrency})..."
                )
                sem = asyncio.Semaphore(concurrency)
                progress = {"done": 0}

                async def _check_fiber(detail_url: str) -> Product | None:
                    # single_product=True: a detail page is about ONE product. Tell the
                    # model to ignore related / "you may also like" carousels so we don't
                    # mis-attribute this page's composition to other products shown on it.
                    async with sem:
                        fhtml, ftext = await self._load_page(context, detail_url)
                        if not fhtml:
                            result = None
                        else:
                            fproducts = await self.extractor.extract_products(
                                ftext, fhtml, detail_url, company_name, single_product=True
                            )
                            # Safety net against carousel leakage: keep only the single
                            # product whose name matches this detail page's URL slug.
                            result = _pick_primary_product(fproducts, detail_url) if fproducts else None
                    progress["done"] += 1
                    label = (
                        f"{result.product_name}: {result.has_natural_fiber or '?'}"
                        if result else "(no product extracted)"
                    )
                    log.info(f"  [Fiber {progress['done']}/{total}] {label}  ←  {detail_url}")
                    return result

                results = await asyncio.gather(
                    *(_check_fiber(u) for u in pending_fiber_urls.values())
                )
                all_products.extend(r for r in results if r is not None)

            await browser.close()

        # Deduplicate by normalized product name (size + type-prefix stripped, lowercased).
        # When the same product appears from a category page AND its own detail page,
        # the surviving record is chosen by this priority (highest wins):
        #   1. came from a direct product DETAIL URL   (so the kept row links to /produkt…)
        #   2. has an actual fiber determination "yes"/"no"  (beats undetermined "")
        #   3. higher fiber_confidence
        # Putting the detail-URL flag first guarantees a collapsed duplicate keeps the
        # specific product link, never the general /kategoria-produktu/ listing link.
        seen_names: dict[str, Product] = {}
        dropped_junk = 0
        for p in all_products:
            # Drop accessories / furniture / category-name / homepage-teaser pseudo-products
            # that slipped in from mixed listing pages.
            if _is_junk_product(p):
                dropped_junk += 1
                continue
            key = _normalize_product_name(p.product_name)
            if not key:
                continue
            if key not in seen_names or _product_quality_key(p) > _product_quality_key(seen_names[key]):
                seen_names[key] = p
        deduped = list(seen_names.values())
        if dropped_junk:
            log.info(f"  Dropped {dropped_junk} non-product row(s) (accessories/furniture/category names)")

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
                await asyncio.sleep(self.cfg.render_wait)
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


def _path_lang(url: str) -> str | None:
    """Return the language code if the path starts with a /xx/ language prefix, else None.

    e.g. https://optimum-materace.pl/en/products → "en"
         https://optimum-materace.pl/kategorie-materacow → None  (site default)
    """
    segments = [s for s in urlparse(url).path.split("/") if s]
    if segments and segments[0].lower() in KNOWN_LANG_CODES:
        return segments[0].lower()
    return None


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


def _is_listing_url(url: str) -> bool:
    """True if URL is a category / listing / index page rather than a single product."""
    return bool(_LISTING_URL_RE.search(urlparse(url).path))


def _is_product_detail_url(url: str) -> bool:
    """True if URL looks like an individual product page rather than a category listing.

    Three detection methods:
    1. Singular "materac-<name>" slug — a SINGLE product (optimum-style short slugs like
       /materac-diament, /materac-rehabilitacyjny-syriusz). Works even for single-segment
       paths. The plural "materace-…" (a category, e.g. /materace-piankowe) is NOT matched
       because it has no dash right after "materac".
    2. A product-namespace segment (produkt / produkty / products / produkte …) appears
       ANYWHERE before the last segment. Covers both janpol-style "/produkty/<slug>" and
       hilding-style "/produkt/<category>/<slug>" (namespace at -3), plus German "/produkte/".
    3. Long last segment (>20 chars, ≥2 dashes) — jysk-style e.g.
       /materace-piankowe/materac-piankowy-80x200cm-hulda-twardy
       /topmaterace/topmaterac-80x200cm-marren  (only 2 dashes!)

    Pagination URLs are always excluded.
    """
    if PAGINATION_PATTERNS.search(url):
        return False
    path = urlparse(url).path.rstrip("/")
    segments = [s for s in path.split("/") if s]
    if not segments:
        return False
    last = segments[-1]
    # Method 1: singular "materac-<name>" / "mattress-<name>" product slug
    low = last.lower()
    if low.startswith("materac-") or low.startswith("mattress-"):
        return True
    if len(segments) < 2:
        return False
    # Method 2: a product-namespace segment somewhere before the last segment.
    # "/produkt/<cat>/<slug>" (hilding) and "/produkte/<family>/<variant>" (lonsberg, DE)
    # both put the namespace at -3, not just -2, so scan all but the final segment.
    if any(seg.lower() in _PRODUCT_NS_SEGMENTS for seg in segments[:-1]):
        return True
    # Method 3: long, dash-rich slug
    return len(last) > 20 and last.count("-") >= 2


# Matches dimension patterns inside product name / URL slug
_SIZE_RE = re.compile(
    r"\b\d{2,3}\s*[xX×]\s*\d{2,3}\s*(?:cm)?\b"   # 80x200, 80x200cm
    r"|\b\d{2,3}\s*cm\b",                           # 180cm
    re.IGNORECASE,
)

# A single hyphen-token that is purely a dimension, e.g. "80x200cm", "60x190x7cm", "200".
_SIZE_TOKEN_RE = re.compile(r"^\d{1,3}(?:[xX×]\d{1,3})*(?:cm)?$", re.IGNORECASE)

# Firmness / colour / variant descriptor tokens. These distinguish variants of the
# SAME product (e.g. "twardy" vs "średni", "biały" vs "szary"), so they are dropped
# from the slug key — we only need to visit ONE detail page per real product in the
# fiber pass. The final name-based dedup would merge them anyway, so visiting every
# firmness/colour separately is wasted time.
_VARIANT_SLUG_TOKENS = frozenset({
    # firmness
    "twardy", "twarda", "twarde", "bardzo", "sredni", "srednia", "srednio",
    "średni", "średnia", "średnio", "sredniotwardy", "miekki", "miękki",
    "miekka", "miękka", "miekko", "miękko", "hard", "medium", "soft", "firm",
    # colours
    "bialy", "biała", "biala", "biały", "szary", "szara", "czarny", "czarna",
    "grafit", "grafitowy", "bezowy", "bezowa", "bezowy", "beżowy", "beżowa",
    "niebieski", "niebieska", "zielony", "zielona", "rozowy", "różowy",
    "czerwony", "brazowy", "brązowy", "kremowy", "white", "grey", "gray", "black",
})


def _base_product_slug(url: str) -> str:
    """Return a size-, firmness- and colour-agnostic key for a product detail URL.

    Collapses variants of the same product to one key so the fiber pass visits each
    product only once. Strips dimension tokens AND firmness/colour descriptor tokens
    from the last path segment, e.g. all of these map to "materace-piankowe/materac-piankowy-svinna":
        /materace-piankowe/materac-piankowy-80x200-svinna-twardy
        /materace-piankowe/materac-piankowy-80x200-svinna-sredni
        /materace-piankowe/materac-piankowy-80x200-svinna-bardzo-twardy
    and these map to "topmaterace/topmaterac-wellpur-gulen":
        /topmaterace/topmaterac-80x200cm-wellpur-gulen-szary
        /topmaterace/topmaterac-80x200cm-wellpur-gulen-bialy
    """
    path = urlparse(url).path.rstrip("/")
    segments = [s for s in path.split("/") if s]
    if not segments:
        return url
    last = _SIZE_RE.sub("-", segments[-1])
    tokens = [
        t for t in last.split("-")
        if t
        and not _SIZE_TOKEN_RE.match(t)
        and t.lower() not in _VARIANT_SLUG_TOKENS
    ]
    base = "-".join(tokens)
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


# Product NAMES that denote a non-mattress item (accessory / furniture). The LLM sometimes
# extracts these from listing pages that mix mattresses with related products. We drop them
# by name because they often have no detail page and would otherwise survive as junk rows.
# NOTE: "topper" matches German "Matratzentopper" but NOT jysk's wanted "Topmaterac"
# (which contains "topm", not "topp").
_NONPRODUCT_NAME_RE = re.compile(
    r"(nakladk|nakładk|ochraniacz|pokrowiec|pokrowc|poszewk|podkladk|podkładk"
    r"|stelaz|stela[zż]|n[oó]zki|n[oó]żki|zaglowek|zagłówek|wezglow|wezgłow"
    r"|matratzentopper|topper|unterbett|bettsofa|bettgestell|polsterbett|otoman|kielich"
    r"|kopfkissen|nackenkissen|\bkissen\b|decke\b|lattenrost|auflage|schlafmaske"
    r"|\bbett\b|\blozko\b|\blóżko\b|\błóżko\b|\bsofa\b|\bbaza\b)",
    re.IGNORECASE | re.UNICODE,
)

# Product NAMES that are really a category / family heading, not a single product.
# Plural "Materace …" / German "…matratzen" standing alone are category tiles the LLM
# misread as products (e.g. "Materace kokosowe", "Naturmatratzen").
_CATEGORY_NAME_RE = re.compile(
    r"^\s*(materace\b|natur.?matratzen\b|matratzen\b)",
    re.IGNORECASE | re.UNICODE,
)


def _is_junk_product(p: Product) -> bool:
    """True if an extracted 'product' is actually an accessory, furniture item, a bare
    category/family name, or a homepage teaser (URL has no path) — i.e. not a real mattress."""
    name = p.product_name or ""
    if _NONPRODUCT_NAME_RE.search(name):
        return True
    if _CATEGORY_NAME_RE.search(name):
        return True
    # Homepage / root URL teaser — a real product or listing always has a path segment.
    if not urlparse(p.url).path.strip("/"):
        return True
    # Extracted from a utility/landing/blog page (e.g. jysk /do-domu, /salon, /inspiration)
    # — those teaser tiles ("Materac KOKOS", "FLYA") are not real products.
    if _is_low_priority(p.url):
        return True
    return False


def _product_quality_key(p: Product) -> tuple:
    """Ranking for choosing which duplicate record to keep (higher wins):
      1. came from a direct product DETAIL URL  (kept row links to /produkt…, not /kategoria)
      2. has an actual fiber verdict "yes"/"no"  (beats undetermined "")
      3. higher fiber_confidence
    Putting the detail-URL flag first guarantees a collapsed duplicate keeps the specific
    product link, never the general category listing link."""
    is_detail = 1 if _is_product_detail_url(p.url) else 0
    has_info = 1 if p.has_natural_fiber in ("yes", "no") else 0
    return (is_detail, has_info, p.fiber_confidence)


def dedup_across_companies(products: list[Product]) -> list[Product]:
    """Collapse duplicates that appear across multiple crawl passes of the SAME company.

    When names.txt lists two sub-sections of one site (e.g. two lonsberg category URLs),
    each pass crawls the whole domain and rediscovers all products, so the same product is
    emitted by both passes. Per-company dedup inside crawl_company can't see across passes;
    this global pass does. Keyed by (company, normalized name) so different companies that
    happen to share a model name (e.g. jysk vs optimum "HULDA") are NOT merged. The kept
    record is the highest quality one per _product_quality_key."""
    best: dict[tuple, Product] = {}
    for p in products:
        name_key = _normalize_product_name(p.product_name)
        if not name_key:
            continue
        key = (p.company_name, name_key)
        if key not in best or _product_quality_key(p) > _product_quality_key(best[key]):
            best[key] = p
    return list(best.values())


# Generic words that are NOT distinctive product identifiers — ignored when matching
# a product name against a URL slug (they appear in almost every slug).
_GENERIC_NAME_TOKENS = frozenset({
    "materac", "topmaterac", "mattress", "piankowy", "piankowa", "sprezynowy",
    "sprężynowy", "sprezynowa", "rehabilitacyjny", "kieszeniowy", "lateksowy",
    "skladany", "składany", "szpitalny", "hybrydowy", "termoelastyczny",
    "poslaniowy", "posłaniowy", "dzieciecy", "dziecięcy",
})


def _pick_primary_product(products: list[Product], detail_url: str) -> Product:
    """From a detail page's extracted products, return the ONE that the page is about.

    A product detail page can still leak "related / you may also like" products into
    the extraction. The primary product is the one whose distinctive name token (brand
    word like GULEN, BILLEFJORD, HULDA) appears in the page's URL slug, e.g.
        url  .../topmaterace/topmaterac-80x200cm-wellpur-gulen-szary
        name "Topmaterac WELLPUR GULEN"  → token "gulen" is in the slug → primary.

    Falls back to the first product if nothing matches the slug.
    """
    if len(products) == 1:
        return products[0]

    slug = urlparse(detail_url).path.rstrip("/").split("/")[-1].lower()

    for p in products:
        tokens = re.findall(r"[a-zà-ſ]{3,}", p.product_name.lower())
        for tok in tokens:
            if tok in _GENERIC_NAME_TOKENS:
                continue
            if tok in slug:
                return p
    return products[0]


# Page-builder data attributes that carry a navigation URL on a non-anchor element.
# Some sites (Elementor / HappyAddons, etc.) make whole product cards clickable via JS
# instead of a plain <a href>, so BeautifulSoup's anchor scan misses the product links.
_DATA_LINK_PLAIN_ATTRS = ("data-href", "data-url", "data-link", "data-permalink", "data-clickurl")

# URLs that are category / listing / index pages rather than a single product.
# Used to filter JS card links so only genuine product pages enter the fiber pass.
# Note: plural "materace-…" / "materace" is a CATEGORY (e.g. /materace-hybrydowe); the
# singular "materac-…" is a product and is intentionally NOT matched here.
_LISTING_URL_RE = re.compile(
    r"(kategori|kolekcj|category|collection|karta-produktowa|produkty-"
    r"|/produkty$|/produkty/|materace(?:[-/]|$)|mattresses(?:[-/]|$))",
    re.IGNORECASE,
)


def _extract_links(
    html: str, base_url: str, base_domain: str, follow_subdomains: bool
) -> tuple[list[str], list[str]]:
    """Extract same-site links from HTML.

    Returns (anchor_links, card_links):
      • anchor_links — normal <a href> links (categories, nav, pagination, products…).
      • card_links   — product-card navigations stored in data-* attributes (JS-clickable
                       cards that aren't <a href>). These are treated as product detail
                       pages by the caller.
    """
    soup = BeautifulSoup(html, "lxml")
    anchors: list[str] = []
    cards: list[str] = []

    def _add(container: list[str], href: str | None) -> None:
        if not href:
            return
        href = href.strip()
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
            return
        full_url = _strip_fragment(urljoin(base_url, href))
        if not full_url.startswith(("http://", "https://")):
            return
        if same_site(full_url, base_domain, follow_subdomains):
            container.append(full_url)

    # Plain anchor links
    for tag in soup.find_all("a", href=True):
        _add(anchors, tag.get("href"))

    # JS card links: HappyAddons stores a JSON blob {"url": "..."} in data-ha-element-link
    for tag in soup.find_all(attrs={"data-ha-element-link": True}):
        raw = tag.get("data-ha-element-link")
        try:
            _add(cards, json.loads(raw).get("url", ""))
        except (ValueError, TypeError, AttributeError):
            pass

    # JS card links: plain URL stored directly in a data-* attribute
    for attr in _DATA_LINK_PLAIN_ATTRS:
        for tag in soup.find_all(attrs={attr: True}):
            _add(cards, tag.get(attr))

    return anchors, cards


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
