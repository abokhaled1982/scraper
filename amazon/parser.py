#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
amazon_parser_pro.py
--------------------
Professional, robust, and extensible Amazon product HTML parser.

Key additions in this version
-----------------------------
- Parses "wie viel gekauft" (e.g., "100+ gekauft Mal im letzten Monat")
  -> purchases_past_month, purchases_past_month_is_plus, purchases_past_month_raw
- Extracts "Produktinformationen" / "Technische Daten" sections including:
  Hersteller, Modellnummer, Abmessungen, Gewicht, Herkunftsland, Erstverfügbarkeit, etc.
  -> product_info (normalized dict + raw keys preserved)

Design goals
------------
- Offline parsing of locally saved Amazon product pages
- Robust against DOM variations (multiple selectors + regex fallbacks)
- Clean architecture: utils -> parser (extractors) -> pipeline
- Outputs:
    out/<file>.json (per product)
    out/summary.jsonl (one product JSON per line)
- No CSV.

Usage
-----
    # Parse default folder "inbox" and write JSON to "out"
    python amazon_parser_pro.py

    # Custom folders
    python amazon_parser_pro.py --inbox "C:\\data\\inbox" --out "C:\\data\\out"

Dependencies
------------
    pip install bs4 lxml

Legal
-----
Parsing Amazon HTML may be subject to Amazon's terms. Use on offline files.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# 3rd-party
try:
    from bs4 import BeautifulSoup
except Exception:
    sys.exit("Missing dependency: bs4. Install via: pip install bs4 lxml")

# ---------------------------------------------------------------------------
# Utils
# ---------------------------------------------------------------------------

def norm_space(s: Optional[str]) -> str:
    """Collapse whitespace and strip."""
    return re.sub(r"\s+", " ", (s or "")).strip()

def extract_html_from_js_wrapper(raw: str) -> str:
    """
    Some scrapers store HTML embedded in a JS string/assignment.
    We trim everything before the first '<html' or '<!doctype'.
    """
    m = re.search(r"(<\s*!doctype\b.*?>|<\s*html\b)", raw, flags=re.I | re.S)
    return raw[m.start():] if m else raw

def clean_price(text: Optional[str]) -> Optional[Dict[str, Any]]:
    """
    Normalize prices like '€ 1.234,56' or '$1,234.56' -> {'raw', 'value', 'currency_hint'}
    """
    if not text:
        return None
    t = text.strip()

    currency = None
    cm = re.search(r"(€|EUR|\$|USD|£|GBP|¥|JPY)", t, re.I)
    if cm:
        currency = (
            cm.group(1)
            .upper()
            .replace("$", "USD")
            .replace("€", "EUR")
            .replace("£", "GBP")
            .replace("¥", "JPY")
        )

    # keep digits and separators
    num = re.sub(r"[^0-9,.\-]", "", t)

    # heuristic: EU decimal
    if num.count(",") and not num.count("."):
        num_eu = num.replace(".", "")
        if "," in num_eu:
            head, _, tail = num_eu.rpartition(",")
            num_std = head.replace(",", "") + "." + tail
        else:
            num_std = num_eu
    else:
        # US style or mixed
        num_std = num.replace(",", "")

    try:
        val = float(num_std)
    except Exception:
        val = None

    return {"raw": t, "value": val, "currency_hint": currency}

def parse_coupon_value(text: Optional[str]) -> Dict[str, Any]:
    """
    Parse coupon hints like "10% coupon" or "€5 coupon".
    Returns {"percent": float|None, "amount": float|None, "currency_hint": str|None}
    """
    if not text:
        return {"percent": None, "amount": None, "currency_hint": None}
    t = text.strip()

    # percentage
    m_pct = re.search(r"(\d{1,3}(?:[.,]\d+)?)\s*%", t)
    if m_pct:
        try:
            pct = float(m_pct.group(1).replace(",", "."))
            return {"percent": pct, "amount": None, "currency_hint": None}
        except Exception:
            pass

    # absolute
    m_abs = re.search(r"(€|EUR|\$|USD|£|GBP|¥|JPY)?\s*([0-9][0-9.,]*)", t, flags=re.I)
    if m_abs:
        cur = m_abs.group(1)
        if cur:
            cur = (
                cur.upper()
                .replace("$", "USD")
                .replace("€", "EUR")
                .replace("£", "GBP")
                .replace("¥", "JPY")
            )
        raw_amt = m_abs.group(2)
        if "," in raw_amt and "." not in raw_amt:
            amt_norm = raw_amt.replace(".", "").replace(",", ".")
        else:
            amt_norm = raw_amt.replace(",", "")
        try:
            amt = float(amt_norm)
            return {"percent": None, "amount": amt, "currency_hint": cur}
        except Exception:
            pass

    return {"percent": None, "amount": None, "currency_hint": None}

def compute_discount_fields(
    price_obj: Optional[Dict[str, Any]],
    original_price_obj: Optional[Dict[str, Any]],
    coupon_info: Optional[Dict[str, Any]],
) -> Dict[str, Optional[float]]:
    """
    Compute:
      - discount_amount
      - discount_percent
      - final_price_after_coupon
    Based on price, original/list price, and optional coupon.
    """
    price_val = price_obj.get("value") if isinstance(price_obj, dict) else None
    orig_val = original_price_obj.get("value") if isinstance(original_price_obj, dict) else None

    discount_amount: Optional[float] = None
    discount_percent: Optional[float] = None
    final_after_coupon: Optional[float] = None

    if price_val is not None and orig_val and orig_val > 0:
        discount_amount = max(orig_val - price_val, 0)
        discount_percent = (discount_amount / orig_val) * 100 if orig_val else None

    if price_val is not None and coupon_info:
        cpct = coupon_info.get("percent")
        camt = coupon_info.get("amount")
        if cpct:
            try:
                final_after_coupon = max(price_val * (1 - cpct / 100.0), 0)
            except Exception:
                pass
        elif camt:
            try:
                final_after_coupon = max(price_val - camt, 0)
            except Exception:
                pass

    def rnd(x: Optional[float]) -> Optional[float]:
        return round(x, 2) if isinstance(x, (int, float)) else None

    return {
        "discount_amount": rnd(discount_amount),
        "discount_percent": rnd(discount_percent),
        "final_price_after_coupon": rnd(final_after_coupon),
    }

def first_nonempty(*vals: Optional[str]) -> Optional[str]:
    for v in vals:
        if v and str(v).strip():
            return v
    return None

# ---------------------------------------------------------------------------
# Data Model
# ---------------------------------------------------------------------------

@dataclass
class ProductData:
    # Core identity
    title: Optional[str] = None
    brand: Optional[str] = None
    asin: Optional[str] = None

    # Pricing
    price: Optional[Dict[str, Any]] = None
    original_price: Optional[Dict[str, Any]] = None
    discount_amount: Optional[float] = None
    discount_percent: Optional[float] = None
    coupon_text: Optional[str] = None
    coupon_value: Dict[str, Any] = field(default_factory=dict)
    final_price_after_coupon: Optional[float] = None

    # Availability / delivery
    availability: Optional[str] = None
    is_prime: bool = False
    shipping_cost_text: Optional[str] = None
    delivery_date: Optional[str] = None
    free_returns: Optional[bool] = None
    returns_days: Optional[int] = None
    returns_text: Optional[str] = None

    # Social proof
    rating_value: Optional[float] = None
    review_count: Optional[int] = None
    answered_questions: Optional[int] = None
    purchases_past_month: Optional[int] = None
    purchases_past_month_is_plus: Optional[bool] = None
    purchases_past_month_raw: Optional[str] = None

    # Ranking / vendor
    bestseller_rank: Optional[str] = None
    seller_name: Optional[str] = None
    seller_rating_percent: Optional[int] = None
    buybox_sold_by: Optional[str] = None

    # Content
    variants: Dict[str, str] = field(default_factory=dict)
    bullets: List[str] = field(default_factory=list)
    description: Optional[str] = None
    images: List[str] = field(default_factory=list)
    badges: List[str] = field(default_factory=list)
    deal_badge: Optional[str] = None

    # Product info / tech details
    product_info: Dict[str, Any] = field(default_factory=dict)
    affiliate_url: Optional[str] = None  # <— diese Zeile hinzufügen
    # File provenance
    _source_file: Optional[str] = None

# ---------------------------------------------------------------------------
# Amazon Product Parser
# ---------------------------------------------------------------------------

class AmazonProductParser:
    """
    Encapsulates parsing logic for a single HTML document.
    Extend by adding methods or enriching selector pools.
    """

    def __init__(self, html_text: str):
        self.html_text = extract_html_from_js_wrapper(html_text)
        try:
            self.soup = BeautifulSoup(self.html_text, "lxml")
        except Exception:
            self.soup = BeautifulSoup(self.html_text, "html.parser")

    # --- small helpers bound to soup ---

    def _select_text(self, *selectors: str) -> Optional[str]:
        for sel in selectors:
            el = self.soup.select_one(sel)
            if el:
                t = norm_space(el.get_text())
                if t:
                    return t
        return None

    def _select_attr(self, selectors: List[str], attr: str) -> Optional[str]:
        for sel in selectors:
            el = self.soup.select_one(sel)
            if el and el.has_attr(attr):
                v = el.get(attr)
                if isinstance(v, list):
                    v = v[0]
                v = norm_space(v)
                if v:
                    return v
        return None

    def _find_by_regex(self, patterns: List[str], from_text: Optional[str] = None) -> Optional[str]:
        text = from_text if from_text is not None else self.soup.get_text(" ", strip=True)
        for pat in patterns:
            m = re.search(pat, text, flags=re.I)
            if m:
                return m.group(1) if m.groups() else m.group(0)
        return None

    # --- extractors ---

    def extract_core(self, data: ProductData) -> None:
        data.title = self._select_text(
            "#productTitle",
            "#title #productTitle",
            "h1.a-size-large.a-spacing-none",
            "span#title",
        )
        data.brand = self._select_text(
            "#bylineInfo",
            "#brand",
            "a#bylineInfo",
            "tr.po-brand td.a-span9 span",
            "div#bylineInfo_feature_div",
        )
        # ASIN via tables or data-asin
        asin = None
        for table in self.soup.select("table, div#detailBulletsWrapper_feature_div, div#productDetails_detailBullets_sections1"):
            cells = table.select("th, td")
            for th in cells:
                if norm_space(th.get_text()).upper() == "ASIN":
                    td = th.find_next("td")
                    if td:
                        asin = norm_space(td.get_text())
                        break
            if asin:
                break
        if not asin:
            candidate = self._select_attr(
                ["#dp", "#centerCol", "#detailBulletsWrapper_feature_div", "div[data-asin]"], "data-asin"
            )
            if candidate and len(candidate) >= 8:
                asin = candidate
        data.asin = asin

    def extract_prices(self, data: ProductData) -> None:
        price_text = self._select_text(
            "#corePrice_feature_div span.a-price span.a-offscreen",
            "#priceblock_ourprice",
            "#priceblock_dealprice",
            "#priceblock_saleprice",
            "span.a-price.aok-align-center .a-offscreen",
        )
        data.price = clean_price(price_text) if price_text else None

        list_price_text = self._select_text(
            "#price span.a-text-price .a-offscreen",
            ".priceBlockStrikePriceString",
            "#priceblock_ourprice_row .a-text-strike",
            "#price-basis span.a-offscreen",
            "span.a-price.a-text-price .a-offscreen",
            "span.a-price[data-a-strike='true'] .a-offscreen",
            "td.a-span12 span.a-color-secondary.a-text-strike",
        )
        data.original_price = clean_price(list_price_text) if list_price_text else None

        # explicit savings %
        explicit_savings = self._select_text(
            "#corePrice_feature_div span.savingsPercentage",
            ".priceBlockSavingsString",
        )
        explicit_pct = None
        if explicit_savings:
            m = re.search(r"(\-?\d{1,3}[.,]?\d?)\s?%", explicit_savings)
            if m:
                try:
                    explicit_pct = float(m.group(1).replace(",", "."))
                except Exception:
                    pass

        coupon_text = self._select_text(
            "#promoPriceBlockMessage_feature_div",
            "#couponFeatureDiv li",
            "span.couponBadge",
            "span#couponText",
            "span[data-csa-c-content-id='couponBadge']",
            "div#promo_feature_div span",
            "div#coupon_feature_div span",
        )
        data.coupon_text = norm_space(coupon_text) if coupon_text else None
        data.coupon_value = parse_coupon_value(data.coupon_text)

        comp = compute_discount_fields(data.price, data.original_price, data.coupon_value)
        data.discount_amount = comp["discount_amount"]
        data.discount_percent = comp["discount_percent"] if comp["discount_percent"] is not None else explicit_pct
        data.final_price_after_coupon = comp["final_price_after_coupon"]

    def extract_availability_and_delivery(self, data: ProductData) -> None:
        data.availability = self._select_text("#availability span", "#availability", "#outOfStock")
        data.is_prime = bool(self.soup.select_one("#primeBadge, i.a-icon.a-icon-prime, .a-prime-badge, .prime-logo"))

        data.delivery_date = first_nonempty(
            self._select_text("#mir-layout-DELIVERY_BLOCK-slot-PRIMARY_DELIVERY_MESSAGE_LARGE"),
            self._select_text("#deliveryMessageMirId"),
            self._select_text("#contextualIngressPtMsg"),
            self._find_by_regex([r"Lieferung\s+bis\s+[^.,;]+", r"Delivery\s+by\s+[^.,;]+"]),
        )
        data.shipping_cost_text = first_nonempty(
            self._select_text("#mir-layout-DELIVERY_BLOCK-slot-SECONDARY_DELIVERY_MESSAGE", "span#price-shipping-message"),
            self._find_by_regex([r"(?:GRATIS Versand|Free delivery|Free shipping|Versandkostenfrei)"]),
        )

        data.returns_text = first_nonempty(
            self._select_text("#returns_policy", "a#returns-policy-anchor", "#RETURNS_POLICY a", "div#retail-seller_profile_container"),
            self._select_text("a#freeReturns", "div#RETURNS_POLICY", "span#FREE_RETURNS"),
            self._find_by_regex([r"Kostenlose Rückgabe", r"free returns"]),
        )
        if data.returns_text:
            data.free_returns = bool(re.search(r"(Kostenlose Rückgabe|free returns)", data.returns_text, flags=re.I))
            mdays = re.search(r"(\d{1,3})\s*(?:Tage|days)", data.returns_text, flags=re.I)
            if mdays:
                try:
                    data.returns_days = int(mdays.group(1))
                except Exception:
                    pass

    def extract_social_proof(self, data: ProductData) -> None:
        rating_text = self._select_text(
            "span[data-hook='rating-out-of-text']",
            "span#acrPopover",
            "span.a-icon-alt",
        )
        if rating_text:
            m = re.search(r"(\d+[.,]?\d*)\s*(?:out of|von)\s*5", rating_text, re.I)
            if m:
                try:
                    data.rating_value = float(m.group(1).replace(",", "."))
                except Exception:
                    pass

        review_count_text = self._select_text(
            "#acrCustomerReviewText",
            "span[data-hook='total-review-count']",
            "#acrCustomerReviewLink #acrCustomerReviewText",
        )
        if review_count_text:
            digits = re.sub(r"[^0-9,.\s]", "", review_count_text)
            digits = digits.replace(".", "").replace(",", "")
            try:
                data.review_count = int(digits)
            except Exception:
                pass

        answered_q_text = self._select_text("#askATFLink span", "#askATFLink", "#ask-btf_feature_div a")
        if answered_q_text:
            m = re.search(r"(\d[\d\.,]*)", answered_q_text)
            if m:
                try:
                    data.answered_questions = int(m.group(1).replace(".", "").replace(",", ""))
                except Exception:
                    pass

        # --- "Bought in past month" / "gekauft im letzten Monat" (handles "100+ gekauft ...")
        purchases_raw = self._find_by_regex(
            [
                r"(\d[\d\.\,]*\+?)\s*(?:bought in the past month|bought in past month)",
                r"(\d[\d\.\,]*\+?)\s*(?:gekauft|Käufe?|mal)\s+(?:im|in den)\s+(?:letzten|vergangenen)?\s*Monat",
            ]
        )
        if purchases_raw:
            data.purchases_past_month_raw = purchases_raw
            is_plus = purchases_raw.strip().endswith("+")
            num_str = purchases_raw.strip().rstrip("+")
            try:
                val = int(num_str.replace(".", "").replace(",", ""))
                data.purchases_past_month = val
                data.purchases_past_month_is_plus = True if is_plus else False
            except Exception:
                # fallback: try to catch "100+" where formatting is odd
                m = re.search(r"(\d+)\s*\+", purchases_raw)
                if m:
                    data.purchases_past_month = int(m.group(1))
                    data.purchases_past_month_is_plus = True

    def extract_ranking_and_vendor(self, data: ProductData) -> None:
        rank_blocks: List[str] = []
        for sel in ["#detailBulletsWrapper_feature_div", "#productDetails_detailBullets_sections1", "#SalesRank"]:
            el = self.soup.select_one(sel)
            if el:
                rank_blocks.append(el.get_text(" ", strip=True))
        ranks_text = " ".join(rank_blocks)
        data.bestseller_rank = self._find_by_regex(
            [r"#\s?(\d[\d,\.]*)\s+in\s+[\w\s&>,-]+", r"Nr\.\s?(\d[\d\.\,]*)\s+in\s+[\w\s&>,-]+"],
            from_text=ranks_text,
        )

        data.seller_name = self._select_text(
            "#sellerProfileTriggerId",
            "#merchant-info a",
            "#tabular-buybox div.tabular-buybox-text a",
        )
        data.buybox_sold_by = self._select_text("#merchant-info", "#tabular-buybox .tabular-buybox-container")

        seller_rating_text = self._find_by_regex([r"(\d{1,3})%\s*positive"])
        if seller_rating_text:
            try:
                data.seller_rating_percent = int(seller_rating_text)
            except Exception:
                pass

    # ---- PRODUCT INFO / TECH DETAILS ----
    def _normalize_info_key(self, k: str) -> str:
        """Normalize German/English keys to stable snake_case."""
        k_norm = norm_space(k).lower()
        mapping = {
            # de -> en (common)
            "hersteller": "manufacturer",
            "marke": "brand",
            "modellnummer": "model_number",
            "artikelmodellnummer": "model_number",
            "artikelabmessungen l x b x h": "dimensions_lwh",
            "abmessungen des pakets": "package_dimensions",
            "abmessungen": "dimensions",
            "artikelgewicht": "item_weight",
            "gewicht": "weight",
            "herkunftsland": "country_of_origin",
            "erste verfügbarkeit": "first_available_date",
            "erstverfügbarkeit": "first_available_date",
            "datum der erstverfügbarkeit": "first_available_date",
            "bestseller-rang": "bestseller_rank_text",
            "sales rank": "bestseller_rank_text",
            "asin": "asin",
            "ean": "ean",
            "gtin": "gtin",
            "upc": "upc",
            "produktabmessungen": "product_dimensions",
            "farbe": "color",
            "größe": "size",
            "material": "material",
            "herstellerreferenz": "manufacturer_reference",
            "modellar": "model_number",
        }
        # try simple mapping first
        if k_norm in mapping:
            return mapping[k_norm]
        # generic normalize
        k_norm = re.sub(r"[^a-z0-9]+", "_", k_norm).strip("_")
        return k_norm

    def _collect_key_values(self, container) -> Dict[str, str]:
        """
        Given a details container (table/div list), try to read key->value pairs.
        Works with various Amazon layouts.
        """
        kv: Dict[str, str] = {}

        # 1) Key in <th>, value in next <td>
        for row in container.select("tr"):
            th = row.find("th")
            td = row.find("td")
            if th and td:
                key = norm_space(th.get_text())
                val = norm_space(td.get_text())
                if key and val:
                    kv[key] = val

        # 2) Detail bullets (dt/dd like)
        for dl in container.select("dl"):
            dts = dl.select("dt")
            dds = dl.select("dd")
            for dt, dd in zip(dts, dds):
                key = norm_space(dt.get_text())
                val = norm_space(dd.get_text())
                if key and val:
                    kv[key] = val

        # 3) Bullet list inside detail wrapper
        for li in container.select("li"):
            # pattern: <span class="a-text-bold">Key:</span> Value
            bold = li.select_one("span.a-text-bold")
            if bold:
                key = norm_space(bold.get_text()).rstrip(":")
                val = norm_space(li.get_text().replace(bold.get_text(), "", 1))
                if key and val:
                    kv[key] = val

        # 4) A newer "po-" table structure (mobile/desktop)
        for row in container.select("div.po-row, div.po-item, tr.po-row"):
            lab = row.select_one(".po-attribute-name, .a-span3, th")
            val = row.select_one(".po-attribute-value, .a-span9, td")
            key = norm_space(lab.get_text()) if lab else None
            value = norm_space(val.get_text()) if val else None
            if key and value:
                kv[key] = value

        return kv

    def extract_product_info(self, data: ProductData) -> None:
        """
        Parse 'Produktinformationen' / 'Technische Daten' blocks from various layouts.
        """
        info_blocks = []

        # Classic details sections
        for sel in [
            "#productDetails_detailBullets_sections1",
            "#productDetails_techSpec_section_1",
            "#productDetails_techSpec_section_2",
            "#detailBulletsWrapper_feature_div",
            "#prodDetails",
        ]:
            el = self.soup.select_one(sel)
            if el:
                info_blocks.append(el)

        # A+ and mobile layouts may use "po-" prefixed containers
        for el in self.soup.select("div#poExpander, div#poDocumentCarousel, div#technicalSpecifications_feature_div"):
            info_blocks.append(el)

        # Aggregate key-values
        raw_kv: Dict[str, str] = {}
        for blk in info_blocks:
            raw_kv.update(self._collect_key_values(blk))

        # Normalize keys
        normalized: Dict[str, Any] = {}
        for k, v in raw_kv.items():
            nk = self._normalize_info_key(k)
            if nk in normalized and normalized[nk] != v:
                # Keep first, also store under nk__alt if different
                suffix = 2
                while f"{nk}__alt{suffix}" in normalized:
                    suffix += 1
                normalized[f"{nk}__alt{suffix}"] = v
            else:
                normalized[nk] = v

        # If manufacturer missing but brand present, set as fallback
        if "manufacturer" not in normalized and data.brand:
            normalized["manufacturer"] = data.brand

        # If ASIN missing but we have data.asin
        if "asin" not in normalized and data.asin:
            normalized["asin"] = data.asin

        data.product_info = {                      # original keys as seen on page
            "normalized": normalized  # normalized keys for stable usage
        }

    def extract_content(self, data: ProductData) -> None:
        # variants (size/color)
        for block in self.soup.select("div#twister, div#variation_size_name, div#variation_color_name"):
            label = block.select_one("label")
            lab = norm_space(label.get_text()) if label else None
            selected = block.select_one("span.selection")
            sel = norm_space(selected.get_text()) if selected else None
            if lab or sel:
                data.variants[lab or "variant"] = sel

        # bullets
        bullets = [
            norm_space(li.get_text())
            for li in self.soup.select("#feature-bullets li:not(.aok-hidden)")
            if norm_space(li.get_text())
        ]
        data.bullets = bullets

        # description
        for sel in ["#productDescription", "div#aplus_feature_div", "div#productDescription_feature_div"]:
            el = self.soup.select_one(sel)
            if el:
                desc = norm_space(el.get_text())
                if desc:
                    data.description = desc
                    break

        # images
        seen: set = set()
        for img_container in self.soup.select("img[data-a-dynamic-image]"):
            raw = img_container.get("data-a-dynamic-image", "")
            urls = re.findall(r'"(https?://[^"]+)"', raw)
            for u in urls:
                if u not in seen and u.startswith("http"):
                    seen.add(u)
                    data.images.append(u)
        for img in self.soup.select("#imgTagWrapperId img, #altImages img, img#landingImage, #main-image-container img"):
            src = img.get("src") or img.get("data-src") or img.get("data-old-hires")
            if src and src.startswith("http") and src not in seen:
                seen.add(src)
                data.images.append(src)

        # badges
        for b in self.soup.select("span.badge-label, span.a-badge-label-inner, i.a-icon-amazons-choice, i.a-icon-bestseller"):
            t = norm_space(b.get_text())
            if t and t not in data.badges:
                data.badges.append(t)

        # deal badge / promo keywords
        data.deal_badge = first_nonempty(
            self._select_text("span#dealBadge_feature_div", "span.a-badge-label-inner", "span.dealBadge"),
            self._find_by_regex([r"(Lightning Deal|Blitzangebot|Prime Day|Angebot des Tages|Deal)"]),
        )
   
    def extract_shortlink(self, data: ProductData) -> None:
        """
        Extracts Amazon shortlink from id='amzn-ss-text-shortlink-textarea'
        Example:
            <textarea id="amzn-ss-text-shortlink-textarea" class="amzn-ss-text-shortlink-textarea">https://amzn.to/3xyz</textarea>
        """
        el = self.soup.select_one("#amzn-ss-text-shortlink-textarea.amzn-ss-text-shortlink-textarea")
        if el:
            shortlink = norm_space(el.get_text() or el.get("value") or "")
            if shortlink:
               data.affiliate_url = shortlink
    # --- public API ---

    def parse(self) -> ProductData:
        data = ProductData()
        self.extract_core(data)
        self.extract_prices(data)
        self.extract_availability_and_delivery(data)
        self.extract_social_proof(data)
        self.extract_ranking_and_vendor(data)
        self.extract_product_info(data)   # NEW
        self.extract_content(data)
        self.extract_shortlink(data)

        return data

# ---------------------------------------------------------------------------
# Ingestion Pipeline
# ---------------------------------------------------------------------------

class InboxPipeline:
    """
    Reads all supported files from an input folder and writes JSON outputs.
    """

    def __init__(self, inbox_dir: Path, out_dir: Path):
        self.inbox_dir = inbox_dir
        self.out_dir = out_dir
        self.out_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _read_text(fp: Path) -> str:
        try:
            return fp.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return fp.read_bytes().decode("utf-8", errors="ignore")

    def run(self) -> Tuple[int, int]:
        files = sorted([p for p in self.inbox_dir.rglob("*") if p.suffix.lower() in {".html", ".htm", ".js"}])
        if not files:
            print(f"[WARN] No .html/.htm/.js files found in: {self.inbox_dir}")
            return (0, 0)

        summary_path = self.out_dir / "summary.jsonl"
        parsed, errors = 0, 0

        with summary_path.open("w", encoding="utf-8") as summary_out:
            for fp in files:
                try:
                    raw = self._read_text(fp)
                    parser = AmazonProductParser(raw)
                    product = parser.parse()
                    product._source_file = str(fp.resolve())

                    out_json = self.out_dir / f"{fp.stem}.json"
                    with out_json.open("w", encoding="utf-8") as f:
                        json.dump(asdict(product), f, ensure_ascii=False, indent=2)

                    summary_out.write(json.dumps(asdict(product), ensure_ascii=False) + "\n")

                    parsed += 1
                    print(f"[OK] {fp.name} -> {out_json.name}")
                except Exception as e:
                    errors += 1
                    print(f"[ERR] {fp} : {e}")

        print(f"\nSummary: parsed={parsed}, errors={errors}, out={self.out_dir}")
        print(f"Summary file: {summary_path}")
        return parsed, errors

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Robust Amazon product HTML parser (offline).")
    ap.add_argument("--inbox", type=str, default="inbox", help="Input folder with HTML/JS files (default: ./inbox)")
    ap.add_argument("--out", type=str, default="out", help="Output folder for JSON files (default: ./out)")
    args = ap.parse_args()

    inbox_dir = Path(args.inbox).resolve()
    out_dir = Path(args.out).resolve()

    if not inbox_dir.exists() or not inbox_dir.is_dir():
        sys.exit(f"Input folder not found: {inbox_dir}")

    pipeline = InboxPipeline(inbox_dir, out_dir)
    pipeline.run()

if __name__ == "__main__":
    main()
