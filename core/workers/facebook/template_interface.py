"""Template interface for Creatomate render requests.

This module maps deal JSON payloads to:
- Creatomate template selection
- template-specific modifications

Template metadata is loaded from JSON files in:
facebook/templates/*.json
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger("template_interface")


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


# ---------------------------------------------------------------------------
# Dynamische Kategorie-/Template-Auflösung (datengetrieben aus Registry)
# ---------------------------------------------------------------------------

def _normalize_category(value: Any) -> str:
    """Normalisiert einen Kategorie-String (lowercase, ohne Sonderzeichen)."""
    if not value:
        return ""
    s = str(value).strip().lower()
    # Umlaute & gängige Trennzeichen normalisieren
    repl = {"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss", "-": "", "_": "", " ": ""}
    for k, v in repl.items():
        s = s.replace(k, v)
    return s


def get_template_catalog() -> list[dict[str, Any]]:
    """Gibt eine kompakte Beschreibung aller registrierten Templates zurueck.

    Wird vom AI-Extraktor genutzt, um dem LLM zu zeigen, welche Templates
    verfuegbar sind und welche Produktkategorien sie jeweils abdecken.
    """
    catalog: list[dict[str, Any]] = []
    for template_type, cfg in TEMPLATE_REGISTRY.items():
        hints = cfg.get("ai_extractor_hints") or {}
        catalog.append(
            {
                "template_type": template_type,
                "description": str(cfg.get("description") or "").strip(),
                "categories": list(hints.get("recommended_categories") or []),
                "is_fallback": bool(hints.get("is_fallback_template")),
                "requires_transparent_product_image": bool(
                    hints.get("requires_transparent_product_image")
                ),
                "preferred_image_type": str(hints.get("preferred_image_type") or "").strip(),
            }
        )
    return catalog


def build_template_catalog_prompt_snippet() -> str:
    """Generiert ein Prompt-Snippet (Deutsch) mit allen verfuegbaren Templates.

    Wird in den AI-Extraktor-Prompt eingefuegt, damit das LLM weiss,
    welchen template_type es waehlen soll (basierend auf der Kategorie).
    """
    lines: list[str] = ["VERFUEGBARE VIDEO-TEMPLATES:"]
    for entry in get_template_catalog():
        cats = ", ".join(entry["categories"]) or "—"
        marker = " (FALLBACK)" if entry["is_fallback"] else ""
        img = entry["preferred_image_type"] or "beliebig"
        desc = entry["description"] or ""
        lines.append(
            f"- {entry['template_type']}{marker}: "
            f"Kategorien=[{cats}] | Bildtyp={img}. {desc}"
        )
    lines.append(
        "WAEHLE den template_type, dessen Kategorien-Liste am besten zur "
        "extrahierten produkt_kategorie passt. Wenn nichts klar passt: "
        "FALLBACK-Template verwenden."
    )
    return "\n".join(lines)


def select_template_type_by_category(category: str) -> str | None:
    """Mappt eine Produktkategorie auf einen template_type aus der Registry.

    Iteriert die Registry, vergleicht normalisierte Kategorie-Strings.
    Gibt None zurueck, wenn kein Match gefunden wird (Caller soll dann
    Fallback-Template waehlen).
    """
    target = _normalize_category(category)
    if not target:
        return None
    for template_type, cfg in TEMPLATE_REGISTRY.items():
        hints = cfg.get("ai_extractor_hints") or {}
        for cat in hints.get("recommended_categories") or []:
            if _normalize_category(cat) == target:
                return template_type
    # Zweite Runde: Teil-Match (z.B. "kinderkleidung" matcht "kleidung")
    for template_type, cfg in TEMPLATE_REGISTRY.items():
        hints = cfg.get("ai_extractor_hints") or {}
        for cat in hints.get("recommended_categories") or []:
            norm = _normalize_category(cat)
            if norm and (norm in target or target in norm):
                return template_type
    return None


def _find_fallback_template_type() -> str | None:
    """Sucht das als is_fallback_template markierte Template in der Registry."""
    for template_type, cfg in TEMPLATE_REGISTRY.items():
        hints = cfg.get("ai_extractor_hints") or {}
        if hints.get("is_fallback_template"):
            return template_type
    return None


def resolve_template_selection(
    deal_data: dict[str, Any],
    default_template_type: str = "offer_type1",
) -> tuple[str, str]:
    """Resolve logical template_type and final template_id for a deal.

    Priority:
    1) explicit template_id in deal JSON
    2) template_type in deal JSON -> lookup in TEMPLATE_REGISTRY
    3) produkt_kategorie / category in deal JSON -> Registry-Lookup
       (ai_extractor_hints.recommended_categories)
    4) is_fallback_template aus Registry
    5) default_template_type
    """
    explicit_template_id = str(deal_data.get("template_id") or "").strip()
    template_type = str(deal_data.get("template_type") or "").strip()

    if explicit_template_id:
        effective_type = template_type or "custom"
        return effective_type, explicit_template_id

    # NEU: Kategorie-basierte Auswahl, wenn kein template_type explizit gesetzt ist.
    if not template_type:
        category = (
            deal_data.get("produkt_kategorie")
            or deal_data.get("kategorie")
            or deal_data.get("category")
            or ""
        )
        matched = select_template_type_by_category(category)
        if matched:
            template_type = matched

    if not template_type:
        template_type = _find_fallback_template_type() or default_template_type

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

    if template_type == "typ3_audio":
        mods = _build_typ3_audio_modifications(deal_data, template_cfg)
    elif template_type == "typ5_sneaker_purple" or template_type.startswith("typ5"):
        mods = _build_typ5_modifications(deal_data, discount_value, template_cfg)
    elif template_type == "typ6_fashion" or template_type.startswith("typ6"):
        mods = _build_typ6_modifications(deal_data, discount_value, template_cfg)
    elif template_type.startswith("reel") or template_kind == "reel" or template_type == "custom":
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

    # Debug-Log: zeigt welche Voiceover-Quelle gewaehlt wurde
    vo_keys = [k for k in mods.keys() if "voiceover" in k.lower()]
    if vo_keys:
        for k in vo_keys:
            v = str(mods[k])
            log.info(f"[VOICEOVER] {template_type} -> {k}: {v[:120]}{'...' if len(v) > 120 else ''}")
    else:
        log.warning(f"[VOICEOVER] {template_type}: KEINE Voiceover-Modifikation gesetzt!")

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
        fallback=str(template_cfg.get("default_cta") or "Folgt uns fuer mehr Rabattaktionen!"),
    )
    website_text = _extract_website_text(deal_data, template_cfg)

    # VOICEOVER TEXT EXTRAHIEREN
    voiceover_text = _first_present_str(
        deal_data,
        ["voiceover", "voiceover_text", "tts_text", "reel_voiceover"],
        fallback="",
    )

    caption_text = _extract_caption_text(deal_data, rabatt_text)

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

    # VOICEOVER TEXT AN CREATOMATE ÜBERGEBEN
    if voiceover_text:
        modifications["Voiceover.text"] = voiceover_text

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


def _build_typ5_modifications(
    deal_data: dict[str, Any],
    discount_value: float,
    template_cfg: dict[str, Any],
) -> dict[str, Any]:
    """Modifications fuer typ5_sneaker_purple (Schuhe/Schmuck/Uhren).

    Layer-Keys laut Template: Background-Media.source, Product-Name.text,
    Subtitle.text, Discount-Badge.text, Price-Badge.text, Image-LNW.source,
    CTA.text, Website.text (CTA/Website liegen in der Outro-Composition).
    """
    images = deal_data.get("template_images") or deal_data.get("images") or []
    product_image_url = next(
        (img for img in images if isinstance(img, str) and img.strip()), ""
    ) or str(deal_data.get("image_url") or "").strip()

    background_url = str(
        deal_data.get("background_media")
        or deal_data.get("background_video")
        or deal_data.get("background_url")
        or ""
    ).strip()

    product_name, product_description = _extract_product_texts(deal_data)
    _, discounted_price = _extract_prices(deal_data)
    discount_text = _extract_discount_text(deal_data, discount_value)

    # Discount-Badge: "35%\nRABATT"
    discount_clean = discount_text.lstrip("-").strip() or "DEAL"
    discount_badge_text = f"{discount_clean}\nRABATT"

    # Price-Badge: "NUR\n€89"
    price_for_badge = discounted_price if discounted_price and discounted_price != "N/A" else ""
    price_badge_text = f"NUR\n{price_for_badge}" if price_for_badge else "TOP\nPREIS"

    cta_text = _first_present_str(
        deal_data,
        ["cta_text", "cta", "call_to_action"],
        fallback=str(template_cfg.get("default_cta") or "Folgt uns fuer mehr Rabattaktionen!"),
    )
    website_text = _extract_website_text(deal_data, template_cfg)
    voiceover_text = _extract_voiceover_text(deal_data)

    modifications: dict[str, Any] = {
        "Product-Name.text": product_name,
        "Subtitle.text": product_description,
        "Discount-Badge.text": discount_badge_text,
        "Price-Badge.text": price_badge_text,
        "CTA.text": cta_text,
        "Website.text": website_text,
    }

    if product_image_url:
        modifications["Image-LNW.source"] = product_image_url
        _apply_image_fit(modifications, "Image-LNW", template_cfg)

    if background_url:
        modifications["Background-Media.source"] = background_url

    if voiceover_text:
        # Audio-Layer 'Voiceover-9ST' im Template; ElevenLabs-TTS via 'source'.
        modifications["Voiceover-9ST.source"] = voiceover_text

    return modifications


def _build_typ6_modifications(
    deal_data: dict[str, Any],
    discount_value: float,
    template_cfg: dict[str, Any],
) -> dict[str, Any]:
    """Modifications fuer typ6_fashion (Mode/Kleidung, Lifestyle-Foto).

    Layer-Keys laut Template:
      - Background-Image.source : Lifestyle-/Modelfoto (images[0] oder image_url)
      - Logo.source             : DEALSBOSS-Logo (default_logo_source aus Registry,
                                  NICHT ueberschreiben sofern nichts explizit gesetzt)
      - Website.text            : affiliate_url / domain
      - Discount.text           : Rabatt in Prozent (z.B. '-30%')
      - Title.text              : reel_titel / title
      - Voiceover-C9N.source    : ElevenLabs TTS
    """
    images = deal_data.get("template_images") or deal_data.get("images") or []
    background_image_url = next(
        (img for img in images if isinstance(img, str) and img.strip()), ""
    ) or str(deal_data.get("image_url") or "").strip()

    product_name, _ = _extract_product_texts(deal_data)
    discount_text = _extract_discount_text(deal_data, discount_value)

    cta_text = _first_present_str(
        deal_data,
        ["cta_text", "cta", "call_to_action"],
        fallback=str(template_cfg.get("default_cta") or "Folgt uns fuer mehr Rabattaktionen!"),
    )
    website_text = _extract_website_text(deal_data, template_cfg)
    voiceover_text = _extract_voiceover_text(deal_data)

    # Logo: nur ueberschreiben, wenn explizit ein logo_url im Deal steht;
    # sonst kommt das Default-Logo direkt aus dem Template selbst.
    explicit_logo = str(
        deal_data.get("logo_url") or deal_data.get("brand_logo") or ""
    ).strip()

    modifications: dict[str, Any] = {
        "Title.text": product_name,
        "Discount.text": discount_text,
        "Website.text": website_text,
        "CTA.text": cta_text,
    }

    if background_image_url:
        modifications["Background-Image.source"] = background_image_url
        _apply_image_fit(modifications, "Background-Image", template_cfg)

    if explicit_logo:
        modifications["Logo.source"] = explicit_logo

    if voiceover_text:
        modifications["Voiceover-C9N.source"] = voiceover_text

    return modifications


def _build_typ3_audio_modifications(
    deal_data: dict[str, Any],
    template_cfg: dict[str, Any],
) -> dict[str, Any]:
    """Erstellt Modifications für das typ3_audio-Template (ElevenLabs-Voiceover).

    Nutzt den Layer-Key 'Voiceover-WZ7.text', der für dieses Template korrekt ist.
    """
    images = deal_data.get("template_images") or deal_data.get("images") or []
    product_image_url = next(
        (img for img in images if isinstance(img, str) and img.strip()), ""
    ) or str(deal_data.get("image_url") or "").strip()

    product_name, product_description = _extract_product_texts(deal_data)
    normal_price, discounted_price = _extract_prices(deal_data)
    discount_text = _extract_discount_text(deal_data, 0.0)
    discount_amount_text = _extract_discount_amount_text(deal_data, normal_price, discounted_price)
    rabatt_text = _extract_rabatt_text(deal_data, discount_text, discount_amount_text)
    caption_text = _extract_caption_text(deal_data, rabatt_text)

    cta_text = _first_present_str(
        deal_data,
        ["cta_text", "cta", "call_to_action"],
        fallback=str(template_cfg.get("default_cta") or "Folgt uns für mehr Rabatte!"),
    )
    website_text = _extract_website_text(deal_data, template_cfg)

    # Voiceover-Text (mit Fallback, falls AI nichts geliefert hat)
    voiceover_text = _extract_voiceover_text(deal_data)

    return {
        "Product-Image.source":     product_image_url,
        "Product-Name.text":        product_name,
        "Product-Description.text": product_description,
        "Normal-Price.text":        normal_price,
        "Discounted-Price.text":    discounted_price,
        "Caption.text":             caption_text,
        "CTA.text":                 cta_text,
        "Website.text":             website_text,
        # Korrekter Layer-Key für das typ3_audio-Template (ElevenLabs TTS via .source)
        "Voiceover-C9N.source":     voiceover_text,
    }


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
        fallback=str(template_cfg.get("default_cta") or "Folgt uns fuer mehr Rabattaktionen!"),
    )
    website_text = _extract_website_text(deal_data, template_cfg)

    # VOICEOVER TEXT EXTRAHIEREN
    voiceover_text = _first_present_str(
        deal_data,
        ["voiceover", "voiceover_text", "tts_text", "reel_voiceover"],
        fallback="",
    )

    caption_text = _extract_caption_text(deal_data, rabatt_text)

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

    # VOICEOVER TEXT AN CREATOMATE ÜBERGEBEN
    if voiceover_text:
        modifications["Voiceover.text"] = voiceover_text

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


def _extract_caption_text(payload: dict[str, Any], rabatt_text: str) -> str:
    """Caption-Text für das Reel-Display.

    Priorität:
    1. reel_caption – vom LLM fertig formatiert (mit Emojis, Coupon, Zeilenumbrüchen)
    2. Einfacher Fallback aus rabatt_text + optionalem Couponcode
    """
    existing = _first_present_str(payload, ["reel_caption"], fallback="N/A")
    if existing != "N/A":
        return existing

    # Fallback: rabatt_text als Basis
    caption = rabatt_text if rabatt_text and rabatt_text not in ("Top Angebot", "") else "🔥 Deal Alert"

    # Gutscheincode anhängen
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


def _extract_voiceover_text(deal_data: dict[str, Any]) -> str:
    """Liefert den TTS-Text fuer ElevenLabs.

    Reihenfolge: AI-Felder -> Fallback aus Titel + Rabatt/Preis.
    Liefert garantiert einen nicht-leeren String, damit Creatomate nicht den
    Template-Default ('This text is read aloud...') verwendet.
    """
    voiceover_text = _first_present_str(
        deal_data,
        ["voiceover", "voiceover_text", "tts_text", "reel_voiceover"],
        fallback="",
    )
    if voiceover_text:
        return voiceover_text

    # Fallback aus vorhandenen Deal-Daten konstruieren
    title = str(deal_data.get("title") or deal_data.get("product_name") or "Dieses Produkt").strip()
    discount_raw = str(deal_data.get("discount_percent") or "").replace("N/A", "").strip()
    _, discounted_price = _extract_prices(deal_data)
    price_part = discounted_price if discounted_price and discounted_price != "N/A" else ""

    if discount_raw and price_part:
        return (
            f"Krasses Angebot heute! {title} jetzt {discount_raw} guenstiger – "
            f"nur {price_part}. Jetzt schnell zuschlagen, Link in der Bio!"
        )
    if price_part:
        return f"Schnell sein lohnt sich! {title} jetzt fuer nur {price_part}. Link in der Bio!"
    return f"Schnell sein lohnt sich! {title} jetzt im Angebot. Link in der Bio!"


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


def _extract_website_text(
    deal_data: dict[str, Any],
    template_cfg: dict[str, Any],
) -> str:
    """Einheitliche Website-Aufloesung fuer alle Template-Builder.

    Reihenfolge: explizit gesetzt -> affiliate_url (AI-Output-Feld) -> Default.
    """
    return _first_present_str(
        deal_data,
        ["website_text", "website", "domain", "affiliate_url", "url"],
        fallback=str(template_cfg.get("default_website") or "www.dealsboss.de"),
    )