# instagram/ig_message.py
# Caption-Generator für Instagram Posts / Reels


def _clean(v) -> str:
    if v is None or str(v).strip() in ("N/A", "null", ""):
        return ""
    return str(v).strip()


def _strip_stars(text: str) -> str:
    return str(text or "").replace("*", "").strip()


def create_ig_caption(data: dict) -> str:
    """Erzeugt die Instagram-Caption aus einem Deal-Dict."""

    title     = _strip_stars(data.get("title") or data.get("name") or "Super Angebot")
    price_raw = data.get("price") or {}
    price     = _clean(price_raw.get("raw") if isinstance(price_raw, dict) else price_raw)
    old_price = _clean(
        (data.get("original_price") or {}).get("raw")
        if isinstance(data.get("original_price"), dict)
        else data.get("original_price")
    )
    discount  = _clean(data.get("discount_percent") or data.get("discount"))
    offer_url = _clean(data.get("affiliate_url") or data.get("url") or "")
    if offer_url in ("N/A", "null"):
        offer_url = ""

    coupon_raw = data.get("coupon") or {}
    coupon     = _clean(
        coupon_raw.get("code") if isinstance(coupon_raw, dict) else data.get("coupon_code")
    )
    extra_text = _strip_stars(data.get("rabatt_text") or data.get("feature_text") or "")
    raw_tags   = data.get("hashtags") or []
    hashtags   = " ".join(_clean(t) for t in raw_tags if _clean(t))
    if not hashtags:
        hashtags = "#Angebot #Schnäppchen #Deal #Rabatt #Amazon #AmazonDeals"

    lines = [title, ""]

    # Preis
    price_parts = []
    if price:
        price_parts.append(f"💶 Nur {price}")
    if old_price and old_price != price:
        price_parts.append(f"(statt {old_price})")
    if discount and discount != "N/A":
        price_parts.append(f"| -{discount} Rabatt 📉")
    if price_parts:
        lines.append(" ".join(price_parts))

    # Gutschein
    if coupon:
        lines.append(f"🏷️ Gutscheincode: {coupon}")

    # Extra-Text
    if extra_text:
        lines.append(f"\n{extra_text}")

    # Link (Instagram erlaubt keine klickbaren Links im Post — Hinweis auf Bio)
    if offer_url:
        lines.append(f"\n🔗 Link in Bio / Kommentaren")

    lines += ["", hashtags]
    return "\n".join(lines)
