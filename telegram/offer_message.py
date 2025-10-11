# telegram/offer_message.py
from html import escape
from typing import Optional, Dict, Any
from pathlib import Path
from os.path import isabs, exists
import os, re

_URL_RE = re.compile(r"^https?://", re.I)

def _is_url(s: Optional[str]) -> bool:
    return bool(s and _URL_RE.match(s))

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
    full = int(val); half = 1 if val - full >= 0.5 else 0
    bar = "⭐"*full + ("✩" if half else "") + "☆"*(5-full-half)
    cnt_txt = f" • {cnt:,}".replace(",", " ") if cnt else ""
    return f"{bar} <i>{val:.1f}/5</i>{cnt_txt}"

def _price_line(d: Dict[str, Any]) -> str:
    p   = (d.get("price") or {}).get("raw")
    o   = (d.get("original_price") or {}).get("raw")
    pct = d.get("discount_percent")
    # Preis fett, UVP gestrichen, Rabatt kursiv – kurz & knackig
    s = f"💶 <b>{escape(p)}</b>" if p else "💶 n/a"
    if o:   s += f"  <s>{escape(o)}</s>"
    if pct: s += f"  <i>(−{int(round(float(pct)))}%)</i>"
    return s

def _affiliate(d: Dict[str, Any], fallback_url: str) -> str:
    return d.get("affiliate_url") or fallback_url or "https://amzn.to/42vWlQM"

def build_caption_html(d: Dict[str, Any], affiliate_fallback: str) -> str:
    """
    Landing-Page-artige, aber kurze Caption (HTML):
    [Badge optional]
    [Titel]
    ━ thin divider ━
    [Preis/Rabatt]
    [Sterne + #Bewertungen]
    [Verfügbarkeit]
    [klarer Link]
    """
    title = escape((d.get("title") or "").strip())
    if len(title) > 90:
        title = title[:87] + "…"

    badge  = _badge(d)
    price  = _price_line(d)
    rating = _stars(d.get("rating_value"), d.get("review_count"))
    avail  = d.get("availability")
    url    = _affiliate(d, affiliate_fallback)

    # Zarte Abstände & Divider (keine CSS, nur Unicode/ZWSP)
    ZWSP = "‎"                      # hauchdünner Spacer
    DIV  = "━" * 18                 # dünner Trenner

    parts = []
    if badge:
        parts.append(badge)
    if title:
        parts.append(f"🛍️ <b>{title}</b>")
    parts.append(DIV)
    parts.append(price)
    parts.append(f"⭐ {rating}")
    if avail:
        parts.append(f"✅ <b>{escape(avail)}</b>")
    parts.append(ZWSP)
    parts.append(f"🔗 <a href=\"{escape(url)}\">Zum Angebot</a>")

    return "\n".join(parts)[:1024]  # Telegram Caption-Limit

def pick_image_source(d: Dict[str, Any], base_dir: Path) -> Optional[str]:
    """
    Gibt HTTPS-URL ODER lokalen absoluten Pfad zurück.
    Fällt auf assets/placeholder_square.jpg zurück (falls vorhanden).
    """
    images = d.get("images") or []
    img0 = images[0] if images else None
    if _is_url(img0):
        return img0
    if img0:
        if not isabs(img0):
            img0 = str((base_dir / img0).resolve())
        return img0 if exists(img0) else None
    ph = (base_dir / "assets" / "placeholder_square.jpg")
    return str(ph) if ph.exists() else None
