# telegram/offer_message.py
# -*- coding: utf-8 -*-
from __future__ import annotations

from html import escape
from typing import Optional, Dict, Any, List
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

# Zarte Layout-Helfer (telegram-sicher, kein CSS)
_THIN = "\u2009"     # thin space
_ZWSP = "\u200E"     # very thin spacer
_DIV  = "━" * 18     # dezenter Divider

# ------------------------------
# Deal-Badge & Bewertung
# ------------------------------

def _badge(d: Dict[str, Any]) -> Optional[str]:
    """Zeigt eine Deal-Badge, wenn discount_percent >= THRESHOLD."""
    try:
        pct = float(d.get("discount_percent") or 0)
    except Exception:
        pct = 0.0
    threshold = 25
    if pct >= threshold:
        # ab 40% -> roter Alarm, sonst normal heiß
        return "🚨 <b>MEGA-DEAL</b>" if pct >= 40 else "🔥 <b>Top-Deal</b>"
    return None

def _stars(val: Optional[float], cnt: Optional[int]) -> str:
    if not val:
        return "⭐️ n/a"
    full = int(val)
    half = 1 if val - full >= 0.5 else 0
    bar = "⭐" * full + ("✩" if half else "") + "☆" * (5 - full - half)
    cnt_txt = f" • {cnt:,}".replace(",", " ") if cnt else ""
    return f"{bar} <i>{val:.1f}/5</i>{cnt_txt}"

# ------------------------------
# Preis / Rabatt / Affiliate
# ------------------------------

def _price_line(d: Dict[str, Any]) -> str:
    p   = (d.get("price") or {}).get("raw")
    o   = (d.get("original_price") or {}).get("raw")
    pct = d.get("discount_percent")
    s = f"💶 <b>{escape(p)}</b>" if p else "💶 n/a"
    if o:
        s += f"  <s>{escape(o)}</s>"
    if pct:
        try:
            s += f"  <i>(−{int(round(float(pct)))}%)</i>"
        except Exception:
            pass
    return s

def _affiliate(d: Dict[str, Any], fallback_url: str) -> str:
    return d.get("affiliate_url") or fallback_url or "https://amzn.to/42vWlQM"

def _with_utm(url: str, d: Dict[str, Any]) -> str:
    """
    Hängt optionale UTM-Parameter an affiliate_url, falls vorhanden:
    d['utm'] z.B. {'utm_source':'tg', 'utm_medium':'post', 'utm_campaign':'oct_deals'}
    """
    utm = d.get("utm") or {}
    if not utm:
        return url
    sep = "&" if "?" in url else "?"
    pairs = "&".join(f"{k}={v}" for k, v in utm.items())
    return f"{url}{sep}{pairs}"

# ------------------------------
# Sektionen/Formatter
# ------------------------------

def _fmt_expires(iso: Optional[str]) -> Optional[str]:
    """Erwartet ISO 8601 (z.B. '2025-10-11T23:59:00Z') und gibt 'DD.MM. HH:MM' zurück."""
    if not iso:
        return None
    try:
        iso = re.sub(r"Z$", "+00:00", iso)
        dt = datetime.fromisoformat(iso)
        return dt.strftime("%d.%m. %H:%M")
    except Exception:
        return None

def _facts_block(price_html: str, d: Dict[str, Any]) -> str:
    """Kompakter Faktenblock mit Preis, UVP, Rabatt, optional Coupon/Timer."""
    p   = (d.get("price") or {}).get("raw")
    o   = (d.get("original_price") or {}).get("raw")
    pct = d.get("discount_percent")
    parts: List[str] = []

    # Zeile 1: Preis fett, UVP gestrichen, Rabatt
    line1: List[str] = []
    if p:  line1.append(f"💶 <b>{escape(p)}</b>")
    if o:  line1.append(f"<s>{escape(o)}</s>")
    if pct:
        try:
            line1.append(f"(−{int(round(float(pct)))}%)")
        except Exception:
            pass
    if line1:
        parts.append(" ".join(line1))

    # Zeile 2: optional Coupon / Timer
    coupon = d.get("coupon_code")
    cnote  = d.get("coupon_note")
    exp    = _fmt_expires(d.get("expires_at"))
    sub: List[str] = []
    if coupon:
        sub.append(f"🎫 <b>Coupon:</b> <code>{escape(str(coupon))}</code>")
    if cnote:
        sub.append(escape(str(cnote)))
    if exp:
        sub.append(f"⏱️ bis {exp}")
    if sub:
        parts.append(" · ".join(sub))

    return "\n".join(parts) if parts else price_html

def _highlights_list(items: Optional[List[str]], limit: int = 4) -> Optional[str]:
    if not items:
        return None
    items = [i.strip() for i in items if i and str(i).strip()]
    if not items:
        return None
    items = items[:limit]
    bullets = "\n".join(f"• {escape(i)}" for i in items)
    return f"✨ <b>Highlights</b>\n{bullets}"

def _service_line(d: Dict[str, Any]) -> Optional[str]:
    ship = d.get("shipping")
    warr = d.get("warranty")
    bits: List[str] = []
    if ship:
        bits.append(f"🚚 {escape(str(ship))}")
    if warr:
        bits.append(f"🛡️ {escape(str(warr))}")
    return " · ".join(bits) if bits else None

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
    Professionell strukturierte Caption (HTML), telegram-sicher.
    style: 'rich' (Sektionen inkl. Highlights) oder 'compact' (kurzer Block)
    Erwartete optionale Felder in d:
      - title, availability, rating_value, review_count
      - price{raw}, original_price{raw}, discount_percent
      - coupon_code, coupon_note, expires_at (ISO 8601)
      - highlights: List[str], shipping, warranty
      - affiliate_url, utm: Dict[str,str]
    """
    title = escape((d.get("title") or "").strip())
    if len(title) > 90:
        title = title[:87] + "…"

    badge  = _badge(d)
    price  = _price_line(d)
    rating = _stars(d.get("rating_value"), d.get("review_count"))
    avail  = d.get("availability")
    url    = _with_utm(_affiliate(d, affiliate_fallback), d)

    parts: List[str] = []

    # Header
    if badge:
        parts.append(badge)
    if title:
        parts.append(f"🛍️ <b>{title}</b>")

    # Divider
    parts.append(_DIV)

    # Faktenblock (Preis + Coupon/Timer)
    parts.append(_facts_block(price, d))

    # Rating/Verfügbarkeit
    meta: List[str] = [f"⭐ {rating}"]
    if avail:
        meta.append(f"✅ <b>{escape(str(avail))}</b>")
    parts.append(" | ".join(meta))

    # Optional: Service/USP
    svc = _service_line(d)
    if svc:
        parts.append(svc)

    # Optional: Highlights (nur im 'rich' Stil)
    if style == "rich":
        hl = _highlights_list(d.get("highlights"))
        if hl:
            parts.append(hl)

    # Spacer + CTA
    parts.append(_ZWSP)
    parts.append(f"🔗 <a href=\"{escape(url)}\">Zum Angebot</a>")

    # Sicher unter Telegram-Caption-Limit (1024 Zeichen) bleiben
    caption = "\n".join(parts)
    if len(caption) > 1024 and style == "rich":
        # Highlights entfernen, falls vorhanden
        parts_no_hl = [p for p in parts if not p.startswith("✨ ")]
        caption = "\n".join(parts_no_hl)
    if len(caption) > 1024:
        # Letzte nicht-wichtige Zeilen kürzen, CTA behalten
        keep: List[str] = []
        used = 0
        cta = f"🔗 <a href=\"{escape(url)}\">Zum Angebot</a>"
        for p in parts:
            if p == cta:
                continue
            if used + len(p) + 1 < 980:  # Reserve für CTA
                keep.append(p)
                used += len(p) + 1
        keep.append(cta)
        caption = "\n".join(keep)

    return caption

# ------------------------------
# Bildquelle (erweitert)
# ------------------------------

def pick_image_source(d: Dict[str, Any], base_dir: Path) -> Optional[str]:
    """
    Gibt bevorzugt eine HTTPS-URL oder einen lokalen absoluten Pfad zurück.
    Erweiterte Version mit mehreren Quellen & Fallback-Strategien:
      - Prüft Reihenfolge: d['main_image'] > d['images'][0..] > d['thumbnail']
      - Unterstützt relative und absolute lokale Pfade
      - Erkennt valide Bildtypen (MIME)
      - Fällt auf category-basierte Platzhalter zurück (assets/placeholder_<category>.jpg)
      - Danach allgemeiner Platzhalter (assets/placeholder_square.jpg)
    """
    def _valid_img(path: str) -> bool:
        mt, _ = mimetypes.guess_type(path)
        return bool(mt and mt.startswith("image/"))

    # 1) Kandidaten sammeln
    candidates: List[str] = []
    if d.get("main_image"):
        candidates.append(d["main_image"])
    if isinstance(d.get("images"), list):
        candidates += [img for img in d["images"] if img]
    if d.get("thumbnail"):
        candidates.append(d["thumbnail"])

    # 2) erste gültige HTTPS-URL bevorzugen
    for img in candidates:
        if _is_url(img) and _valid_img(img):
            return img

    # 3) lokale Pfade durchgehen
    for img in candidates:
        if not img:
            continue
        path = Path(img)
        if not path.is_absolute():
            path = (base_dir / path).resolve()
        if path.exists() and _valid_img(str(path)):
            return str(path)

    # 4) Kategorie-Platzhalter
    cat = (d.get("category") or "").lower().strip()
    if cat:
        ph_cat = base_dir / "assets" / f"placeholder_{cat}.jpg"
        if ph_cat.exists():
            return str(ph_cat)

    # 5) allgemeiner Platzhalter
    ph = base_dir / "assets" / "placeholder_square.jpg"
    if ph.exists():
        return str(ph)

    return None

# ------------------------------
# Optional: Inline-Buttons-Build
# ------------------------------

def build_inline_keyboard(d: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Baut eine Inline-Keyboard-Struktur aus d['buttons'] oder verwendet Defaults.
    Erwartet optional:
        d['buttons'] = [
            {"text": "🛒 Jetzt kaufen", "url": "https://..."},
            {"text": "ℹ️ Details", "url": "https://..."}
        ]
    """
    buttons = d.get("buttons")
    if isinstance(buttons, list) and buttons:
        rows = []
        # einfache 2er-Reihe; passe bei Bedarf das Chunking an
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

    # Fallback: wenn es eine Affiliate-URL gibt, einen einzelnen CTA anbieten
    aff = d.get("affiliate_url")
    if aff:
        return {
            "inline_keyboard": [
                [{"text": "🛒 Jetzt kaufen", "url": str(aff)}]
            ]
        }
    return None

# ------------------------------
# __all__ (saubere Exports)
# ------------------------------

__all__ = [
    "build_caption_html",
    "pick_image_source",
    "build_inline_keyboard",
]
