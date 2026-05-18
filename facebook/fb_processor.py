# facebook/fb_processor.py
# Verarbeitet eine einzelne Deal-JSON-Datei

import json
import pathlib
import re
import sys
import requests

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from config import IMAGES_DIR, DEALS_SENT_DIR, DEALS_FAILED_DIR

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
    return s in ("", "N/A", "null", "none", "0", "0.00", "0.00 €", "0 €")


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
        print(f"[IMG] Download fehlgeschlagen für {product_id}: {e}")
        return None


async def process_single_deal(full_path: pathlib.Path, sent_ids: set, fb_service) -> bool:
    product_id = full_path.stem
    if product_id in sent_ids:
        return False
    try:
        data       = json.loads(full_path.read_text(encoding="utf-8"))
        validation = validate_deal_data(data)
        if not validation["valid"]:
            print(f"[FILTER] 🗑️ {full_path.name}: {validation['reason']}. Verschiebe nach failed.")
            DEALS_FAILED_DIR.mkdir(parents=True, exist_ok=True)
            full_path.rename(DEALS_FAILED_DIR / full_path.name)
            return False
        print(f"[PROCESS] 🚀 Guter Deal ({validation['discount']}%): {product_id}")
        images    = data.get("images") or []
        image_url = data.get("image_url") or (images[0] if images else None)
        local_img = download_image(image_url, product_id)
        success = await fb_service.send_post(data, local_img)
        if not success:
            print(f"[WARN] ⚠️ send_post hat Fehler/Timeout zurückgemeldet für {product_id}. Datei bleibt in queue/.")
            return False
        sent_ids.add(product_id)
        # Deal-JSON nach sent/ verschieben — NUR wenn wirklich erfolgreich gepostet
        dest = DEALS_SENT_DIR / full_path.name
        full_path.rename(dest)
        print(f"[DONE] ✅ Deal gepostet und nach sent/ verschoben: {product_id}")
        return True
    except Exception as e:
        print(f"[ERROR] Fehler bei {full_path.name}: {e}", file=sys.stderr)
        return False