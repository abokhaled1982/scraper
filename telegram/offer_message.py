# telegram/offer_message.py
# -*- coding: utf-8 -*-
from __future__ import annotations

from html import escape
from typing import Optional, Dict, Any, List, Tuple
from pathlib import Path
from os.path import isabs, exists
from datetime import datetime
import mimetypes
import re

# ------------------------------
# Basics & Utilities
# ------------------------------

_URL_RE = re.compile(r"^https?://", re.I)

def _is_url(s: Optional[str]) -> bool:
    return bool(s and _URL_RE.match(s))

# Zarte Layout-Helfer (telegram-sicher)
_THIN = "\u2009"
_ZWSP = "\u200E"
_DIV  = ""  # keine Linien

# ------------------------------
# Schema-Helper
# ------------------------------

_NUM_RE = re.compile(r"-?\d+(?:[.,]\d+)?")

def _as_number(val: Optional[object]) -> Optional[float]:
    """
    Robust: akzeptiert 22, "22", "-22%", "22.5%", "22,5" => float
    """
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val)
    m = _NUM_RE.search(s)
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", "."))
    except Exception:
        return None

def _get_discount_percent_number(d: Dict[str, Any]) -> Optional[float]:
    """
    Liefert z.B. 22.0 für "-22%" (Vorzeichen wird als Betrag genutzt).
    """
    raw = d.get("discount_percent")
    num = _as_number(raw)
    if num is None:
        return None
    return abs(num)

def _get_rating(d: Dict[str, Any]) -> Tuple[Optional[float], Optional[int]]:
    """
    Extrahiert Bewertungswert und Anzahl.
    """
    if isinstance(d.get("rating"), dict):
        r = d["rating"]
        val = _as_number(r.get("value"))
        cnt = int(r.get("counts")) if r.get("counts") is not None else None
        return val, cnt
    # Fallback Alt-Schema
    val = _as_number(d.get("rating_value"))
    cnt = int(d.get("review_count")) if d.get("review_count") is not None else None
    return val, cnt


# ------------------------------
# Deal-Badge & Bewertung
# ------------------------------

def _badge(d: Dict[str, Any]) -> Optional[str]:
    """
    Badge skaliert mit Rabatt.
    """
    pct = _get_discount_percent_number(d) or 0.0
    if pct >= 50:
        return "🔥 <b>UNGLAUBLICHER PREISSTURZ!</b> 🔥"
    elif pct >= 35:
        return "🔥 <b>TOP-DEAL DES TAGES!</b>"
    elif pct >= 20:
        return "✨ Gutes Angebot ✨"
    return None

def _stars(val: Optional[float], cnt: Optional[int]) -> str:
    if not val:
        return ""  # nichts anzeigen, wenn keine Bewertung
    full = int(val)
    half = 1 if val - full >= 0.5 else 0
    bar = "⭐" * full + ("✩" if half else "") + "☆" * (5 - full - half)
    cnt_txt = f" ({cnt:,} Bewertungen)" if cnt else ""
    return f"{bar} <i><b>{val:.1f}/5</b></i>{cnt_txt.replace(',', ' ')}"

# ------------------------------
# Preis / Rabatt / Affiliate
# ------------------------------

def _affiliate(d: Dict[str, Any], fallback_url: str) -> str:
    return d.get("affiliate_url") or fallback_url or "https://amzn.to/42vWlQM"

def _with_utm(url: str, d: Dict[str, Any]) -> str:
    utm = d.get("utm") or {}
    if not utm:
        return url
    sep = "&" if "?" in url else "?"
    pairs = "&".join(f"{k}={v}" for k, v in utm.items())
    return f"{url}{sep}{pairs}"

# ------------------------------
# Datumsformatierung
# ------------------------------

def _fmt_expires(iso: Optional[str]) -> Optional[str]:
    if not iso:
        return None
    try:
        iso = re.sub(r"Z$", "+00:00", iso)
        dt = datetime.fromisoformat(iso)
        return dt.strftime("%d.%m. <b>%H:%M</b> Uhr")
    except Exception:
        return None

# ------------------------------
# Caption-Builder (Hauptfunktion)
# ------------------------------

def build_caption_html(
    d: Dict[str, Any],
    affiliate_fallback: str,
    *,
    style: str = "rich"
) -> str:
    """
    Erstellt die minimale, Telegram-sichere Caption ohne Produkt-Details.
    """
    title = escape((d.get("title") or "").strip())
    if len(title) > 90:
        title = title[:87] + "…"

    badge  = _badge(d)
    r_val, r_cnt = _get_rating(d)       
    rating = _stars(r_val, r_cnt)
    avail  = d.get("availability")
    url    = _with_utm(_affiliate(d, affiliate_fallback), d)
    market = d.get("market") # <-- NEU: Marktplatz holen
    # --- Coupon-Logik: Priorisiert neues Schema, Fallback auf altes ---
    coupon = d.get("coupon", {}).get("code") 
    if not coupon: 
        coupon = d.get("coupon_code")
   
    # --- Daten für den Preis- & Coupon-Block ---
    p   = (d.get("price") or {}).get("raw")
    o   = (d.get("original_price") or 0)
    pct_abs = _get_discount_percent_number(d)
    cnote  = d.get("coupon_note")
    exp    = _fmt_expires(d.get("expires_at"))

    parts: List[str] = []

    # 1. Titel
    if title:
        parts.append(f"🛍️ <b><u>{title}</u></b>")
        parts.append("\n")

    # 2. Deal-Badge
    if badge:
        parts.append(badge)
        parts.append("\n")

    # --- Integrierter Preis- & Rabatt-Block ---
    price_lines: List[str] = []
    line1: List[str] = []

    if p:
        line1.append(f"💶 <b><u>{escape(p)}</u></b>")
    if o:
        line1.append(f"  <s>{escape(o)}</s>")

    if pct_abs is not None:
        pct_val = int(round(pct_abs))
        rabatt_text = f"⬇️<b>-{pct_val}% Rabatt</b>"
        line1.append(f" {rabatt_text}")
    if line1:
        price_lines.append(" ".join(line1))

    # Coupon-Code, Hinweis und Ablaufdatum
    sub: List[str] = []
    if coupon and coupon != "N/A":
        sub.append(f"🎟️ <b>COUPON:</b> <code>{escape(str(coupon))}</code>")
    if cnote:
        sub.append(f"ℹ️ <i>{escape(str(cnote))}</i>")
    if exp:
        sub.append(f"⏱️ <b>ENDE:</b> {exp}")

    if sub:
        if price_lines:
             price_lines.append("") 
        price_lines.append("\n".join(sub))
        
    if price_lines:
        parts.append("\n".join(price_lines))
    # --- Ende Preis- & Coupon-Block ---

    parts.append("\n") # Leerzeile für Trennung zum Status

    if market and market != "N/A":
        parts.append(f"🏬 Marktplatz: <b>{escape(str(market))}</b>")

    # 3. Bewertungs- und Statusinformationen
    if rating:
        parts.append(f"⭐️ Bewertung: {rating}")
    
    # Status wird nur angezeigt, wenn er nicht leer und nicht "N/A" ist
    if avail and avail != "N/A":
        parts.append(f"✅ Status: <b>{escape(str(avail))}</b>")

    #ship = d.get("shipping") or d.get("shipping_info")
    # Versand wird nur angezeigt, wenn er nicht leer und nicht "N/A" ist
    # if ship and ship!="N/A":
    #     parts.append(f"🚚 Versand: <b>{escape(str(ship))}</b>")
     
    # 4. Call to Action (CTA)
    parts.append("\n\n")
    parts.append(f"🛒 <b><a href=\"{escape(url)}\">DIREKT ZUM ANGEBOT!</a></b> 🚀")

    # Kürzung Logik (vereinfacht, da Highlights/Details entfernt wurden)
    caption = "\n".join(parts)
    caption = re.sub(r'\n\s*\n\s*\n', '\n\n', caption).strip()
    
    if len(caption) > 1024:
        keep: List[str] = []
        used = 0
        cta = f"🛒 <b><a href=\"{escape(url)}\">DIREKT ZUM ANGEBOT!</a></b> 🚀"
        for p in parts:
            if p == cta:
                continue
            if used + len(p) + 1 < 980:
                keep.append(p)
                used += len(p) + 1
        keep.append(cta)
        caption = "\n".join(keep)
        caption = re.sub(r'\n\s*\n\s*\n', '\n\n', caption).strip()

    return caption

# ------------------------------
# Bildquelle
# ------------------------------

def pick_image_source(d: Dict[str, Any], base_dir: Path) -> Optional[str]:
    """
    Gibt den Pfad zu einem lokalen Bild zurück. 
    !!! URLs werden hier IGNORIERT, da sie nun in telRouter.py asynchron verarbeitet werden.
    """
    def _valid_img(path: str) -> bool:
        mt, _ = mimetypes.guess_type(path)
        return bool(mt and mt.startswith("image/"))

    candidates: List[str] = []
    if d.get("main_image"):
        candidates.append(d["main_image"])
    if isinstance(d.get("images"), list):
        candidates += [img for img in d["images"] if img]
    if d.get("thumbnail"):
        candidates.append(d["thumbnail"])

    # 1. Lokale Pfade suchen
    for img in candidates:
        if not img or _is_url(img): # <- NEU: Ignoriere alle URLs
            continue
        path = Path(img)
        if not path.is_absolute():
            path = (base_dir / path).resolve()
        if path.exists() and _valid_img(str(path)):
            return str(path)

    # 2. Fallback: Placeholder
    cat = (d.get("category") or "").lower().strip()
    if cat:
        ph_cat = base_dir / "assets" / f"placeholder_{cat}.jpg"
        if ph_cat.exists():
            return str(ph_cat)

    ph = base_dir / "assets" / "placeholder_square.jpg"
    if ph.exists():
        return str(ph)
    return None

# ------------------------------
# Inline-Buttons
# ------------------------------

def build_inline_keyboard(d: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    buttons = d.get("buttons")
    if isinstance(buttons, list) and buttons:
        rows = []
        row: List[Dict[str, str]] = []
        for b in buttons:
            if not b or not b.get("text") or not b.get("url"):
                continue
            row.append({"text": str(b["text"]), "url": str(b["url"])})
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        return {"inline_keyboard": rows} if rows else None

    aff = d.get("affiliate_url")
    if aff:
        return {
            "inline_keyboard": [
                [{"text": "🛒 Jetzt kaufen", "url": str(aff)}]
            ]
        }
    return None

__all__ = [
    "build_caption_html",
    "pick_image_source",
    "build_inline_keyboard",
]