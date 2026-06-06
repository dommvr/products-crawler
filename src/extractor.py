"""
extractor.py — uses OpenAI to extract product data from page text/HTML.

Responsibilities:
- Translate category names to English for cross-language matching.
- Determine if a page is likely to contain target products.
- Extract structured product records including size variants.
- Assign a confidence score 0–1.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from openai import AsyncOpenAI

from src.config import Config

logger = logging.getLogger("crawler")


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
        self.client = AsyncOpenAI(api_key=cfg.openai_api_key)
        self._translated_categories: list[str] = []

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
            resp = await self.client.chat.completions.create(
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
            resp = await self.client.chat.completions.create(
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
    ) -> list[Product]:
        """
        Extract product records from page content.
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

        user_prompt = f"""Extract products from this webpage that belong to these categories: {cats_str}

IMPORTANT RULES:
- Only extract products from the specified categories. Ignore everything else.
- Return each UNIQUE PRODUCT NAME only ONCE — do NOT create separate entries for different sizes.
  Example: if you see "Materac HULDA 80x200", "Materac HULDA 90x200", "Materac HULDA 160x200",
  return a single entry with product_name = "Materac HULDA" (no dimensions in the name).
- photo_url: extract the direct image URL if visible in the HTML. Use empty string if not found.
- confidence: float 0.0–1.0 reflecting how certain you are this is a real product from the target category.
- has_natural_fiber: Determine whether THIS specific product was MANUFACTURED/CONSTRUCTED
  using coconut fiber (włókno kokosowe) or sisal (sizal) as a structural component of the mattress.
  Look ONLY at material composition sections, construction layer descriptions, or specification
  tables that describe what THIS product is made of.
  Rules:
    "yes"  — a composition/construction section explicitly states this product contains
             włókno kokosowe / kokos / sizal / sisal / coconut fiber / coconut fibre.
    "no"   — a composition/construction section lists the product's materials and none of
             the above appear.
    ""     — no material composition information is present on the page, OR the terms appear
             only in general FAQ text, comparison tables, other products' descriptions,
             or filter/navigation labels (not describing THIS product's construction).
  DO NOT set "yes" just because the keyword appears anywhere on the page.
- fiber_confidence: float 0.0–1.0 reflecting certainty of has_natural_fiber.
  Use 0.9–1.0 when the exact material term appears in a product composition/spec section.
  Use 0.5–0.7 when you are inferring from a construction description without exact wording.
  Use 0.0 when has_natural_fiber is "".
- fiber_evidence: Copy the VERBATIM text fragment (≤ 120 characters) from the page that most
  directly supports your has_natural_fiber decision — ideally the material list or spec sentence.
  Use empty string "" when has_natural_fiber is "".
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
            resp = await self.client.chat.completions.create(
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
                fiber_raw = str(item.get("has_natural_fiber", "")).strip().lower()
                fiber_val = fiber_raw if fiber_raw in ("yes", "no") else ""
                products.append(Product(
                    company_name=company_name,
                    category=item.get("category", self.cfg.categories[0]),
                    product_name=item.get("product_name", "").strip(),
                    url=url,
                    photo_url=item.get("photo_url", "").strip(),
                    confidence=float(item.get("confidence", 0.5)),
                    has_natural_fiber=fiber_val,
                    fiber_confidence=float(item.get("fiber_confidence", 0.0)),
                    fiber_evidence=str(item.get("fiber_evidence", "")).strip(),
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
