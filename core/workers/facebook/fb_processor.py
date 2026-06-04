# facebook/fb_processor.py
# Verarbeitet eine einzelne Deal-JSON-Datei

import json
import pathlib
import re
import sys
import requests

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from core.logging import get_logger  # noqa: E402
log = get_logger("fb_processor")  # noqa: E402
from core.paths import IMAGES_DIR
from core.db import deals_repo

HERE          = pathlib.Path(__file__).resolve().parent
IMAGES_FOLDER = IMAGES_DIR

DOWNLOAD_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36"
}


def _is_empty(value) -> bool:
    """Gibt True zurück wenn der Wert leer, 'N/A', 'null', '0', '0.00' o.ä. ist."""
    if value is None:
        return True
    s = str(value).strip()
    # Case-insensitive Vergleich, damit auch 'None' (str(None)) abgefangen wird
    return s.lower() in ("", "n/a", "null", "none", "0", "0.00", "0.00 €", "0 €")


def validate_deal_data(data: dict) -> dict:
    # Reel-Dateien werden vom Reels-Watcher behandelt, nicht hier
    if data.get("type") == "reel":
        return {"valid": False, "reason": "Typ ist 'reel' – wird vom Reels-Watcher verarbeitet", "discount": 0}

    # Titel-Check
    title = str(data.get("title") or "").strip()
    if not title or title.upper() == "N/A":
        return {"valid": False, "reason": "Kein gültiger Titel vorhanden", "discount": 0}

    # URL-Check
    if not data.get("affiliate_url"):
        return {"valid": False, "reason": "Affiliate-URL fehlt", "discount": 0}

    # Preis-Check: 0.00, N/A oder fehlend → ungültig
    price_raw = data.get("price") or {}
    price_str = str(price_raw.get("raw") if isinstance(price_raw, dict) else price_raw).strip()
    if _is_empty(price_str):
        return {"valid": False, "reason": f"Kein gültiger Preis vorhanden ('{price_str}')", "discount": 0}

    # Bild-Check: mindestens eine verwertbare Bild-URL
    images     = data.get("images") or []
    image_url  = data.get("image_url") or (images[0] if images else None)
    if not image_url or not str(image_url).startswith("http"):
        return {"valid": False, "reason": "Kein Bild vorhanden", "discount": 0}

    discount_value = 0.0
    raw_discount   = str(data.get("discount_percent") or "").strip()
    if raw_discount and raw_discount != "N/A":
        cleaned = raw_discount.replace("-", "").replace("%", "").replace(",", ".")
        try:
            discount_value = float(cleaned)
        except ValueError:
            pass
    return {"valid": True, "reason": "OK", "discount": discount_value}


def download_image(url: str, product_id: str) -> pathlib.Path | None:
    if not url or str(url).strip().upper() == "N/A" or not url.startswith("http"):
        return None
    IMAGES_FOLDER.mkdir(parents=True, exist_ok=True)
    ext_match  = re.search(r"\.(png|jpg|jpeg|webp|gif)", url, re.IGNORECASE)
    ext        = ext_match.group(0) if ext_match else ".jpg"
    local_path = IMAGES_FOLDER / f"{product_id}{ext}"
    if local_path.exists():
        return local_path
    try:
        resp = requests.get(url, headers=DOWNLOAD_HEADERS, timeout=15, stream=True)
        resp.raise_for_status()
        with open(local_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=65536):
                f.write(chunk)
        return local_path
    except Exception as e:
        log.info(f"[IMG] Download fehlgeschlagen für {product_id}: {e}")
        return None


async def process_single_deal(deal: dict, sent_ids: set, fb_service) -> bool:
    product_id = deal.get("product_id")
    deal_id = deal.get("id")
    data = deal.get("payload") or {}
    if not product_id or product_id in sent_ids:
        return False
    try:
        validation = validate_deal_data(data)
        if not validation["valid"]:
            log.error(f"[FILTER] 🗑️ {product_id}: {validation['reason']}. Mark failed in DB.")
            if deal_id:
                deals_repo.mark_failed(deal_id, validation["reason"])
            return False
        log.info(f"[PROCESS] 🚀 Guter Deal ({validation['discount']}%): {product_id}")
        images    = data.get("images") or []
        image_url = data.get("image_url") or (images[0] if images else None)
        local_img = download_image(image_url, product_id)
        success = await fb_service.send_post(data, local_img)
        if not success:
            log.error(f"[WARN] ⚠️ send_post hat Fehler/Timeout zurückgemeldet für {product_id}. Deal bleibt in Queue.")
            return False
        sent_ids.add(product_id)
        if deal_id:
            deals_repo.add_event(deal_id, "posted", "facebook")
            deals_repo.mark_sent(deal_id, detail="facebook")
        log.info(f"[DONE] ✅ Deal gepostet und in DB als 'sent' markiert: {product_id}")
        return True
    except Exception as e:
        log.error(f"[ERROR] Fehler bei {product_id}: {e}")
        if deal_id:
            try:
                deals_repo.mark_failed(deal_id, str(e))
            except Exception:
                pass
        return False