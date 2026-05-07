# facebook/fb_message.py
# Text-Generator für Facebook-Posts
# Portierung von whatsapp/src/facebook_message.js


def _strip_stars(text: str) -> str:
    if not text:
        return ""
    return str(text).replace("*", "").strip()


def _clean(v) -> str:
    if v is None or str(v).strip() in ("N/A", "null", ""):
        return ""
    return str(v).strip()


def create_facebook_message(data: dict) -> str:
    """Erzeugt den Facebook-Post-Text aus einem Deal-Dict."""
    title     = _strip_stars(data.get("title") or data.get("name") or "Super Angebot")
    price_raw = data.get("price") or {}
    price     = _clean(price_raw.get("raw") if isinstance(price_raw, dict) else price_raw)
    old_price = _clean(
        (data.get("original_price") or {}).get("raw")
        if isinstance(data.get("original_price"), dict)
        else data.get("original_price")
    )
    discount  = _clean(data.get("discount_percent") or data.get("discount"))

    coupon_raw = data.get("coupon") or {}
    coupon     = _clean(
        coupon_raw.get("code") if isinstance(coupon_raw, dict) else data.get("coupon_code")
    )
    extra_text = _strip_stars(data.get("rabatt_text") or data.get("feature_text") or "")
    raw_tags   = data.get("hashtags") or []
    hashtags   = " ".join(_clean(t) for t in raw_tags if _clean(t))

    # Zeile 1: Titel
    msg = f"{title}\n"

    # Zeile 2: Preisübersicht
    price_parts = []
    if price:
        price_parts.append(f"💶 Nur {price}")
    if old_price and old_price != price:
        price_parts.append(f"(statt {old_price})")
    if discount and discount != "N/A":
        price_parts.append(f"| {discount} Rabatt 📉")
    if price_parts:
        msg += " ".join(price_parts) + "\n"

    # Zeile 3: Details
    details = []
    if coupon and coupon != "N/A":
        details.append(f"Code an der Kasse: {coupon}")
    if extra_text and len(extra_text) > 5 and extra_text != "N/A":
        details.append(extra_text)
    if details:
        msg += "\n".join(details) + "\n"

    # Zeile 4: Kommentar-Hinweis
    msg += "👇 Link zum Deal in den Kommentaren 👇\n"

    # Hashtags
    if hashtags:
        msg += f"\n{hashtags}"
    else:
        msg += "\n#Angebot #Schnäppchen #Deal #Rabatt"

    return msg.strip()
