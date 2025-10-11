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
# _DIV wird jetzt entfernt oder durch einen einfachen Abstand ersetzt
_DIV  = "" # KEINE VISUELLEN TRENNLINIEN MEHR

# ------------------------------
# Deal-Badge & Bewertung (ANGEPASST FÜR 50%+)
# ------------------------------

def _badge(d: Dict[str, Any]) -> Optional[str]:
    """Zeigt eine Deal-Badge mit aggressiverer Emoj-Skalierung."""
    try:
        pct = float(d.get("discount_percent") or 0)
    except Exception:
        pct = 0.0
    
    # NEU: Skalierung der Emojis basierend auf dem Rabatt
    if pct >= 50:
        # >50% Rabatt: Roter Alarm, dreifache Flammen, starker Aufruf
        return "🔥🔥 <b>UNGLAUBLICHER PREISSTURZ!</b> 🔥🔥"
    elif pct >= 35:
        # 35-49% Rabatt: Super-Deal
        return "🔥 <b>TOP-DEAL DES TAGES!</b> "
    elif pct >= 20:
        # 20-34% Rabatt: Solider Deal
        return "✨✨ Gutes Angebot ✨✨"
    return None

def _stars(val: Optional[float], cnt: Optional[int]) -> str:
    if not val:
        return "⭐ n/a"
    full = int(val)
    half = 1 if val - full >= 0.5 else 0
    # Sterne mit Fettschrift im Wert für mehr Betonung
    bar = "⭐" * full + ("✩" if half else "") + "☆" * (5 - full - half)
    cnt_txt = f" ({cnt:,} Bewertungen)" if cnt else ""
    # Bewertung kursiv + fett, um die "Farbe" des Textes zu simulieren
    return f"{bar} <i><b>{val:.1f}/5</b></i>{cnt_txt.replace(',', ' ')}"

# ------------------------------
# Preis / Rabatt / Affiliate (UNVERÄNDERT – ersetzt durch _facts_block)
# ------------------------------

def _price_line(d: Dict[str, Any]) -> str:
    p   = (d.get("price") or {}).get("raw")
    o   = (d.get("original_price") or {}).get("raw")
    pct = d.get("discount_percent")
    s = f"💰 <b>{escape(p)}</b>" if p else "💰 n/a"
    if o:
        s += f"  <i><s>{escape(o)}</s></i>"
    if pct:
        try:
            s += f" <b>({int(round(float(pct)))}% Rabatt!)</b>"
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
# Sektionen/Formatter (GEÄNDERT FÜR BESSERE TRENNUNG UND BETONUNG)
# ------------------------------

def _fmt_expires(iso: Optional[str]) -> Optional[str]:
    """Erwartet ISO 8601 und gibt 'DD.MM. HH:MM' zurück."""
    if not iso:
        return None
    try:
        iso = re.sub(r"Z$", "+00:00", iso)
        dt = datetime.fromisoformat(iso)
        # Betonung der Uhrzeit
        return dt.strftime("%d.%m. <b>%H:%M</b> Uhr")
    except Exception:
        return None

def _facts_block(price_html: str, d: Dict[str, Any]) -> str:
    """Kompakter Faktenblock mit Preis, UVP, Rabatt, optional Coupon/Timer."""
    p   = (d.get("price") or {}).get("raw")
    o   = (d.get("original_price") or {}).get("raw")
    pct = d.get("discount_percent")
    parts: List[str] = []
    
    # 1. Zeile: PREIS-Highlights
    line1: List[str] = []
    
    # Preis: STARK FETT & UNTERSTRICHEN (Simuliert größere/rote Schrift)
    if p:
        line1.append(f"💶 <b><u>{escape(p)}</u></b>") 
    
    if o:
        # Durchgestrichener Originalpreis
        line1.append(f"  <s>{escape(o)}</s>")
    
    # Rabatt: NEUE AGGRESSIVE FORMATIERUNG
    if pct:
        try:
            pct_val = int(round(float(pct)))
            if pct_val >= 50:
                # >50% Rabatt: Roter "Alarm" mit Doppel-Pfeil und UNTERSTRICHEN
                rabatt_text = f"🔥🔥 <b><u>-{pct_val}% ERSPARNIS</u></b>"
            elif pct_val >= 35:
                 # 35-49%: Doppel-Pfeil und Fett
                rabatt_text = f"⬇️⬇️ <b>-{pct_val}% Rabatt</b>"
            else:
                 # Standard-Rabatt
                rabatt_text = f"➖ <i>-{pct_val}%</i>"
                
            line1.append(f" {rabatt_text}")
        except Exception:
            pass
            
    # Fügt die Preis-Highlights zusammen, getrennt durch ein großes Leerzeichen
    if line1:
        parts.append(" ".join(line1))

    # 2. Zeile: Coupon / Timer (mit zusätzlicher Leerzeile, wenn vorhanden)
    coupon = d.get("coupon_code")
    cnote  = d.get("coupon_note")
    exp    = _fmt_expires(d.get("expires_at"))
    sub: List[str] = []
    
    if coupon:
        # Coupon Code: mit Block-Schriftart und Betonung des Coupons
        sub.append(f"🎟️ **COUPON:** <code>{escape(str(coupon))}</code>")
    if cnote:
        # Wichtiger Coupon-Hinweis (kursiv)
        sub.append(f"ℹ️ <i>{escape(str(cnote))}</i>")
    if exp:
        # Ablaufdatum: mit Timer-Emoji und Fettschrift
        sub.append(f"⏱️ **ENDE:** {exp}")
        
    if sub:
        # Mehr Abstand, um den Coupon-Block hervorzuheben
        parts.append("") 
        parts.append("\n".join(sub))
        
    # KEIN ZUSÄTZLICHER ABSATZ HIER, WIRD IM CAPTION-BUILDER GEMACHT
    return "\n".join(parts) if parts else price_html

def _highlights_list(items: Optional[List[str]], limit: int = 4) -> Optional[str]:
    if not items:
        return None
    items = [i.strip() for i in items if i and str(i).strip()]
    if not items:
        return None
    items = items[:limit]
    # Aufzählungszeichen mit Häkchen-Emoji für mehr Attraktivität
    bullets = "\n".join(f"👉 {escape(i)}" for i in items)
    # Titel der Highlights in Fettschrift und klarer Abstand
    return f"⭐ <b>DETAILS:</b>\n{bullets}"

def _service_line(d: Dict[str, Any]) -> Optional[str]:
    ship = d.get("shipping")
    warr = d.get("warranty")
    bits: List[str] = []
    if ship:
        # Lieferinformationen: Fett und mit Emojis
        bits.append(f"🚚 Versand: <b>{escape(str(ship))}</b>")
    if warr:
        # Garantie: Fett und mit Emojis
        bits.append(f"🛡️ Garantie: <b>{escape(str(warr))}</b>")
    return " | ".join(bits) if bits else None

# ------------------------------
# Caption-Builder (Hauptfunktion - VIEL MEHR ABSTÄNDE)
# ------------------------------

def build_caption_html(
    d: Dict[str, Any],
    affiliate_fallback: str,
    *,
    style: str = "rich"
) -> str:
    """
    Professionell strukturierte Caption (HTML), telegram-sicher, mit viel Platz und Fokus auf den Deal.
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

    # 1. Header (Badge & Titel) - MAXIMALE BETONUNG
  
    if title:
        parts.append(f"🛍️ <b><u>{title}</u></b>") # Titel unterstrichen
        parts.append("\n") # GROSSER ABSATZ ZUM PREISBLOCK
   

    if badge:
        parts.append(badge)
        parts.append("\n") # GROSSER ABSATZ ZUM PREISBLOCK
    

    # 2. Faktenblock (Preis + Coupon/Timer) - DAS ZENTRUM
    facts_block = _facts_block(price, d).strip()
    if facts_block:
        parts.append(facts_block)

    parts.append("\n") # GROSSER ABSATZ ZUR META-INFO

    # 3. Rating/Verfügbarkeit
    parts.append(f"⭐️ Bewertung: {rating}")
    if avail:
        # Verfügbarkeit: Fette Schrift
       parts.append(f"✅ Status: <b>{escape(str(avail))}</b>")
    
    # 4. Optional: Service/USP
    svc = _service_line(d)
    if svc:
        parts.append("") # Leerer Abstand
        parts.append(svc)

    # 5. Optional: Highlights (nur im 'rich' Stil)
    if style == "rich":
        hl = _highlights_list(d.get("highlights"))
        if hl:
            parts.append("\n") # Großer Abstand vor Highlights
            parts.append(hl)
            
    # 6. CTA - DER WICHTIGSTE BLOCK
    parts.append("\n\n") # MAXIMALER ABSTAND
   
    # CTA: Mit prominentem Kaufen-Emoji und Fettschrift
    parts.append(f"🛒 <b><a href=\"{escape(url)}\">DIREKT ZUM ANGEBOT!</a></b> 🚀")
   


    # Sicher unter Telegram-Caption-Limit (1024 Zeichen) bleiben (unveränderte Logik)
    caption = "\n".join(parts)
    if len(caption) > 1024 and style == "rich":
        # Highlights entfernen, falls vorhanden
        parts_no_hl = [p for p in parts if not p.startswith("⭐ ")]
        caption = "\n".join(parts_no_hl)
    if len(caption) > 1024:
        # Letzte nicht-wichtige Zeilen kürzen, CTA behalten
        keep: List[str] = []
        used = 0
        cta = f"🛒 <b><a href=\"{escape(url)}\">DIREKT ZUM ANGEBOT!</a></b> 🚀"
        for p in parts:
            if p == cta:
                continue
            if used + len(p) + 1 < 980:  # Reserve für CTA
                keep.append(p)
                used += len(p) + 1
        keep.append(cta)
        caption = "\n".join(keep)
        
    # Entferne überflüssige aufeinanderfolgende Leerzeilen (zwei oder mehr)
    # Dies ist eine einfache Heuristik, um das finale Ergebnis aufzuräumen
    caption = re.sub(r'\n\s*\n\s*\n', '\n\n', caption).strip()

    return caption

# ------------------------------
# Bildquelle (erweitert) (UNVERÄNDERT)
# ------------------------------

def pick_image_source(d: Dict[str, Any], base_dir: Path) -> Optional[str]:
    """
    Gibt bevorzugt eine HTTPS-URL oder einen lokalen absoluten Pfad zurück.
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
# Optional: Inline-Buttons-Build (UNVERÄNDERT)
# ------------------------------

def build_inline_keyboard(d: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Baut eine Inline-Keyboard-Struktur aus d['buttons'] oder verwendet Defaults.
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
# __all__ (saubere Exports) (UNVERÄNDERT)
# ------------------------------

__all__ = [
    "build_caption_html",
    "pick_image_source",
    "build_inline_keyboard",
]