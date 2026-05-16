# instagram/ig_processor.py
# Verarbeitet eine einzelne Deal-JSON-Datei für Instagram

import json
import pathlib
import re
import sys

import requests

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from config import IMAGES_DIR, DEALS_SENT_DIR

IMAGES_FOLDER = IMAGES_DIR

DOWNLOAD_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36"
}

# Mindest-Rabatt für Instagram-Post
MIN_DISCOUNT_PERCENT = 10.0


def validate_deal_data(data: dict) -> dict:
    if not data.get("title"):
        return {"valid": False, "reason": "Kein Titel", "discount": 0}
    discount_value = 0.0
    raw_discount   = str(data.get("discount_percent") or "").strip()
    if raw_discount and raw_discount not in ("N/A", ""):
        cleaned = raw_discount.replace("-", "").replace("%", "").replace(",", ".")
        try:
            discount_value = float(cleaned)
        except ValueError:
            pass
    if discount_value < MIN_DISCOUNT_PERCENT:
        return {
            "valid": False,
            "reason": f"Rabatt zu gering ({discount_value:.0f}% < {MIN_DISCOUNT_PERCENT}%)",
            "discount": discount_value,
        }
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
        print(f"[IG-IMG] Download fehlgeschlagen für {product_id}: {e}")
        return None


def ensure_jpeg(path: pathlib.Path) -> pathlib.Path:
    """Konvertiert ein Bild zu JPEG falls nötig (Instagram bevorzugt JPEG)."""
    if path.suffix.lower() in (".jpg", ".jpeg"):
        return path
    try:
        from PIL import Image
        jpg_path = path.with_suffix(".jpg")
        img = Image.open(path).convert("RGB")
        img.save(jpg_path, "JPEG", quality=92)
        return jpg_path
    except Exception as e:
        print(f"[IG-IMG] Konvertierung fehlgeschlagen: {e} — nutze Original")
        return path


async def process_single_deal(full_path: pathlib.Path, sent_ids: set) -> bool:
    """Verarbeitet eine Deal-JSON und postet auf Instagram."""
    import instagram.ig_service as ig_service
    from instagram.ig_message import create_ig_caption

    product_id = full_path.stem
    if product_id in sent_ids:
        return False

    try:
        data       = json.loads(full_path.read_text(encoding="utf-8"))
        validation = validate_deal_data(data)
        if not validation["valid"]:
            print(f"[IG-FILTER] ⏭️ {full_path.name}: {validation['reason']}")
            return False

        print(f"[IG-PROCESS] 📸 Deal ({validation['discount']:.0f}%): {product_id}")

        caption = create_ig_caption(data)
        offer_url = str(data.get("affiliate_url") or data.get("url") or "").strip()
        if offer_url in ("N/A", "null", ""):
            offer_url = ""

        deal_type  = str(data.get("type") or "").strip().lower()

        # Nur Reels auf Instagram posten – normale Posts werden übersprungen
        if deal_type != "reel":
            print(f"[IG-SKIP] Kein Reel (type={deal_type!r}) – Instagram überspringt {product_id}.")
            full_path.unlink(missing_ok=True)
            return False

        images     = data.get("images") or []
        image_url  = data.get("image_url") or (images[0] if images else None)
        local_img  = download_image(image_url, product_id) if image_url else None

        success = False

        if deal_type == "reel":
            # Reel-Video posten
            from config import VIDEOS_QUEUE_DIR
            video_candidates = list(VIDEOS_QUEUE_DIR.glob(f"{product_id}.*"))
            if video_candidates:
                video_path = video_candidates[0]
                thumb = local_img if local_img else None
                media_id = ig_service.post_reel(video_path, caption, thumb)
                success = bool(media_id)
            else:
                print(f"[IG-PROCESS] ⚠️ Kein Video für Reel {product_id} gefunden – überspringe.")
                return False

        if not success:
            print(f"[IG-WARN] ⚠️ Upload fehlgeschlagen für {product_id}. Datei bleibt in queue/.")
            return False

        # Bio-Link + Kommentar mit Affiliate-Link
        if offer_url and media_id:
            ig_service.update_bio_link(offer_url)
            try:
                ig_service.post_comment(media_id, f"🔗 Zum Angebot: {offer_url}")
            except Exception as e:
                print(f"[IG-COMMENT] Konnte Kommentar nicht posten: {e}")

        sent_ids.add(product_id)
        print(f"[IG-DONE] ✅ Instagram-Post erfolgreich: {product_id}")
        return True

    except Exception as e:
        print(f"[IG-ERROR] Fehler bei {full_path.name}: {e}", file=sys.stderr)
        return False
