# reels/reels_processor.py
# Verarbeitet eine einzelne Deal-JSON-Datei für Reels

import asyncio
import json
import pathlib
import re
import sys
import requests
from facebook.reels_service import download_video, render_reel, render_typ3_audio
from facebook.template_interface import (
    build_modifications_for_template,
    resolve_template_selection,
)

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from config import IMAGES_DIR, DEALS_SENT_DIR, DEALS_FAILED_DIR, VIDEOS_SENT_DIR, VIDEOS_QUEUE_DIR

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
    # Titel-Check
    title = str(data.get("title") or "").strip()
    if not title or title.upper() == "N/A":
        return {"valid": False, "reason": "Kein gültiger Titel vorhanden", "discount": 0}

    # URL-Check
    if not data.get("affiliate_url"):
        return {"valid": False, "reason": "Daten unvollständig (Titel/URL fehlt)", "discount": 0}

    # Preis-Check: 0.00, N/A oder fehlend → ungültig
    price_raw = data.get("price") or {}
    price_str = str(price_raw.get("raw") if isinstance(price_raw, dict) else price_raw).strip()
    if _is_empty(price_str):
        return {"valid": False, "reason": f"Kein gültiger Preis vorhanden ('{price_str}')", "discount": 0}

    # Bild-Check: mindestens eine verwertbare Bild-URL
    images    = data.get("images") or []
    image_url = data.get("image_url") or (images[0] if images else None)
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

        # Nur Dateien mit "type": "reel" verarbeiten
        if data.get("type") != "reel":
            return False

        validation = validate_deal_data(data)
        if not validation["valid"]:
            print(f"[FILTER] 🗑️ {full_path.name}: {validation['reason']}. Verschiebe nach failed.")
            DEALS_FAILED_DIR.mkdir(parents=True, exist_ok=True)
            full_path.rename(DEALS_FAILED_DIR / full_path.name)
            return False
        print(f"[PROCESS] 🚀 Guter Deal für Reels ({validation['discount']}%): {product_id}")

        # Prüfe ob ein bereits gerendertes Video in der Video-Queue vorhanden ist
        existing_video = VIDEOS_QUEUE_DIR / f"{product_id}.mp4"
        if existing_video.exists():
            print(f"[VIDEO] ♻️  Vorhandenes Video gefunden – Creatomate-Render übersprungen: {existing_video.name}")
            local_video = existing_video
        else:
            template_type, template_id = resolve_template_selection(data, default_template_type="typ3_audio")

            print(f"[TEMPLATE] type={template_type} id={template_id}")

            if template_type == "typ3_audio":
                # Audio-Reel: ElevenLabs-Voiceover via Creatomate
                render_result = await asyncio.get_event_loop().run_in_executor(
                    None,
                    render_typ3_audio,
                    data,
                    template_id,
                )
            else:
                modifications = build_modifications_for_template(
                    data,
                    template_type=template_type,
                    discount_value=validation["discount"],
                )
                render_result = await asyncio.get_event_loop().run_in_executor(
                    None,
                    render_reel,
                    modifications,
                    template_id,
                )
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
            print(f"[FACEBOOK] ✅ Reel erfolgreich gepostet: {product_id}")

            # ── Instagram: gleiches Video posten ─────────────────────────────
            try:
                import instagram.ig_service as ig_service
                from instagram.ig_message import create_ig_caption
                ig_caption = create_ig_caption(data)
                video_path = pathlib.Path(local_video) if local_video else None
                if video_path and video_path.exists():
                    media_id = ig_service.post_reel(video_path, ig_caption)
                    if media_id:
                        print(f"[INSTAGRAM] ✅ Reel auch auf Instagram gepostet: {product_id}")
                        # Affiliate-Link als Kommentar + Bio-Link aktualisieren
                        offer_url = str(data.get("affiliate_url") or data.get("url") or "").strip()
                        if offer_url and offer_url not in ("N/A", "null", ""):
                            # Bio-Link auf aktuelles Angebot setzen (klickbar!)
                            ig_service.update_bio_link(offer_url)
                            # Kommentar als Backup
                            try:
                                ig_service.post_comment(media_id, f"🔗 Zum Angebot: {offer_url}")
                            except Exception as ce:
                                print(f"[INSTAGRAM] ⚠️ Kommentar fehlgeschlagen: {ce}")
                    else:
                        print(f"[INSTAGRAM] ⚠️ Instagram-Upload fehlgeschlagen – FB-Post bleibt gültig.")
                else:
                    print(f"[INSTAGRAM] ⚠️ Video-Datei nicht mehr vorhanden – Instagram übersprungen.")
            except SystemExit as e:
                # ig_service wirft SystemExit wenn keine Session → nur warnen, nicht abbrechen
                print(f"[INSTAGRAM] ⚠️ Kein Instagram-Login – übersprungen. ({e})")
            except Exception as ig_e:
                print(f"[INSTAGRAM] ⚠️ Fehler beim Instagram-Post: {ig_e}")
            # ─────────────────────────────────────────────────────────────────

            # JSON nach deals/sent/ verschieben — NUR wenn wirklich erfolgreich
            dest_json = DEALS_SENT_DIR / full_path.name
            full_path.rename(dest_json)
            # Video nach media/videos/sent/ verschieben
            if local_video and pathlib.Path(local_video).exists():
                dest_video = VIDEOS_SENT_DIR / pathlib.Path(local_video).name
                pathlib.Path(local_video).rename(dest_video)
                print(f"[VIDEO] ✅ Video nach sent/ verschoben: {dest_video.name}")
            sent_ids.add(product_id)
            return True
        else:
            print(f"[FACEBOOK] ❌ Reel konnte nicht gepostet werden (Addon Fehler/Timeout): {product_id}. Datei bleibt in queue/.")
            return False
    except Exception as e:
        print(f"[ERROR] Fehler bei {full_path.name}: {e}", file=sys.stderr)
        return False