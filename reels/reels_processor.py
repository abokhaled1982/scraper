# reels/reels_processor.py
# Verarbeitet eine einzelne Deal-JSON-Datei für Reels

import asyncio
import json
import pathlib
import re
import sys
import requests
from .reels_service import render_reel, download_video

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

def download_image(url: str, product_id: str) -> str | None:
    if not url or str(url).strip().upper() == "N/A" or not url.startswith("http"):
        return None
    IMAGES_FOLDER.mkdir(parents=True, exist_ok=True)
    ext_match  = re.search(r"\.(png|jpg|jpeg|webp|gif)", url, re.IGNORECASE)
    ext        = ext_match.group(0) if ext_match else ".jpg"
    local_path = IMAGES_FOLDER / f"{product_id}{ext}"
    if local_path.exists():
        return str(local_path)  # Return local path? Wait, for API, need URL? No, the API takes URLs.
    # The API takes URLs, so we need to upload or use the original URL.
    # For now, return the original URL if download not needed.
    # But to be safe, return the url.
    return url

async def process_single_deal(full_path: pathlib.Path, sent_ids: set) -> bool:
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
        print(f"[PROCESS] 🚀 Guter Deal für Reels ({validation['discount']}%): {product_id}")
        
        images = data.get("images") or []
        image_urls = [img for img in images[:3] if img]  # Take up to 3 images
        
        discount_text = f"-{int(validation['discount'])}%" if validation['discount'] else "-0%"
        
        modifications = {
            "Call to Action.text": "See you at\nwww.mybrand.com"
        }
        
        for i, img_url in enumerate(image_urls, 1):
            modifications[f"Product Image {i}.source"] = img_url
            modifications[f"Product Offer {i}.text"] = discount_text
        
        # If less than 3, perhaps repeat or leave empty
        for i in range(len(image_urls) + 1, 4):
            if image_urls:
                modifications[f"Product Image {i}.source"] = image_urls[0]
                modifications[f"Product Offer {i}.text"] = discount_text
        
        render_result = await asyncio.get_event_loop().run_in_executor(None, render_reel, modifications)
        print(f"[DONE] ✅ Reel erfolgreich gerendert: {product_id}, URL: {render_result.get('url')}")
        
        # Video herunterladen
        local_video = await asyncio.get_event_loop().run_in_executor(None, download_video, render_result, product_id)
        if local_video:
            print(f"[VIDEO] ✅ Video heruntergeladen: {local_video}")
        else:
            print(f"[VIDEO] ❌ Video-Download fehlgeschlagen für {product_id}")

        # Sende an Facebook-Addon
        from facebook import fb_service
        sent = await fb_service.send_post(data, None, local_video)
        if sent:
            print(f"[FACEBOOK] ✅ Reel erfolgreich an Addon gesendet: {product_id}")
        else:
            print(f"[FACEBOOK] ❌ Reel konnte nicht an Addon gesendet werden: {product_id}")

        sent_ids.add(product_id)
        return True
    except Exception as e:
        print(f"[ERROR] Fehler bei {full_path.name}: {e}", file=sys.stderr)
        return False