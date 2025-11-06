#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
amazon_parser_pro.py
--------------------
Professional, robust, and extensible Amazon product HTML parser.

Wichtig:
- Dedizierte, robuste Preisberechnung (compute_final_price_and_discounts)
  unter Berücksichtigung sequenzieller Doppelrabatte.
- Feld discount_reason_summary für die saubere Erklärung der Preisreduzierung.
- Mapping-Schicht (to_b0_schema) für vollständige Datenübertragung.
- NEU: Logik zur Erstellung von 'rabatt_details' für UX/UI (kurz & effektiv).

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
    Normalisiert Preise (zurück auf String, da interne Berechnung Strings nutzt)
    """
    if not text:
        return None
    return text.strip()

def parse_coupon_value(text: Optional[str]) -> Dict[str, Any]:
    """
    Coupons wie '10% Coupon' oder '5 € Coupon' -> {'percent','amount','currency_hint'}
    """
    if not text:
        return {"percent": None, "amount": None, "currency_hint": None}
    t = text.strip()

    m_pct = re.search(r"(\d{1,3}(?:[.,]\d+)?)\s*%", t)
    if m_pct:
        try:
            pct = float(m_pct.group(1).replace(",", "."))
            return {"percent": pct, "amount": None, "currency_hint": None}
        except Exception:
            pass

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
    """Konvertiert Preis-String (z.B. "139,99€") in Float (z.B. 139.99)."""
    if not price_string:
        return 0.0
    
    # Entferne Währungssymbole und Whitespace
    temp_string = re.sub(r"[^\d.,]", "", price_string)
    
    # Heuristik: Bestimme das Dezimaltrennzeichen
    if temp_string.count(',') > 0 and temp_string.count('.') == 0:
        # EU-Format
        number_string = temp_string.replace('.', '').replace(',', '.')
    elif temp_string.count('.') > 0 and temp_string.count(',') == 0:
        # US-Format
        number_string = temp_string.replace(',', '')
    else:
        # Komplex (Fallback)
        if temp_string.count('.') > 0 and temp_string.rfind('.') > temp_string.rfind(','):
             number_string = temp_string.replace(',', '')
        elif temp_string.count(',') > 0 and temp_string.rfind(',') > temp_string.rfind('.'):
             number_string = temp_string.replace('.', '').replace(',', '.')
        else:
            number_string = temp_string.replace(',', '')
    
    try:
        return float(number_string)
    except ValueError:
        return 0.0


def format_discount_summary_for_ux(
    original_price_string: Optional[str],
    current_price_string: Optional[str],
    coupon_info: Optional[Dict[str, Any]],
) -> str:
    """
    Erstellt den kurzen, effektiven Rabatt-Text nach dem gewünschten UX/UI-Schema:
    'Rabatt: -26% zusätzlich -10,0 €/Kasse' oder '-10€ Guthaben'.
    """
    orig_val = parse_price_string_to_float(original_price_string)
    price_val = parse_price_string_to_float(current_price_string)
    
    # 1. Streichpreis-Rabatt-Teil (z.B. Rabatt: -26%)
    streich_rabatt_str = ""
    if orig_val > 0 and price_val > 0 and orig_val > price_val:
        rabatt_betrag_pct = orig_val - price_val
        rabatt_prozent_pct = (rabatt_betrag_pct / orig_val) * 100
        streich_rabatt_str = f"Rabatt: -{rabatt_prozent_pct:.0f}%"
        
    # 2. Coupon-Rabatt-Teil (z.B. zusätzlich -10,0 €/Kasse)
    coupon_rabatt_str = ""
    if coupon_info:
        cpct = coupon_info.get("percent")
        camt = coupon_info.get("amount")
        currency_hint = coupon_info.get('currency_hint') or '€'
        
        if cpct:
            # Beispiel: 50 % Coupon
            coupon_rabatt_str = f"zusätzlich -{cpct:.0f}%/Kasse"
        elif camt:
            # Beispiel: 10,00 € Coupon
            # Verwende formatierte Zahl und das Währungssymbol
            amount_str = f"{camt:.1f}".replace('.', ',')
            coupon_rabatt_str = f"zusätzlich -{amount_str} {currency_hint}/Kasse"

    # 3. Zusammenfügen der Teile
    if streich_rabatt_str and coupon_rabatt_str:
        # Fall: Doppelrabatt (Rabatt: -26% zusätzlich -10,0 €/Kasse)
        return f"{streich_rabatt_str} {coupon_rabatt_str}"
    elif coupon_rabatt_str:
        # Fall: Nur Coupon (z.B. -10€ Guthaben)
        if coupon_info.get("amount"):
             amount_str = f"{coupon_info.get('amount'):.0f}".replace('.', ',')
             currency_hint = coupon_info.get('currency_hint') or '€'
             return f"-{amount_str}{currency_hint} Guthaben"
        elif coupon_info.get("percent"):
             return f"-{coupon_info.get('percent'):.0f}% Coupon"
    
    # Fall: Nur Streichpreis oder kein Rabatt
    if streich_rabatt_str:
         return streich_rabatt_str
         
    return "Kein Rabatt"

def compute_final_price_and_discounts(
    original_price_string: Optional[str],
    current_price_string: Optional[str],
    coupon_info: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Berechnet den finalen Preis, den Gesamtrabatt und generiert eine Zusammenfassung.
    Berücksichtigt sequentielle Doppelrabatte (Streichpreis dann Coupon).

    Args:
        original_price_string (str): Der durchgestrichene Listenpreis (z.B. "59,97€").
        current_price_string (str): Der Preis vor Coupon (z.B. "39,97€").
        coupon_info (dict): Parsed Coupon-Werte.

    Returns:
        dict: Enthält final_price_after_coupon, discount_amount, discount_percent (total),
              und discount_reason_summary.
    """
    orig_val = parse_price_string_to_float(original_price_string)
    price_val = parse_price_string_to_float(current_price_string)

    # Initiale Werte
    final_after_coupon: float = price_val
    discount_amount: float = 0.0
    
    summary_parts: List[str] = []
    
    # 1. Streichpreis-Rabatt (Originalpreis -> Preis VOR Coupon)
    if orig_val > 0 and price_val > 0 and orig_val > price_val:
        rabatt_betrag_pct = orig_val - price_val
        rabatt_prozent_pct = (rabatt_betrag_pct / orig_val) * 100
        # Rabatt auf nächste volle Zahl runden (wie Amazon es oft macht)
        summary_parts.append(f"Preisreduzierung ({rabatt_prozent_pct:.0f}%) vom Listenpreis")
        discount_amount += rabatt_betrag_pct
        
    # 2. Coupon-Abzug (Preis VOR Coupon -> Endpreis)
    coupon_amount: Optional[float] = None
    
    if coupon_info and current_price_string and price_val > 0:
        cpct = coupon_info.get("percent")
        camt = coupon_info.get("amount")
        currency_hint = coupon_info.get('currency_hint') or '€'
        
        coupon_prefix = "Zusätzlich Coupon-Rabatt von"
        
        if cpct:
            # Prozentualer Coupon
            final_after_coupon = max(price_val * (1 - cpct / 100.0), 0)
            coupon_amount = price_val - final_after_coupon
            summary_parts.append(f"{coupon_prefix} {cpct:.0f} %")
            
        elif camt:
            # Absoluter Coupon
            coupon_amount = camt
            final_after_coupon = max(price_val - camt, 0)
            summary_parts.append(f"{coupon_prefix} {camt:.2f} {currency_hint} (fester Betrag)")

        discount_amount += (coupon_amount or 0)
        
    # 3. Finaler Gesamt-Rabatt (Originalpreis -> Endpreis)
    discount_percent_total: Optional[float] = None
    if orig_val > 0 and final_after_coupon < orig_val:
        discount_percent_total = ((orig_val - final_after_coupon) / orig_val) * 100
        
    # 4. Rabatt-Zusammenfassung generieren (Langer Text)
    if not summary_parts and price_val > 0 and orig_val == price_val:
        final_summary = "Kein Rabatt gefunden (Vollpreis)."
    elif not summary_parts and (price_val == 0 and orig_val == 0) or (price_val == 0 and orig_val > 0):
        final_summary = "Preisinformationen unvollständig oder Artikel kostenlos."
    else:
        final_summary = "Endpreis ermittelt durch: " + ". + ".join(summary_parts) + "."

    # 5. Formatierung des Endpreises
    final_after_coupon_str = f"{final_after_coupon:.2f}".replace('.', ',') + "€"
    
    def rnd(x: Optional[float]) -> Optional[float]:
        # Rundung auf zwei Dezimalstellen
        return round(x, 2) if isinstance(x, (int, float)) else None

    return {
        "discount_amount": rnd(discount_amount),
        "discount_percent": rnd(discount_percent_total), # Gesamt-Prozent (Original -> Endpreis)
        "final_price_after_coupon": final_after_coupon_str, 
        "discount_reason_summary": final_summary,
    }


def parse_discount_percent(explicit_savings: Optional[str]) -> Optional[float]:
    """
    Parses the discount percentage (e.g., '-24 %', '24%', '24') from a string
    and returns it as a positive floating-point number (absolute value).
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
    price: Optional[str] = None # String des aktuellen Preises (z.B. "39,97€", VOR Coupon)
    original_price: Optional[str] = None # String des Listenpreises (z.B. "59,97€")
    discount_amount: Optional[float] = None # Gesamt-Rabatt-Betrag (Original -> Endpreis)
    discount_percent: Optional[str] = None # Gesamt-Rabatt-Prozent (Original -> Endpreis)
    coupon_text: Optional[str] = None
    coupon_value: Dict[str, Any] = field(default_factory=dict)
    final_price_after_coupon: Optional[str] = None # Der tatsächliche Endpreis NACH allen Rabatten
    discount_reason_summary: Optional[str] = None # NEUES FELD: Zusammenfassung der Rabattgründe (Langversion)

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

    # --- Helpers (Unverändert) ---

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

    # --- Extractors (Unverändert) ---

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
        # 1. Aktueller Preis (Reduziert oder Vollpreis) - Preis VOR Coupon
        price_text = self._select_text(
            "#corePrice_feature_div span.a-price span.a-offscreen",
            "#priceblock_ourprice",
            "#priceblock_dealprice",
            "#priceblock_saleprice",
            "span.a-price.aok-align-center .a-offscreen",
        )
        data.price = clean_price(price_text) # Speichert den String

        # 2. Durchgestrichener Preis (Listenpreis / UVP)
        list_price_text = self._select_text(
            "#price span.a-text-price .a-offscreen",
            ".priceBlockStrikePriceString",
            "#priceblock_ourprice_row .a-text-strike",
            "#price-basis span.a-offscreen",
            "span.a-price.a-text-price .a-offscreen",
            "span.a-price[data-a-strike='true'] .a-offscreen",
            "td.a-span12 span.a-color-secondary.a-text-strike",
        )
        
        # 3. Bestimmung des Originalpreises (Fallback auf data.price, wenn kein Streichpreis)
        data.original_price = clean_price(list_price_text) if list_price_text else data.price

        # 4. Explizite Ersparnis (Prozent) - Nur zur Info, nicht für Berechnung
        explicit_savings = self._select_text(
                "#corePrice_feature_div span.savingsPercentage",
                ".priceBlockSavingsString",
                "span.savingPriceOverride.savingsPercentage",  
            )
       
        # 5. Coupon-Erkennung
        structured_coupon_price = self._select_text("label.ct-coupon-checkbox-label span.a-offscreen")
        coupon_text = None
        if structured_coupon_price:
            coupon_text = f"{structured_coupon_price} Rabatt"
        else:
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

        # 6. Preisberechnung (NEU: Saubere Funktion für alle Berechnungen)
        comp = compute_final_price_and_discounts(
            data.original_price, 
            data.price, 
            data.coupon_value
        )
        
        # Speichere die Ergebnisse der Berechnung in den Produktfeldern
        data.discount_amount = comp["discount_amount"]
        
        # Formatiere den Gesamt-Prozent-Rabatt als String (z.B. "-81%")
        data.discount_percent = f"-{comp['discount_percent']:.0f}%" if comp["discount_percent"] else "N/A"
        
        # Speichere den Endpreis NACH Coupon
        data.final_price_after_coupon = comp["final_price_after_coupon"]
        
        # Speichere die Zusammenfassung des Rabattgrundes
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

    # ---- PRODUCT INFO / TECH DETAILS (Unverändert) ----
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
        """
        Extrahiert Amazon-Shortlink (z.B. amzn.to) falls auf der Seite vorhanden.
        """
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
# Schema Mapping -> "BO..."-Struktur (Feldnamen werden hier auf das ZIEL-Schema gemappt)
# ---------------------------------------------------------------------------

def to_b0_schema(product: "ProductData") -> Dict[str, Any]:
    """
    Mappt ProductData -> BO…-Schema (Feldnamen + Struktur wie B0DSLBN5FS.json),
    wobei alle Felder übertragen werden.
    """
    price_obj = product.price # Aktueller Preis String (VOR Coupon)
    orig_obj  = product.original_price # Originalpreis String
    discount_percent_str =product.discount_percent if product.discount_percent else "N/A"

    # Nutze den Endpreis nach Coupon.
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

    # Coupon Details
    coboun_more = None
    if product.coupon_value:
        if product.coupon_value.get("percent"):
            try:
                coboun_more = f"Spare {int(round(product.coupon_value['percent']))} %"
            except Exception:
                coboun_more = product.coupon_text
        elif product.coupon_value.get("amount") is not None:
            amt = product.coupon_value.get("amount")
            cur = product.coupon_value.get("currency_hint") or "€"
            try:
                coboun_more = product.coupon_text
            except Exception:
                coboun_more = f"Spare {amt:.2f} {cur}".strip()
    if not coboun_more and product.coupon_text:
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

    # Technische Details (flattern/übertragen)
    tech_details_flat: Dict[str, Any] = product.product_info.get("normalized", {})

    # NEU: Generieren des kurzen UX-Textes für rabatt_details
    rabatt_kurztext = format_discount_summary_for_ux(
        product.original_price, 
        product.price, 
        product.coupon_value
    )


    out = {
        # CORE / IDENTITÄT
        "title": product.title or None,
        "market":"AMAZON",
        "affiliate_url": affiliate_url,
        "brand": product.brand or None,
        "product_id": product.asin or None,

        # PREISE / RABATTE
        "price": final_price_for_schema,        # Endpreis (NACH Coupon/Rabatt)
        "original_price": orig_obj,             # Höchster Basispreis
        "discount_amount": product.discount_amount,
        "discount_percent": discount_percent_str, # Gesamt-Rabatt in %
        "coupon_text": product.coupon_text or None,
        "coupon_value": {
            "percent": product.coupon_value.get("percent") if product.coupon_value else None,
            "amount": product.coupon_value.get("amount") if product.coupon_value else None,
            "currency_hint": product.coupon_value.get("currency_hint") if product.coupon_value else None,
        },
        # HIER WIRD DER KURZE UX-TEXT GESPEICHERT
        "rabatt_details": rabatt_kurztext, 
        # HIER WIRD DER LANGE TEXT GESPEICHERT
        "rabatt_details_lang": product.discount_reason_summary, 
        "coboun": coboun_block,

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
        "bullets": features_list, # Dupliziere für Redundanz
        "variants": product.variants,
        "badges": product.badges,
        "deal_badge": product.deal_badge,

        # TECHNISCHE DETAILS (aus 'product_info' geflattet)
        "technical_details": tech_details_flat,

        # QUELLE
        "_source_file": product._source_file,
    }
    return out

# ---------------------------------------------------------------------------
# CLI (unverändert)
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
                    # Wende das BO-Schema Mapping an
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