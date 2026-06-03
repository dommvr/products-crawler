"""
crawler.py — Playwright-based website crawler.

For each company URL it:
  1. Discovers all product-listing and product-detail pages.
  2. Follows pagination links.
  3. Optionally follows subdomain links.
  4. On product pages: clicks size/variant selectors and captures changed content.
  5. Passes page content to the Extractor.
  6. Respects max_pages, delay, and skip list.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import deque
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, Page, BrowserContext, TimeoutError as PWTimeout

from src.config import Config
from src.extractor import Extractor, Product
from src.utils import get_root_domain, is_skippable, logger, same_site

# CSS selectors that suggest size/variant controls
VARIANT_SELECTORS = [
    "select[name*='size']",
    "select[name*='variant']",
    "select[name*='Size']",
    ".size-selector option",
    ".variant-selector option",
    "[data-size]",
    "[data-variant]",
    ".product-size button",
    ".product-variant button",
    ".size-option",
    ".variant-option",
    "button[data-size]",
    "button.size",
    # Generic — buttons/links inside a product that look like sizes
    ".sizes a",
    ".sizes button",
    ".wymiary a",
    ".rozmiary a",
    ".rozmiary button",
]

# Link patterns that suggest product listings or detail pages
PRODUCT_URL_PATTERNS = re.compile(
    r"/(product|produkt|item|p/|sklep|shop|kategoria|category|kolekcja|collection"
    r"|materac|sofa|lozko|fotel|kanapa|bed|mattress|pillow|furniture)",
    re.IGNORECASE,
)

# Pagination patterns
PAGINATION_PATTERNS = re.compile(
    r"[?&](page|strona|p|pg)=\d+|/page/\d+|/strona/\d+",
    re.IGNORECASE,
)


class Crawler:
    def __init__(self, cfg: Config, extractor: Extractor) -> None:
        self.cfg = cfg
        self.extractor = extractor

    async def crawl_company(self, start_url: str, company_name: str) -> list[Product]:
        """
        Crawl the entire site starting from start_url.
        Returns all extracted products for this company.
        """
        log = logging.getLogger("crawler")
        all_products: list[Product] = []
        visited: set[str] = set()
        queue: deque[str] = deque([start_url])
        base_domain = get_root_domain(start_url)
        pages_visited = 0

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

            while queue and (self.cfg.max_pages == 0 or pages_visited < self.cfg.max_pages):
                url = queue.popleft()
                norm_url = _strip_fragment(url)

                if norm_url in visited:
                    continue
                if is_skippable(url, self.cfg.skip_domains):
                    continue
                if not same_site(url, base_domain, self.cfg.follow_subdomains):
                    continue

                visited.add(norm_url)
                pages_visited += 1

                log.info(f"  [{pages_visited}/{self.cfg.max_pages or '∞'}] {url}")

                html, text = await self._load_page(context, url)
                if not html:
                    continue

                # ── Check if this page likely has target products ──────────
                if not await self.extractor.is_product_page(text, url):
                    log.debug(f"  No target products on page, still collecting links: {url}")
                    # Still collect links — might lead to product pages
                    new_links = _extract_links(html, url, base_domain, self.cfg.follow_subdomains)
                    for link in new_links:
                        if link not in visited:
                            queue.append(link)
                    if self.cfg.delay > 0:
                        await asyncio.sleep(self.cfg.delay)
                    continue

                # ── Extract products from this page ────────────────────────
                products = await self.extractor.extract_products(text, html, url, company_name)

                # ── Click size/variant options and re-extract if needed ────
                if self.cfg.extract_size_variants:
                    variant_products = await self._extract_variants(context, url, company_name, html)
                    # Merge: add variants not already captured by name
                    existing_names = {p.product_name for p in products}
                    for vp in variant_products:
                        if vp.product_name not in existing_names:
                            products.append(vp)
                            existing_names.add(vp.product_name)

                if products:
                    log.info(f"  Found {len(products)} product(s) on {url}")
                    all_products.extend(products)
                else:
                    log.debug(f"  LLM found no matching products on {url}")

                # ── Collect links for further crawling ─────────────────────
                new_links = _extract_links(html, url, base_domain, self.cfg.follow_subdomains)
                for link in new_links:
                    if link not in visited:
                        queue.append(link)

                if self.cfg.delay > 0:
                    await asyncio.sleep(self.cfg.delay)

            await browser.close()

        # Deduplicate by (product_name, url)
        seen: set[tuple[str, str]] = set()
        deduped: list[Product] = []
        for p in all_products:
            key = (p.product_name.lower().strip(), p.url)
            if key not in seen:
                seen.add(key)
                deduped.append(p)

        log.info(f"  Total unique products for {company_name}: {len(deduped)}")
        return deduped

    async def _load_page(self, context: BrowserContext, url: str) -> tuple[str, str]:
        """Load a page and return (html, clean_text). Returns ('','') on failure."""
        for attempt in range(1, self.cfg.max_retries + 1):
            try:
                page: Page = await context.new_page()
                await page.goto(url, wait_until="domcontentloaded", timeout=self.cfg.page_timeout)
                # Wait a moment for JS rendering
                await asyncio.sleep(1.5)
                html = await page.content()
                text = _html_to_text(html)
                await page.close()
                return html, text
            except PWTimeout:
                logger.warning(f"  Timeout on {url} (attempt {attempt}/{self.cfg.max_retries})")
            except Exception as e:
                logger.warning(f"  Error loading {url} (attempt {attempt}): {e}")
            finally:
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
        original_html: str,
    ) -> list[Product]:
        """
        Open the page, find size/variant controls, click each option,
        capture any content changes, extract products from each state.
        """
        variant_products: list[Product] = []
        page: Page | None = None
        try:
            page = await context.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=self.cfg.page_timeout)
            await asyncio.sleep(1.5)

            for selector in VARIANT_SELECTORS:
                try:
                    elements = await page.query_selector_all(selector)
                    if not elements:
                        continue

                    for el in elements:
                        try:
                            # Capture description/title before click
                            before_html = await page.content()

                            tag = await el.evaluate("e => e.tagName.toLowerCase()")
                            if tag == "option":
                                # Select via parent <select>
                                parent_select = await el.evaluate_handle(
                                    "e => e.closest('select')"
                                )
                                value = await el.get_attribute("value")
                                if value:
                                    await parent_select.select_option(value=value)
                            else:
                                await el.click()

                            await asyncio.sleep(0.8)
                            after_html = await page.content()

                            # Only re-extract if page content changed meaningfully
                            if _content_changed(before_html, after_html):
                                after_text = _html_to_text(after_html)
                                new_prods = await self.extractor.extract_products(
                                    after_text, after_html, url, company_name
                                )
                                variant_products.extend(new_prods)

                        except Exception:
                            continue
                    # Only process first matching selector type per page
                    if variant_products:
                        break
                except Exception:
                    continue

        except Exception as e:
            logger.debug(f"  Variant extraction error on {url}: {e}")
        finally:
            if page:
                try:
                    await page.close()
                except Exception:
                    pass

        return variant_products


# ── Helpers ──────────────────────────────────────────────────────────────────

def _extract_links(html: str, base_url: str, base_domain: str, follow_subdomains: bool) -> list[str]:
    """Extract and filter all href links from HTML."""
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
    """Remove #fragment from URL."""
    return url.split("#")[0].rstrip("/") or url


def _html_to_text(html: str) -> str:
    """Convert HTML to clean readable text."""
    soup = BeautifulSoup(html, "lxml")
    # Remove noise elements
    for tag in soup(["script", "style", "noscript", "header", "footer", "nav", "aside"]):
        tag.decompose()
    text = soup.get_text(separator="\n", strip=True)
    # Collapse excessive blank lines
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    return "\n".join(lines)


def _content_changed(before: str, after: str, threshold: int = 50) -> bool:
    """
    True if the two HTML strings differ enough to suggest the product
    description or title changed (not just a button state).
    """
    # Compare cleaned text lengths and content
    before_text = _html_to_text(before)
    after_text = _html_to_text(after)
    if abs(len(before_text) - len(after_text)) > threshold:
        return True
    # Check if price or dimension patterns changed
    dim_re = re.compile(r"\d{2,3}\s*[xX×]\s*\d{2,3}")
    before_dims = set(dim_re.findall(before_text))
    after_dims = set(dim_re.findall(after_text))
    return before_dims != after_dims
