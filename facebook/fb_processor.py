# facebook/fb_processor.py
# Verarbeitet eine einzelne Deal-JSON-Datei

import json
import pathlib
import re
import sys
import requests

HERE          = pathlib.Path(__file__).resolve().parent
IMAGES_FOLDER = HERE / "images"

DOWNLOAD_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36"
}


def validate_deal_data(data: dict) -> dict:
    if not data.get("title") or not data.get("affiliate_url"):
        return {"valid": False, "reason": "Daten unvollständig (Titel/URL fehlt)", "discount": 0}
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
            print(f"[FILTER] 🗑️ {full_path.name}: {validation['reason']}. Lösche Datei.")
            full_path.unlink(missing_ok=True)
            return False
        print(f"[PROCESS] 🚀 Guter Deal ({validation['discount']}%): {product_id}")
        images    = data.get("images") or []
        image_url = data.get("image_url") or (images[0] if images else None)
        local_img = download_image(image_url, product_id)
        await fb_service.send_post(data, local_img)
        sent_ids.add(product_id)
        print(f"[DONE] ✅ Deal erfolgreich verarbeitet: {product_id}")
        return True
    except Exception as e:
        print(f"[ERROR] Fehler bei {full_path.name}: {e}", file=sys.stderr)
        return False
