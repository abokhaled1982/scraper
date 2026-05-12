"""Template interface for Creatomate render requests.

This module maps deal JSON payloads to:
- Creatomate template selection
- template-specific modifications

Template metadata is loaded from JSON files in:
facebook/templates/*.json
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


def _load_template_registry() -> dict[str, dict[str, Any]]:
    registry: dict[str, dict[str, Any]] = {}
    if not TEMPLATES_DIR.exists():
        return registry

    for fp in sorted(TEMPLATES_DIR.glob("*.json")):
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ValueError(f"Invalid template file '{fp.name}': {exc}") from exc

        template_type = str(data.get("template_type") or fp.stem).strip()
        if not template_type:
            raise ValueError(f"template_type missing in '{fp.name}'")

        template_id = str(data.get("template_id") or "").strip()
        if not template_id:
            raise ValueError(f"template_id missing in '{fp.name}'")

        registry[template_type] = data

    return registry


TEMPLATE_REGISTRY: dict[str, dict[str, Any]] = _load_template_registry()


def resolve_template_selection(
    deal_data: dict[str, Any],
    default_template_type: str = "offer_type1",
) -> tuple[str, str]:
    """Resolve logical template_type and final template_id for a deal.

    Priority:
    1) explicit template_id in deal JSON
    2) template_type in deal JSON -> lookup in TEMPLATE_REGISTRY
    3) default_template_type
    """
    explicit_template_id = str(deal_data.get("template_id") or "").strip()
    template_type = str(deal_data.get("template_type") or "").strip()

    if explicit_template_id:
        effective_type = template_type or "custom"
        return effective_type, explicit_template_id

    if not template_type:
        template_type = default_template_type

    template_cfg = TEMPLATE_REGISTRY.get(template_type)
    if not template_cfg:
        available = ", ".join(sorted(TEMPLATE_REGISTRY.keys()))
        raise ValueError(
            f"Unknown template_type '{template_type}'. Available: {available}"
        )

    template_id = str(template_cfg.get("template_id") or "").strip()
    if not template_id:
        raise ValueError(f"template_id missing in registry for '{template_type}'")

    return template_type, template_id


def build_modifications_for_template(
    deal_data: dict[str, Any],
    template_type: str,
    discount_value: float = 0.0,
) -> dict[str, Any]:
    """Build Creatomate modifications for the selected template type."""
    template_cfg = TEMPLATE_REGISTRY.get(template_type, {})
    template_kind = str(template_cfg.get("kind") or "").strip().lower()

    if template_type.startswith("reel") or template_kind == "reel" or template_type == "custom":
        mods = _build_reel_type_modifications(deal_data, discount_value, template_cfg)
    elif template_type.startswith("offer") or template_kind == "offer":
        mods = _build_offer_type_modifications(deal_data, discount_value, template_cfg)
    else:
        # Safe fallback for unknown naming conventions.
        mods = _build_reel_type_modifications(deal_data, discount_value, template_cfg)

    # Optional overrides from deal JSON.
    # Example:
    # "template_modifications": {"CTA.text": "Follow us"}
    custom_overrides = deal_data.get("template_modifications")
    if isinstance(custom_overrides, dict):
        mods.update(custom_overrides)

    return mods


def _build_reel_type_modifications(
    deal_data: dict[str, Any],
    discount_value: float,
    template_cfg: dict[str, Any],
) -> dict[str, Any]:
    # User can explicitly control images via template_images.
    # Fallback to extracted product images in `images`.
    images = deal_data.get("template_images") or deal_data.get("images") or []
    image_urls = [img for img in images if isinstance(img, str) and img.strip()][:3]
    if not image_urls:
        image_url = str(deal_data.get("image_url") or "").strip()
        if image_url:
            image_urls = [image_url]

    product_name, product_description = _extract_product_texts(deal_data)
    normal_price, discounted_price = _extract_prices(deal_data)
    discount_text = _extract_discount_text(deal_data, discount_value)
    discount_amount_text = _extract_discount_amount_text(
        deal_data,
        normal_price,
        discounted_price,
    )
    rabatt_text = _extract_rabatt_text(deal_data, discount_text, discount_amount_text)
    cta_text = _first_present_str(
        deal_data,
        ["cta_text", "cta", "call_to_action"],
        fallback="Folgt uns fuer mehr Rabattaktionen!",
    )
    website_text = _first_present_str(
        deal_data,
        ["website_text", "website", "domain"],
        fallback="www.deealsboss.de",
    )

    caption_text = _extract_caption_text(deal_data, discount_text)

    modifications: dict[str, Any] = {
        "Call to Action.text": cta_text,
        "CTA.text": cta_text,
        "Website.text": website_text,
        "Product-Name.text": product_name,
        "Product-Description.text": product_description,
        "Caption.text": caption_text,
        "Normal-Price.text": normal_price,
        "Discounted-Price.text": discounted_price,
        "Discount.text": discount_text,
        "Discount-Percent.text": discount_text,
        "Discount-Amount.text": discount_amount_text,
        "Rabatt-Text.text": rabatt_text,
        "Rabatt.text": rabatt_text,
    }

    # Expected layer naming in the current reel template:
    # Product Image 1..3, Product Offer 1..3
    for i, img_url in enumerate(image_urls, 1):
        modifications[f"Product Image {i}.source"] = img_url
        modifications[f"Product Offer {i}.text"] = discount_text
        _apply_image_fit(modifications, f"Product Image {i}", template_cfg)

    # Keep all slots populated if fewer than 3 images are available.
    if image_urls:
        for i in range(len(image_urls) + 1, 4):
            modifications[f"Product Image {i}.source"] = image_urls[0]
            modifications[f"Product Offer {i}.text"] = discount_text
            _apply_image_fit(modifications, f"Product Image {i}", template_cfg)

    return modifications


def _build_offer_type_modifications(
    deal_data: dict[str, Any],
    discount_value: float,
    template_cfg: dict[str, Any],
) -> dict[str, Any]:
    images = deal_data.get("template_images") or deal_data.get("images") or []
    first_image = ""
    for img in images:
        if isinstance(img, str) and img.strip():
            first_image = img
            break
    if not first_image:
        first_image = str(deal_data.get("image_url") or "").strip()

    product_name, product_description = _extract_product_texts(deal_data)
    normal_price, discounted_price = _extract_prices(deal_data)
    discount_text = _extract_discount_text(deal_data, discount_value)
    discount_amount_text = _extract_discount_amount_text(
        deal_data,
        normal_price,
        discounted_price,
    )
    rabatt_text = _extract_rabatt_text(deal_data, discount_text, discount_amount_text)
    cta_text = _first_present_str(
        deal_data,
        ["cta_text", "cta", "call_to_action"],
        fallback="Folgt uns fuer mehr Rabattaktionen!",
    )
    website_text = _first_present_str(
        deal_data,
        ["website_text", "website", "domain"],
        fallback="www.deealsboss.de",
    )

    caption_text = _extract_caption_text(deal_data, discount_text)

    modifications = {
        "Product-Image.source": first_image,
        "Product-Name.text": product_name,
        "Product-Description.text": product_description,
        "Normal-Price.text": normal_price,
        "Discounted-Price.text": discounted_price,
        "Discount.text": discount_text,
        "Discount-Percent.text": discount_text,
        "Discount-Amount.text": discount_amount_text,
        "Rabatt-Text.text": rabatt_text,
        "Rabatt.text": rabatt_text,
        "Caption.text": caption_text,
        "CTA.text": cta_text,
        "Website.text": website_text,
    }
    _apply_image_fit(modifications, "Product-Image", template_cfg)
    return modifications


def _truncate_for_reel(text: str, max_chars: int = 22) -> str:
    """Kürzt Text an Wortgrenze für das Reel-Display."""
    if len(text) <= max_chars:
        return text
    truncated = text[:max_chars]
    last_space = truncated.rfind(" ")
    if last_space > 4:
        truncated = truncated[:last_space]
    return truncated.rstrip(",.;:") + "..."


def _extract_caption_text(payload: dict[str, Any], discount_text: str) -> str:
    """Baut den Caption-Text für das Reel-Template."""
    existing = _first_present_str(payload, ["reel_caption"], fallback="N/A")
    if existing != "N/A":
        return existing

    # Fallback: aus Rabatt-Info aufbauen
    if discount_text and discount_text not in ("-0%", "N/A", ""):
        caption = f"Sale Alert {discount_text} 🔥"
    else:
        caption = "Discount Alert 🔥"

    # Gutscheincode aus verschachteltem coupon-Dict oder flachem Feld lesen
    coupon_raw = payload.get("coupon") or {}
    coupon_code = ""
    if isinstance(coupon_raw, dict):
        coupon_code = str(coupon_raw.get("code") or "").strip()
    if not coupon_code:
        coupon_code = str(
            payload.get("coupon_code") or payload.get("gutschein_code") or ""
        ).strip()
    if coupon_code and coupon_code.lower() not in ("n/a", "null", "none", ""):
        caption += f"\nCode: {coupon_code}"

    return caption


def _extract_product_texts(payload: dict[str, Any]) -> tuple[str, str]:
    # LLM-generierter Kurztitel hat Priorität, sonst Fallback mit Kürzung
    product_name = _first_present_str(
        payload,
        ["reel_titel", "title", "product_name", "name"],
        fallback="Top Deal",
    )
    if not str(payload.get("reel_titel") or "").strip():
        product_name = _truncate_for_reel(product_name, max_chars=22)

    # LLM-generierte Kurzbeschreibung hat Priorität, sonst auf 4 Wörter kürzen
    product_description = _first_present_str(
        payload,
        ["reel_beschreibung", "description", "subtitle", "feature_text", "rabatt_text"],
        fallback="Starkes Angebot nur kurze Zeit.",
    )
    if not str(payload.get("reel_beschreibung") or "").strip():
        words = product_description.split()
        if len(words) > 4:
            product_description = " ".join(words[:4]) + " ..."

    return product_name, product_description


def _extract_prices(payload: dict[str, Any]) -> tuple[str, str]:
    normal_price = _first_present_str(
        payload,
        ["normal_price", "old_price", "list_price", "original_price"],
        fallback="N/A",
    )
    discounted_price = _first_present_str(
        payload,
        ["discounted_price", "price", "deal_price", "current_price"],
        fallback="N/A",
    )
    return normal_price, discounted_price


def _extract_discount_amount_text(
    payload: dict[str, Any],
    normal_price_text: str,
    discounted_price_text: str,
) -> str:
    # Prefer explicit value from payload.
    existing = _first_present_str(
        payload,
        ["discount_amount"],
        fallback="N/A",
    )
    if existing != "N/A":
        return existing

    normal_value = _parse_price_to_float(normal_price_text)
    discounted_value = _parse_price_to_float(discounted_price_text)
    if normal_value is None or discounted_value is None:
        return "N/A"

    diff = normal_value - discounted_value
    if diff <= 0:
        return "N/A"
    return _format_eur(diff)


def _extract_discount_text(payload: dict[str, Any], discount_value: float) -> str:
    raw = str(payload.get("discount_percent") or "").strip()
    if raw and raw.upper() != "N/A":
        cleaned = raw.replace(" ", "")
        if "%" in cleaned:
            return cleaned if cleaned.startswith("-") else f"-{cleaned.lstrip('+')}"
    if discount_value:
        return f"-{int(discount_value)}%"
    # Try deriving percent from prices.
    normal_price = _first_present_str(
        payload,
        ["normal_price", "old_price", "list_price", "original_price"],
        fallback="N/A",
    )
    discounted_price = _first_present_str(
        payload,
        ["discounted_price", "price", "deal_price", "current_price"],
        fallback="N/A",
    )
    normal_value = _parse_price_to_float(normal_price)
    discounted_value = _parse_price_to_float(discounted_price)
    if normal_value and discounted_value and normal_value > discounted_value:
        pct = round((normal_value - discounted_value) / normal_value * 100)
        return f"-{pct}%"
    return "-0%"


def _extract_rabatt_text(
    payload: dict[str, Any],
    discount_percent_text: str,
    discount_amount_text: str,
) -> str:
    existing = _first_present_str(
        payload,
        ["rabatt_text"],
        fallback="N/A",
    )
    if existing != "N/A":
        return existing

    if discount_amount_text != "N/A" and discount_percent_text not in ("", "-0%"):
        return f"Spare {discount_amount_text} ({discount_percent_text})"
    if discount_amount_text != "N/A":
        return f"Spare {discount_amount_text}"
    if discount_percent_text not in ("", "-0%"):
        return f"Rabatt {discount_percent_text}"
    return "Top Angebot"


def _apply_image_fit(
    modifications: dict[str, Any],
    layer_name: str,
    template_cfg: dict[str, Any],
) -> None:
    image_fit = str(template_cfg.get("image_fit") or "").strip().lower()
    if image_fit:
        modifications[f"{layer_name}.fit"] = image_fit


def _parse_price_to_float(text: str) -> float | None:
    value = str(text or "").strip()
    if not value or value.upper() in {"N/A", "NONE", "NULL"}:
        return None
    cleaned = (
        value.replace("€", "")
        .replace("EUR", "")
        .replace(" ", "")
        .replace(".", "")
        .replace(",", ".")
    )
    try:
        return float(cleaned)
    except ValueError:
        return None


def _format_eur(value: float) -> str:
    return f"{value:.2f}".replace(".", ",") + " €"


def _first_present_str(
    payload: dict[str, Any],
    keys: list[str],
    fallback: str,
) -> str:
    for key in keys:
        if key not in payload:
            continue
        value = payload.get(key)
        if value is None:
            continue
        if isinstance(value, dict):
            raw = value.get("raw")
            val = value.get("value")
            if raw is not None and str(raw).strip():
                return str(raw).strip()
            if val is not None and str(val).strip():
                return str(val).strip()
            continue
        text = str(value).strip()
        if text and text.upper() not in {"N/A", "NONE", "NULL"}:
            return text
    return fallback
