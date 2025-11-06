#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
amazon_parser_pro.py
--------------------
Professional, robust, and extensible Amazon product HTML parser.

Wichtig:
- Dedizierte, robuste Preisberechnung (compute_final_price_and_discounts)
  unter Berücksichtigung sequenzieller Doppelrabatte (Listenpreis -> Sichtpreis -> Kasse/Coupon).
- 'discount_reason_summary' erklärt die Schritte knapp & eindeutig.
- 'rabatt_details' liefert einen sehr kurzen, UI-tauglichen Text.
- Mapping-Schicht (to_b0_schema) gibt ein kompaktes Zielschema aus.

Benutzung (Standalone):
    python parser.py --inbox inbox --out out

Abhängigkeiten:
    pip install bs4 lxml
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
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
    """Whitespace normalisieren und trimmen."""
    return re.sub(r"\s+", " ", (s or "")).strip()

def extract_html_from_js_wrapper(raw: str) -> str:
    """
    Falls HTML in einer JS-Variable eingewickelt ist, ab <html…> oder <!doctype…> extrahieren.
    """
    m = re.search(r"(<\s*!doctype\b.*?>|<\s*html\b)", raw, flags=re.I | re.S)
    return raw[m.start():] if m else raw

def clean_price(text: Optional[str]) -> Optional[str]:
    """
    Normalisiert Preise (zurück auf String, da interne Berechnung Strings nutzt).
    """
    if not text:
        return None
    return text.strip()

def parse_coupon_value(text: Optional[str]) -> Dict[str, Any]:
    """
    Coupons/Kassenhinweise wie '10% Coupon' oder '5 € an der Kasse' -> {'percent','amount','currency_hint'}
    Prozent hat Vorrang, wenn beides vorkommt.
    """
    if not text:
        return {"percent": None, "amount": None, "currency_hint": None}

    t = text.strip()

    # Prozent z. B. "10%", "10 %"
    m_pct = re.search(r"(\d{1,3}(?:[.,]\d+)?)\s*%", t)
    if m_pct:
        try:
            pct = float(m_pct.group(1).replace(",", "."))
            return {"percent": pct, "amount": None, "currency_hint": None}
        except Exception:
            pass

    # Absolutbetrag inkl. Währung
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

def parse_price_string_to_float(price_string: Optional[str]) -> float:
    """Konvertiert Preis-String (z.B. '139,99€') in Float (z.B. 139.99)."""
    if not price_string:
        return 0.0

    temp_string = re.sub(r"[^\d.,]", "", price_string)

    # Heuristik: EU vs. US
    if temp_string.count(",") > 0 and temp_string.count(".") == 0:
        number_string = temp_string.replace(".", "").replace(",", ".")
    elif temp_string.count(".") > 0 and temp_string.count(",") == 0:
        number_string = temp_string.replace(",", "")
    else:
        if temp_string.count(".") > 0 and temp_string.rfind(".") > temp_string.rfind(","):
            number_string = temp_string.replace(",", "")
        elif temp_string.count(",") > 0 and temp_string.rfind(",") > temp_string.rfind("."):
            number_string = temp_string.replace(".", "").replace(",", ".")
        else:
            number_string = temp_string.replace(",", "")

    try:
        return float(number_string)
    except ValueError:
        return 0.0

# ---------------------------------------------------------------------------
# Rabatt-Label (kurz) – kompakt & eindeutig für die UI
# ---------------------------------------------------------------------------

def build_short_discount_label(
    original_price_string: Optional[str],
    current_price_string: Optional[str],
    coupon_info: Optional[Dict[str, Any]],
    kasse_info: Optional[Dict[str, Any]],
) -> str:
    """
    Erzeugt einen sehr kurzen, eindeutigen Rabatt-Text, z. B.:
      '-33% + -5€/Kasse + -10% Coupon'
    Falls keine Reduktion erkennbar: 'Kein Rabatt'
    """
    parts: List[str] = []

    # Listenpreis -> Sichtpreis
    o = parse_price_string_to_float(original_price_string)
    p = parse_price_string_to_float(current_price_string)
    if o > 0 and p > 0 and o > p:
        pct = round((o - p) / o * 100)
        if pct > 0:
            parts.append(f"-{pct}%")

    # Kasse (Prozent, dann Betrag)
    if kasse_info:
        k_pct = kasse_info.get("percent")
        k_amt = kasse_info.get("amount")
        k_cur = (kasse_info.get("currency_hint") or "€").replace("EUR", "€")
        if k_pct:
            parts.append(f"-{int(round(k_pct))}%/Kasse")
        if k_amt:
            parts.append(f"-{int(round(k_amt))}{k_cur}/Kasse")

    # Coupon (Prozent, dann Betrag)
    if coupon_info:
        c_pct = coupon_info.get("percent")
        c_amt = coupon_info.get("amount")
        c_cur = (coupon_info.get("currency_hint") or "€").replace("EUR", "€")
        if c_pct:
            parts.append(f"-{int(round(c_pct))}% Coupon")
        if c_amt:
            parts.append(f"-{int(round(c_amt))}{c_cur} Coupon")

    return " + ".join(parts) if parts else "Kein Rabatt"

# ---------------------------------------------------------------------------
# Finale Preisberechnung (einzige, robuste Funktion)
# ---------------------------------------------------------------------------

def compute_final_price_and_discounts(
    original_price_string: Optional[str],
    current_price_string: Optional[str],
    coupon_info: Optional[Dict[str, Any]] = None,
    kasse_info: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Berechnet Endpreis + Gesamtrabatt sequentiell und deterministisch.

    Reihenfolge:
      1) Basispreis = current_price (wenn fehlend -> original_price; sonst 0)
      2) Prozentuale Checkout-Rabatte (erst Kasse %, dann Coupon %)
      3) Feste Beträge (erst Kasse €, dann Coupon €)

    Rückgabe:
      - final_price_after_coupon (formatiert, EUR)
      - discount_amount (Gesamtersparnis in €)
      - discount_percent (Gesamt in %, relativ zum Listenpreis – wenn vorhanden)
      - discount_reason_summary (knappe Erklärung der Schritte)
    """
    def _fmt_eur(value: float) -> str:
        return f"{value:.2f}".replace(".", ",") + "€"

    def _apply_percent(base: float, pct: float) -> Tuple[float, float]:
        new_price = max(base * (1 - pct / 100.0), 0.0)
        return new_price, base - new_price

    def _apply_amount(base: float, amt: float) -> Tuple[float, float]:
        new_price = max(base - amt, 0.0)
        return new_price, base - new_price

    def _read(info: Optional[dict]) -> Tuple[Optional[float], Optional[float], str]:
        if not info:
            return None, None, "€"
        return info.get("percent"), info.get("amount"), (info.get("currency_hint") or "€").replace("EUR", "€")

    # Eingänge normalisieren
    orig_val = parse_price_string_to_float(original_price_string)
    price_val = parse_price_string_to_float(current_price_string)
    base_price = price_val or orig_val or 0.0

    # Streichpreis-Info (nur für Darstellung/Prozent-Total)
    list_reduction_pct = None
    if orig_val > 0 and price_val > 0 and orig_val > price_val:
        list_reduction_pct = (orig_val - price_val) / orig_val * 100.0

    k_pct, k_amt, k_cur = _read(kasse_info)
    c_pct, c_amt, c_cur = _read(coupon_info)

    # Sequentielle Anwendung
    running = base_price
    total_checkout_amt = 0.0
    steps: List[str] = []

    if list_reduction_pct is not None and list_reduction_pct > 0:
        steps.append(f"Reduzierung vom Listenpreis (-{list_reduction_pct:.0f}%)")

    # Prozent zuerst (Kasse → Coupon)
    if k_pct:
        running, saved = _apply_percent(running, float(k_pct))
        total_checkout_amt += saved
        steps.append(f"Kasse: -{float(k_pct):.0f}%")

    if c_pct:
        running, saved = _apply_percent(running, float(c_pct))
        total_checkout_amt += saved
        steps.append(f"Coupon: -{float(c_pct):.0f}%")

    # Danach feste Beträge (Kasse → Coupon)
    if k_amt:
        running, saved = _apply_amount(running, float(k_amt))
        total_checkout_amt += saved
        steps.append(f"Kasse: -{float(k_amt):.2f} {k_cur}")

    if c_amt:
        running, saved = _apply_amount(running, float(c_amt))
        total_checkout_amt += saved
        steps.append(f"Coupon: -{float(c_amt):.2f} {c_cur}")

    final_price = running

    # Gesamt-Rabatt in € und %
    if orig_val > 0:
        total_discount_amt = orig_val - final_price
        total_discount_pct = max(min((total_discount_amt / orig_val) * 100.0, 100.0), 0.0)
    else:
        total_discount_amt = base_price - final_price
        total_discount_pct = None  # ohne Listenpreis ist %-Angabe nicht sinnvoll

    long_summary = "Endpreis ermittelt durch: " + " + ".join(steps) + "." if steps else "Kein Rabatt gefunden (Vollpreis)."

    return {
        "discount_amount": round(total_discount_amt, 2),
        "discount_percent": round(total_discount_pct, 2) if total_discount_pct is not None else None,
        "final_price_after_coupon": _fmt_eur(final_price),
        "discount_reason_summary": long_summary,
    }

def parse_discount_percent(explicit_savings: Optional[str]) -> Optional[float]:
    """
    Extrahiert Prozent (z. B. '-24 %', '24%', '24') als positive Zahl.
    """
    if not explicit_savings:
        return None
    m = re.search(r"(\-?\d{1,3}[.,]?\d?)\s?%", explicit_savings)
    if m:
        try:
            return abs(float(m.group(1).replace(",", ".")))
        except Exception:
            return None
    return None

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
    # Identität
    title: Optional[str] = None
    brand: Optional[str] = None
    asin: Optional[str] = None

    # Preise
    price: Optional[str] = None  # Sichtpreis (VOR Coupon/Kasse)
    original_price: Optional[str] = None  # Listenpreis/UVP (durchgestrichen)
    discount_amount: Optional[float] = None  # Gesamtersparnis (Original -> Endpreis)
    discount_percent: Optional[str] = None   # Gesamtersparnis in % (als String, z. B. "-33%")
    coupon_text: Optional[str] = None
    kasse_rabatt_text: Optional[str] = None
    kasse_rabatt_value: Dict[str, Any] = field(default_factory=dict)  # <-- Korrigierter Typ
    coupon_value: Dict[str, Any] = field(default_factory=dict)
    final_price_after_coupon: Optional[str] = None  # Endpreis NACH allen Rabatten
    discount_reason_summary: Optional[str] = None   # Erklärung der Preisfindung

    # Verfügbarkeit / Versand
    availability: Optional[str] = None
    is_prime: bool = False
    shipping_cost_text: Optional[str] = None
    delivery_date: Optional[str] = None
    free_returns: Optional[bool] = None
    returns_days: Optional[int] = None
    returns_text: Optional[str] = None

    # Social Proof
    rating_value: Optional[float] = None
    review_count: Optional[int] = None
    answered_questions: Optional[int] = None
    purchases_past_month: Optional[int] = None
    purchases_past_month_is_plus: Optional[bool] = None
    purchases_past_month_raw: Optional[str] = None

    # Ranking / Händler
    bestseller_rank: Optional[str] = None
    seller_name: Optional[str] = None
    seller_rating_percent: Optional[int] = None
    buybox_sold_by: Optional[str] = None

    # Inhalte
    variants: Dict[str, str] = field(default_factory=dict)
    bullets: List[str] = field(default_factory=list)
    description: Optional[str] = None
    images: List[str] = field(default_factory=list)
    badges: List[str] = field(default_factory=list)
    deal_badge: Optional[str] = None

    # Produktinfos (Technische Daten)
    product_info: Dict[str, Any] = field(default_factory=dict)

    # Quelle
    _source_file: Optional[str] = None

# ---------------------------------------------------------------------------
# Amazon Product Parser
# ---------------------------------------------------------------------------

class AmazonProductParser:
    """Parser für eine einzelne Amazon-Produktseite (offline HTML)."""

    def __init__(self, html_text: str):
        self.html_text = extract_html_from_js_wrapper(html_text)
        try:
            self.soup = BeautifulSoup(self.html_text, "lxml")
        except Exception:
            self.soup = BeautifulSoup(self.html_text, "html.parser")

    # --- Helpers ---

    def _select_text(self, *selectors: str) -> Optional[str]:
        """Erstes matchendes Element aus Selectors holen, normalisierten Text zurückgeben."""
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

    # --- Extractors ---

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
        # ASIN
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
        """
        Extrahiert Sichtpreis/Listenpreis + Coupon und 'an der Kasse' und berechnet den Endpreis.
        """
        # 1) Aktueller Sichtpreis (VOR Checkout-Rabatten)
        price_text = self._select_text(
            "#corePrice_feature_div span.a-price span.a-offscreen",
            "#priceblock_ourprice",
            "#priceblock_dealprice",
            "#priceblock_saleprice",
            "span.a-price.aok-align-center .a-offscreen",
        )
        data.price = clean_price(price_text)

        # 2) Durchgestrichener Preis (Listenpreis/UVP)
        list_price_text = self._select_text(
            "span.basisPrice .a-offscreen",
            "#price span.a-text-price .a-offscreen"
        )
        data.original_price = clean_price(list_price_text) if list_price_text else None

        # 3) Coupon (z. B. "10% Coupon" oder "5 € Coupon")
        coupon_text = self._select_text(
            "span.couponLabelText",
            "span.couponBadge",
            "span#couponText",
        )
        data.coupon_text = norm_space(coupon_text) if coupon_text else None
        data.coupon_value = parse_coupon_value(data.coupon_text)

        # 4) Rabatt an der Kasse (Success-Alert o.ä.)
        kasse_text = self._select_text(
            "span[data-csa-c-type='item'] div.a-alert-inline-success div.a-alert-content",
            "div.a-box-inner.a-alert-container .a-alert-content",
            "#promoPriceBlockMessage_feature_div",
            "#couponFeatureDiv li",
            "div#promo_feature_div span",
            "div#coupon_feature_div span",
        )
        data.kasse_rabatt_text = norm_space(kasse_text) if kasse_text else None
        data.kasse_rabatt_value = parse_coupon_value(data.kasse_rabatt_text)

        # 5) Fallback: Wenn gar kein Listenpreis existiert, lassen wir ihn None (Prozent dann 'N/A')
        #    Die Berechnung nutzt in diesem Fall den Sichtpreis als Basis.

        # 6) Finale Berechnung
        comp = compute_final_price_and_discounts(
            data.original_price,
            data.price,
            data.coupon_value,
            data.kasse_rabatt_value
        )

        # Ergebnisse speichern
        data.discount_amount = comp["discount_amount"]
        if comp["discount_percent"] is not None:
            data.discount_percent = f"-{comp['discount_percent']:.0f}%"
        else:
            data.discount_percent = "N/A"
        data.final_price_after_coupon = comp["final_price_after_coupon"]
        data.discount_reason_summary = comp["discount_reason_summary"]

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
            self._find_by_regex([r"(Kostenlose Rückgabe|free returns)"]),
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

        # „Gekauft im letzten Monat“ (z. B. „100+ gekauft …“)
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
        """De/En-Schlüssel → stabile snake_case Keys."""
        k_norm = norm_space(k).lower()
        mapping = {
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
        if k_norm in mapping:
            return mapping[k_norm]
        k_norm = re.sub(r"[^a-z0-9]+", "_", k_norm).strip("_")
        return k_norm

    def _collect_key_values(self, container) -> Dict[str, str]:
        """
        Liest Key/Value-Paare aus diversen Layouts (Tabellen, dl/dd, po-rows).
        """
        kv: Dict[str, str] = {}

        # 1) Klassisch: th/td
        for row in container.select("tr"):
            th = row.find("th")
            td = row.find("td")
            if th and td:
                key = norm_space(th.get_text())
                val = norm_space(td.get_text())
                if key and val:
                    kv[key] = val

        # 2) dl/dt/dd
        for dl in container.select("dl"):
            dts = dl.select("dt")
            dds = dl.select("dd")
            for dt, dd in zip(dts, dds):
                key = norm_space(dt.get_text())
                val = norm_space(dd.get_text())
                if key and val:
                    kv[key] = val

        # 3) Bullets mit bold key
        for li in container.select("li"):
            bold = li.select_one("span.a-text-bold")
            if bold:
                key = norm_space(bold.get_text()).rstrip(":")
                val = norm_space(li.get_text().replace(bold.get_text(), "", 1))
                if key and val:
                    kv[key] = val

        # 4) Neues "po-" Layout
        for row in container.select("div.po-row, div.po-item, tr.po-row"):
            lab = row.select_one(".po-attribute-name, .a-span3, th")
            val = row.select_one(".po-attribute-value, .a-span9, td")
            key = norm_space(lab.get_text()) if lab else None
            value = norm_space(val.get_text()) if val else None
            if key and value:
                kv[key] = value

        return kv

    def extract_product_info(self, data: ProductData) -> None:
        """Produktinformationen / Technische Daten einsammeln."""
        info_blocks = []

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

        for el in self.soup.select("div#poExpander, div#poDocumentCarousel, div#technicalSpecifications_feature_div"):
            info_blocks.append(el)

        raw_kv: Dict[str, str] = {}
        for blk in info_blocks:
            raw_kv.update(self._collect_key_values(blk))

        normalized: Dict[str, Any] = {}
        for k, v in raw_kv.items():
            nk = self._normalize_info_key(k)
            if nk in normalized and normalized[nk] != v:
                suffix = 2
                while f"{nk}__alt{suffix}" in normalized:
                    suffix += 1
                normalized[f"{nk}__alt{suffix}"] = v
            else:
                normalized[nk] = v

        if "manufacturer" not in normalized and data.brand:
            normalized["manufacturer"] = data.brand
        if "asin" not in normalized and data.asin:
            normalized["asin"] = data.asin

        data.product_info = {"normalized": normalized}

    def extract_content(self, data: ProductData) -> None:
        # Varianten
        for block in self.soup.select("div#twister, div#variation_size_name, div#variation_color_name"):
            label = block.select_one("label")
            lab = norm_space(label.get_text()) if label else None
            selected = block.select_one("span.selection")
            sel = norm_space(selected.get_text()) if selected else None
            if lab or sel:
                data.variants[lab or "variant"] = sel

        # Bullets
        bullets = [
            norm_space(li.get_text())
            for li in self.soup.select("#feature-bullets li:not(.aok-hidden)")
            if norm_space(li.get_text())
        ]
        data.bullets = bullets

        # Beschreibung
        for sel in ["#productDescription", "div#aplus_feature_div", "div#productDescription_feature_div"]:
            el = self.soup.select_one(sel)
            if el:
                desc = norm_space(el.get_text())
                if desc:
                    data.description = desc
                    break

        # Bilder
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

        # Badges
        for b in self.soup.select("span.badge-label, span.a-badge-label-inner, i.a-icon-amazons-choice, i.a-icon-bestseller"):
            t = norm_space(b.get_text())
            if t and t not in data.badges:
                data.badges.append(t)

        # Deal-Hinweise
        data.deal_badge = first_nonempty(
            self._select_text("span#dealBadge_feature_div", "span.a-badge-label-inner", "span.dealBadge"),
            self._find_by_regex([r"(Lightning Deal|Blitzangebot|Prime Day|Angebot des Tages|Deal)"]),
        )

    def extract_shortlink(self, data: ProductData) -> None:
        """Extrahiert Amazon-Shortlink (z. B. amzn.to), falls vorhanden."""
        el = self.soup.select_one("#amzn-ss-text-shortlink-textarea.amzn-ss-text-shortlink-textarea")
        if el:
            shortlink = norm_space(el.get_text() or el.get("value") or "")
            if shortlink:
                data.product_info["shortlink"] = shortlink

    # --- Public API ---

    def parse(self) -> ProductData:
        data = ProductData()
        self.extract_core(data)
        self.extract_prices(data)
        self.extract_availability_and_delivery(data)
        self.extract_social_proof(data)
        self.extract_ranking_and_vendor(data)
        self.extract_product_info(data)
        self.extract_content(data)
        self.extract_shortlink(data)
        return data

# ---------------------------------------------------------------------------
# Schema Mapping -> Ziel-Struktur
# ---------------------------------------------------------------------------

def to_b0_schema(product: "ProductData") -> Dict[str, Any]:
    """
    Mappt ProductData -> Zielschema (kompakt & UI-freundlich).
    """
    price_obj = product.price
    orig_obj = product.original_price
    discount_percent_str = product.discount_percent if product.discount_percent else "N/A"

    # Endpreis ist der relevante Preis fürs Frontend
    final_price_for_schema = product.final_price_after_coupon

    # Rating Block
    rating_block = None
    if product.rating_value is not None or product.review_count is not None:
        rating_block = {"value": product.rating_value, "counts": product.review_count}

    # Features / Bullets
    features_list = product.bullets or []
    feature_text = " • ".join(features_list) if features_list else None
    description_text = product.description or None

    # Units Sold
    units_sold: Optional[str] = None
    if product.purchases_past_month is not None:
        units_sold = f"{product.purchases_past_month}{'+' if product.purchases_past_month_is_plus else ''} im letzten Monat"
    elif product.purchases_past_month_raw:
        units_sold = product.purchases_past_month_raw

    # Coupon-Block (nur zur Anzeige; Code nicht geparst)
    coboun_more = None
    if product.coupon_text:
        coboun_more = product.coupon_text
    coboun_block = {"code": "N/A", "code_details": "N/A", "more": coboun_more}

    # Affiliate URL / Shortlink
    affiliate_url = None
    if product.asin:
        affiliate_url = f"https://www.amazon.de/dp/{product.asin}"
    try:
        shortlink = product.product_info.get("shortlink")
        if shortlink:
            affiliate_url = shortlink
    except Exception:
        pass

    # Technische Details (geflattet)
    tech_details_flat: Dict[str, Any] = product.product_info.get("normalized", {})

    # Kurzer Rabatt-Text (komplett, inkl. Kasse & Coupon)
    rabatt_kurztext = build_short_discount_label(
        product.original_price,
        product.price,
        product.coupon_value,
        product.kasse_rabatt_value
    )

    out = {
        # CORE / IDENTITÄT
        "title": product.title or None,
        "market": "AMAZON",
        "affiliate_url": affiliate_url,
        "brand": product.brand or None,
        "product_id": product.asin or None,

        # PREISE / RABATTE
        "price": final_price_for_schema,          # Endpreis (NACH allen Rabatten)
        "original_price": orig_obj,               # Listenpreis (falls vorhanden)
        "discount_amount": product.discount_amount,
        "discount_percent": discount_percent_str, # Gesamt-Rabatt in %
        "coupon_text": product.coupon_text or None,
        "coupon_value": {
            "percent": product.coupon_value.get("percent") if product.coupon_value else None,
            "amount": product.coupon_value.get("amount") if product.coupon_value else None,
            "currency_hint": product.coupon_value.get("currency_hint") if product.coupon_value else None,
        },
        "rabatt_details": rabatt_kurztext,                 # kurz & eindeutig
        "rabatt_details_lang": product.discount_reason_summary,  # Erklärung

        # SOCIAL PROOF
        "rating": rating_block,
        "review_count": product.review_count,
        "answered_questions": product.answered_questions,
        "units_sold": units_sold,
        "purchases_past_month": product.purchases_past_month,
        "purchases_past_month_is_plus": product.purchases_past_month_is_plus,

        # HÄNDLER / VERFÜGBARKEIT
        "seller_name": product.seller_name or None,
        "buybox_sold_by": product.buybox_sold_by or None,
        "seller_rating_percent": product.seller_rating_percent,
        "bestseller_rank_text": product.bestseller_rank,
        "availability": product.availability or None,
        "is_prime": product.is_prime,
        "shipping_info": product.shipping_cost_text or None,
        "delivery_date": product.delivery_date or None,
        "returns_text": product.returns_text,
        "free_returns": product.free_returns,

        # INHALT / MEDIEN
        "images": product.images or [],
        "features": features_list,
        "feature_text": feature_text,
        "description": description_text,
        "bullets": features_list,  # Dupliziert für Redundanz
        "variants": product.variants,
        "badges": product.badges,
        "deal_badge": product.deal_badge,

        # TECHNISCHE DETAILS
        "technical_details": tech_details_flat,

        # QUELLE
        "_source_file": product._source_file,
    }
    return out

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

class InboxPipeline:
    """
    Liest HTML aus --inbox und schreibt JSON nach --out.
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
                    # Zielschema anwenden
                    data = to_b0_schema(product)
                    with out_json.open("w", encoding="utf-8") as f:
                        json.dump(data, f, ensure_ascii=False, indent=2)

                    summary_out.write(json.dumps(data, ensure_ascii=False) + "\n")

                    parsed += 1
                    print(f"[OK] {fp.name} -> {out_json.name}")
                except Exception as e:
                    errors += 1
                    print(f"[ERR] {fp} : {e}")

        print(f"\nSummary: parsed={parsed}, errors={errors}, out={self.out_dir}")
        print(f"Summary file: {summary_path}")
        return parsed, errors

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
