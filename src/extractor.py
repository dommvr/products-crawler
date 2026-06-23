"""
extractor.py — uses OpenAI to extract product data from page text/HTML.

Responsibilities:
- Translate category names to English for cross-language matching.
- Determine if a page is likely to contain target products.
- Extract structured product records including size variants.
- Assign a confidence score 0–1.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from dataclasses import dataclass
from typing import Any

from openai import (
    AsyncOpenAI,
    APIConnectionError,
    APIError,
    APITimeoutError,
    RateLimitError,
)

from src.config import Config

logger = logging.getLogger("crawler")

# Backoff tuning for OpenAI rate-limit (429) / transient errors. Exponential with jitter,
# capped — but the server's Retry-After hint (when present) takes precedence.
_RETRY_BASE_DELAY = 1.0   # seconds, first backoff step
_RETRY_MAX_DELAY = 30.0   # seconds, per-attempt cap


def _retry_after_seconds(exc: Exception) -> float | None:
    """Extract the server's suggested wait from a 429 response, if it provided one.

    OpenAI sets either `retry-after-ms` (milliseconds) or `retry-after` (seconds)."""
    resp = getattr(exc, "response", None)
    headers = getattr(resp, "headers", None) or {}
    ms = headers.get("retry-after-ms")
    if ms is not None:
        try:
            return float(ms) / 1000.0
        except (TypeError, ValueError):
            pass
    ra = headers.get("retry-after")
    if ra is not None:
        try:
            return float(ra)
        except (TypeError, ValueError):
            pass
    return None


@dataclass
class Product:
    company_name: str
    category: str
    product_name: str
    url: str
    photo_url: str
    confidence: float
    has_natural_fiber: str = ""    # "yes" / "no" / "" (undetermined)
    fiber_confidence: float = 0.0  # 0–1 how certain the fiber detection is
    fiber_evidence: str = ""       # verbatim text snippet that backed the decision

    def to_row(self) -> list[Any]:
        return [
            self.company_name,
            self.category,
            self.product_name,
            self.url,
            self.photo_url,
            round(self.confidence, 2),
            self.has_natural_fiber,
            round(self.fiber_confidence, 2),
            self.fiber_evidence,
        ]

    @staticmethod
    def headers() -> list[str]:
        return [
            "Company Name",
            "Category",
            "Product Name",
            "URL",
            "Photo URL",
            "Confidence",
            "Contains Natural Fiber",
            "Fiber Confidence",
            "Fiber Evidence",
        ]


class Extractor:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        # max_retries=0: we do our own retry loop (_create_completion) so backoff is
        # explicit and logged, and so we can honor the 429 Retry-After hint ourselves.
        self.client = AsyncOpenAI(api_key=cfg.openai_api_key, max_retries=0)
        self._translated_categories: list[str] = []

    async def _create_completion(self, **kwargs: Any):
        """Call chat.completions.create with bounded retry + exponential backoff.

        OpenAI returns HTTP 429 when we exceed the per-minute token (TPM) limit; under
        concurrent crawling this happens in bursts. We retry on 429, timeouts, connection
        errors, and transient 5xx — backing off (with jitter, honoring Retry-After) so an
        extraction isn't silently dropped. Non-retryable client errors (e.g. bad request)
        are raised immediately."""
        attempts = max(1, self.cfg.llm_max_retries)
        last_exc: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                return await self.client.chat.completions.create(**kwargs)
            except RateLimitError as e:
                last_exc = e
                delay = _retry_after_seconds(e)
                if delay is None:
                    delay = min(_RETRY_BASE_DELAY * 2 ** (attempt - 1), _RETRY_MAX_DELAY)
            except (APITimeoutError, APIConnectionError) as e:
                last_exc = e
                delay = min(_RETRY_BASE_DELAY * 2 ** (attempt - 1), _RETRY_MAX_DELAY)
            except APIError as e:
                # Retry transient server errors (5xx); re-raise other client errors.
                status = getattr(e, "status_code", None)
                if status is not None and 500 <= status < 600:
                    last_exc = e
                    delay = min(_RETRY_BASE_DELAY * 2 ** (attempt - 1), _RETRY_MAX_DELAY)
                else:
                    raise
            if attempt >= attempts:
                break
            delay += random.uniform(0, _RETRY_BASE_DELAY)  # jitter to de-sync concurrent retries
            logger.warning(
                f"  OpenAI {type(last_exc).__name__} (attempt {attempt}/{attempts}) — "
                f"retrying in {delay:.1f}s"
            )
            await asyncio.sleep(delay)
        # Retries exhausted — surface the last error to the caller's handler.
        assert last_exc is not None
        raise last_exc

    async def translate_categories(self) -> list[str]:
        """Translate configured category names to English (cached after first call)."""
        if self._translated_categories:
            return self._translated_categories

        cats = self.cfg.categories
        prompt = (
            "Translate the following product category names to English. "
            "Return ONLY a JSON array of strings, same order, no explanation.\n"
            f"Categories: {json.dumps(cats, ensure_ascii=False)}"
        )
        try:
            resp = await self._create_completion(
                model=self.cfg.llm_model,
                temperature=0,
                max_tokens=200,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = resp.choices[0].message.content.strip()
            raw = re.sub(r"```json|```", "", raw).strip()
            translated = json.loads(raw)
            if isinstance(translated, list):
                self._translated_categories = [str(t) for t in translated]
                logger.info(f"  Categories (translated): {self._translated_categories}")
                return self._translated_categories
        except Exception as e:
            logger.warning(f"  Category translation failed: {e} — using originals")

        self._translated_categories = cats
        return self._translated_categories

    async def is_product_page(self, text: str, url: str) -> bool:
        """Quick cheap check: does this page likely contain target products?"""
        en_cats = await self.translate_categories()
        all_cats = list(set(self.cfg.categories + en_cats))
        cats_str = ", ".join(all_cats)

        prompt = (
            f"Does the following webpage text contain or list products from these categories: {cats_str}?\n"
            "Reply with only YES or NO.\n\n"
            f"URL: {url}\n\n"
            f"TEXT (first 1500 chars):\n{text[:1500]}"
        )
        try:
            resp = await self._create_completion(
                model=self.cfg.llm_model,
                temperature=0,
                max_tokens=5,
                messages=[{"role": "user", "content": prompt}],
            )
            answer = resp.choices[0].message.content.strip().upper()
            return answer.startswith("YES")
        except Exception as e:
            logger.warning(f"  is_product_page check failed: {e}")
            return False

    async def extract_products(
        self,
        text: str,
        html: str,
        url: str,
        company_name: str,
        skip_fiber: bool = False,
        single_product: bool = False,
    ) -> list[Product]:
        """
        Extract product records from page content.

        Args:
            skip_fiber: When True (main crawl / category pages), fiber detection is
                        skipped entirely.  Fiber info is only reliable when reading a
                        dedicated product detail page, so the main crawl passes True
                        and the fiber-detection pass passes False (default).
            single_product: When True (fiber pass on a product DETAIL page), the page
                        is about exactly ONE product.  The model is told to return only
                        the primary product and ignore related / recommended / "you may
                        also like" carousels — this prevents one detail page's material
                        composition from being mis-attributed to other products shown on it.

        Returns a (possibly empty) list of Product objects.
        """
        en_cats = await self.translate_categories()
        all_cats = list(set(self.cfg.categories + en_cats))
        cats_str = ", ".join(all_cats)

        system_prompt = (
            "You are a product data extraction specialist. "
            "Extract structured product information from webpage content. "
            "Return ONLY valid JSON, no markdown, no explanation."
        )

        # ── Fiber detection section of the prompt ─────────────────────────────
        if skip_fiber:
            # Category / listing pages: never determine fiber — only detail pages are reliable
            fiber_prompt_section = """\
- has_natural_fiber: Always return empty string "".
  (Fiber detection is only performed on dedicated product detail pages, not here.)
- fiber_confidence: Always return 0.0
- fiber_evidence: Always return ""\
"""
        else:
            # Dedicated product detail page: apply strict fiber detection rules
            fiber_prompt_section = """\
- has_natural_fiber: Determine whether THIS specific product was manufactured using one of
  EXACTLY these two materials as a structural layer of the mattress:

    QUALIFYING MATERIALS — the ONLY ones that trigger "yes":
      • włókno kokosowe — also written as: kokos, włókno z łupiny kokosowej, płyta kokosowa,
        mata kokosowa, warstwa kokosowa, coconut fibre, coconut fiber, coir
      • sizal / sisal — also written as: włókno sizalowe, sisal fiber
      • coconut latex / lateks kokosowy / kokosowy lateks — a COMPOSITE material that bonds
        coconut fiber with latex.  It QUALIFIES because it contains coconut fiber as a
        structural component.  Do NOT confuse with pure natural latex (which does NOT qualify).

    NON-QUALIFYING materials — return "" even when the word "naturalny/naturalne" appears nearby:
      • pure lateks / pure latex WITHOUT coconut: naturalny lateks, latex Pulse, lateks Pulse,
        kauczuk, rubber, mleko kauczukowe, naturalne mleko kauczukowe, mleczko kauczukowe,
        natural latex — these are rubber tree derivatives, NOT coconut fiber or sisal.
        EXCEPTION: "coconut latex / lateks kokosowy" DOES qualify (see above).
      • wool / wełna (alpaca wool, merino wool, wełna alpaki, etc.) — does NOT qualify
      • pianka HR / pianka wysokoelastyczna / HR foam
      • pianka visco / memory foam / pianka termoelastyczna / viscoelastic
      • pianka Energy Foam, pianka hybrydowa
      • sprężyny, sprężyny kieszeniowe, bonell, kieszenie
      • poliester, bawełna, lyocell, tencel, tkanina, włókno poliestrowe

  Decision rules:
    "yes" — the product's OWN composition/construction/specification section explicitly
            mentions włókno kokosowe, kokos, sizal, sisal, coconut latex, or lateks kokosowy
            as a material IN THIS product.
    "no"  — the page contains a description, composition, construction, or specification
            of THIS product, and NONE of the qualifying materials (coconut fiber / sisal)
            appear in it.  This is the DEFAULT whenever the product is described but coconut
            fiber / sisal is not mentioned.  Choose "no" — do NOT hedge to "".
    ""    — ONLY when the page contains essentially NO descriptive information about this
            product at all: no composition, no construction details, no specification,
            no material description — i.e. there is nothing to judge from.
            Also use "" if the qualifying terms appear ONLY in unrelated context (general
            category descriptions, FAQ text, navigation/filter labels, comparison tables,
            OTHER products' descriptions, or "some models contain coconut" statements that
            are not specific to THIS product) AND this product's own materials are not given.

  IMPORTANT: Prefer "no" over "". If you can see what the product is made of (even partially)
  and coconut fiber / sisal is not among the materials, the answer is "no", not "".

  CRITICAL — common mistakes to avoid:
    ✓ "coconut latex" → return "yes" (coconut fiber bonded with latex — QUALIFIES)
    ✓ "lateks kokosowy" → return "yes" (Polish for coconut latex — QUALIFIES)
    ✗ "naturalny lateks" → return "" (pure natural latex = rubber, does NOT qualify)
    ✗ "naturalne mleko kauczukowe" → return "" (rubber tree milk ≠ coconut fiber)
    ✗ "alpaca wool / wełna alpaki" → return "no" (wool does NOT qualify)
    ✗ "niektóre modele zawierają kokos" → return "" (general statement, not this product)
    ✗ keyword appears in a different product's description → return ""

- fiber_confidence: float 0.0–1.0 reflecting certainty of has_natural_fiber.
  Use 0.9–1.0 when the exact qualifying term appears ("yes"), OR when a clear composition
  section is present and simply lacks coconut fiber / sisal ("no").
  Use 0.5–0.7 when inferring from a partial construction description without exact wording.
  Use 0.0 only when has_natural_fiber is "".
- fiber_evidence: Copy the VERBATIM text fragment (≤ 120 characters) from the page that
  most directly supports your has_natural_fiber decision — ideally the material list or
  specification sentence.  Use "" when has_natural_fiber is "".\
"""

        # Single-product preamble for the fiber pass (detail pages)
        single_product_rule = ""
        if single_product:
            single_product_rule = (
                "- THIS IS A SINGLE PRODUCT DETAIL PAGE. Extract ONLY the one primary product "
                "that this page is about (the product shown in the page title / main H1 heading "
                "and the main description/specification area).\n"
                "  Do NOT extract related products, recommended products, 'you may also like', "
                "'customers also viewed', 'podobne produkty', 'polecane', comparison items, or any "
                "product shown in a carousel/grid of OTHER products. Return EXACTLY ONE product.\n"
                "  The material composition you report MUST come from THIS product's own "
                "description — never from another product listed on the page.\n"
            )

        user_prompt = f"""Extract products from this webpage that belong to these categories: {cats_str}

IMPORTANT RULES:
{single_product_rule}- Only extract products from the specified categories. Ignore everything else.
- Return each UNIQUE PRODUCT NAME only ONCE — do NOT create separate entries for different sizes.
  Example: if you see "Materac HULDA 80x200", "Materac HULDA 90x200", "Materac HULDA 160x200",
  return a single entry with product_name = "Materac HULDA" (no dimensions in the name).
- photo_url: extract the direct image URL if visible in the HTML. Use empty string if not found.
- confidence: float 0.0–1.0 reflecting how certain you are this is a real product from the target category.
{fiber_prompt_section}
- If no matching products found, return {{"products": []}}

Return this exact JSON structure:
{{
  "products": [
    {{
      "product_name": "Product name WITHOUT size variant (e.g. 'Materac HULDA', not 'Materac HULDA 80x200')",
      "category": "matched category name",
      "photo_url": "direct image URL or empty string",
      "confidence": 0.95,
      "has_natural_fiber": "yes",
      "fiber_confidence": 0.95,
      "fiber_evidence": "Skład: pianka HR, włókno kokosowe 2cm, pianka visco"
    }}
  ]
}}

URL: {url}

PAGE TEXT (read the FULL text carefully — material information is often in the lower sections):
{text[:12000]}

RELEVANT HTML SNIPPET (for image URLs):
{_extract_img_tags(html)[:2000]}
"""

        try:
            resp = await self._create_completion(
                model=self.cfg.llm_model,
                temperature=self.cfg.llm_temperature,
                max_tokens=self.cfg.llm_max_tokens,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            )
            raw = resp.choices[0].message.content.strip()
            raw = re.sub(r"```json|```", "", raw).strip()
            data = json.loads(raw)
            products: list[Product] = []
            for item in data.get("products", []):
                if not isinstance(item, dict):
                    continue
                if skip_fiber:
                    # Ignore whatever the LLM returned for fiber fields — force empty
                    fiber_val = ""
                    fiber_conf = 0.0
                    fiber_evid = ""
                else:
                    fiber_raw = str(item.get("has_natural_fiber", "")).strip().lower()
                    fiber_val = fiber_raw if fiber_raw in ("yes", "no") else ""
                    fiber_conf = float(item.get("fiber_confidence", 0.0))
                    fiber_evid = str(item.get("fiber_evidence", "")).strip()
                products.append(Product(
                    company_name=company_name,
                    category=item.get("category", self.cfg.categories[0]),
                    product_name=item.get("product_name", "").strip(),
                    url=url,
                    photo_url=item.get("photo_url", "").strip(),
                    confidence=float(item.get("confidence", 0.5)),
                    has_natural_fiber=fiber_val,
                    fiber_confidence=fiber_conf,
                    fiber_evidence=fiber_evid,
                ))
            return products
        except json.JSONDecodeError as e:
            logger.warning(f"  JSON parse error in extraction: {e}")
            return []
        except Exception as e:
            logger.warning(f"  Extraction failed: {e}")
            return []


def _extract_img_tags(html: str) -> str:
    """Pull img src and srcset attributes from HTML for the LLM to use."""
    img_re = re.compile(r'<img[^>]+>', re.IGNORECASE)
    src_re = re.compile(r'src=["\']([^"\']+)["\']', re.IGNORECASE)
    lines = []
    for tag in img_re.findall(html):
        m = src_re.search(tag)
        if m:
            lines.append(m.group(1))
    return "\n".join(lines)
