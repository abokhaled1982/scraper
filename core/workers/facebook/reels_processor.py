# reels/reels_processor.py
# Verarbeitet eine einzelne Deal-JSON-Datei für Reels

import asyncio
import json
import pathlib
import re
import sys
import requests
from core.workers.facebook.reels_service import download_video, render_reel, render_typ3_audio
from core.workers.facebook.template_interface import (
    build_modifications_for_template,
    resolve_template_selection,
)

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from core.paths import IMAGES_DIR, VIDEOS_SENT_DIR, VIDEOS_QUEUE_DIR
from core.db import deals_repo
from core.logging import get_logger  # noqa: E402
log = get_logger("reels_processor")  # noqa: E402

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

async def process_single_deal(deal: dict, sent_ids: set) -> bool:
    product_id = deal.get("product_id")
    deal_id = deal.get("id")
    data = deal.get("payload") or {}
    if not product_id or product_id in sent_ids:
        return False
    try:
        # Nur Deals mit "type": "reel" verarbeiten
        if data.get("type") != "reel":
            return False

        validation = validate_deal_data(data)
        if not validation["valid"]:
            log.error(f"[FILTER] 🗑️ {product_id}: {validation['reason']}. Mark failed in DB.")
            if deal_id:
                deals_repo.mark_failed(deal_id, validation["reason"])
            return False
        log.info(f"[PROCESS] 🚀 Guter Deal für Reels ({validation['discount']}%): {product_id}")

        # Prüfe ob ein bereits gerendertes Video in der Video-Queue vorhanden ist
        existing_video = VIDEOS_QUEUE_DIR / f"{product_id}.mp4"
        if existing_video.exists():
            log.info(f"[VIDEO] ♻️  Vorhandenes Video gefunden – Creatomate-Render übersprungen: {existing_video.name}")
            local_video = existing_video
        else:
            template_type, template_id = resolve_template_selection(data, default_template_type="typ3_audio")

            log.info(f"[TEMPLATE] type={template_type} id={template_id}")

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
            log.info(f"[DONE] ✅ Reel erfolgreich gerendert: {product_id}, URL: {render_result.get('url')}")

            # Video herunterladen
            local_video = await asyncio.get_event_loop().run_in_executor(None, download_video, render_result, product_id)
            if local_video:
                log.info(f"[VIDEO] ✅ Video heruntergeladen: {local_video}")
            else:
                log.error(f"[VIDEO] ❌ Video-Download fehlgeschlagen für {product_id}")

        # ── Telegram: Video SOFORT senden (vor Facebook), wie ein normaler Post ──
        # Facebook bleibt mit eigenem Timer; Telegram darf nicht warten.
        if local_video and pathlib.Path(local_video).exists():
            try:
                from core.workers.telegram.tel_video_sender import send_reel_video
                tg_ok = await send_reel_video(pathlib.Path(local_video), data)
                if tg_ok:
                    log.info(f"[TELEGRAM] ✅ Reel sofort an Telegram gesendet: {product_id}")
                else:
                    log.warning(f"[TELEGRAM] ⚠️ Telegram-Versand fehlgeschlagen – FB-Flow läuft weiter.")
            except Exception as tg_e:
                log.error(f"[TELEGRAM] ⚠️ Fehler beim Telegram-Video-Versand: {tg_e}")
        # ─────────────────────────────────────────────────────────────────────────

        # Sende an Facebook-Addon
        from core.workers.facebook import fb_service
        sent = await fb_service.send_post(data, None, local_video)
        if sent:
            log.info(f"[FACEBOOK] ✅ Reel erfolgreich gepostet: {product_id}")

            # ── Instagram: gleiches Video posten ─────────────────────────────
            try:
                import core.workers.instagram.ig_service as ig_service
                from core.workers.instagram.ig_message import create_ig_caption
                ig_caption = create_ig_caption(data)
                video_path = pathlib.Path(local_video) if local_video else None
                if video_path and video_path.exists():
                    media_id = ig_service.post_reel(video_path, ig_caption)
                    if media_id:
                        log.info(f"[INSTAGRAM] ✅ Reel auch auf Instagram gepostet: {product_id}")
                        # Affiliate-Link als Kommentar + Bio-Link aktualisieren
                        offer_url = str(data.get("affiliate_url") or data.get("url") or "").strip()
                        if offer_url and offer_url not in ("N/A", "null", ""):
                            # Bio-Link auf aktuelles Angebot setzen (klickbar!)
                            ig_service.update_bio_link(offer_url)
                            # Kommentar als Backup
                            try:
                                ig_service.post_comment(media_id, f"🔗 Zum Angebot: {offer_url}")
                            except Exception as ce:
                                log.warning(f"[INSTAGRAM] ⚠️ Kommentar fehlgeschlagen: {ce}")
                    else:
                        log.warning(f"[INSTAGRAM] ⚠️ Instagram-Upload fehlgeschlagen – FB-Post bleibt gültig.")
                else:
                    log.warning(f"[INSTAGRAM] ⚠️ Video-Datei nicht mehr vorhanden – Instagram übersprungen.")
            except SystemExit as e:
                # ig_service wirft SystemExit wenn keine Session → nur warnen, nicht abbrechen
                log.warning(f"[INSTAGRAM] ⚠️ Kein Instagram-Login – übersprungen. ({e})")
            except Exception as ig_e:
                log.error(f"[INSTAGRAM] ⚠️ Fehler beim Instagram-Post: {ig_e}")
            # ─────────────────────────────────────────────────────────────────

            # Deal in DB als 'sent' markieren — NUR wenn wirklich erfolgreich
            if deal_id:
                deals_repo.mark_sent(deal_id, detail="facebook+instagram")
            # Video nach media/videos/sent/ verschieben
            if local_video and pathlib.Path(local_video).exists():
                dest_video = VIDEOS_SENT_DIR / pathlib.Path(local_video).name
                pathlib.Path(local_video).rename(dest_video)
                log.info(f"[VIDEO] ✅ Video nach sent/ verschoben: {dest_video.name}")
            sent_ids.add(product_id)
            return True
        else:
            log.error(f"[FACEBOOK] ❌ Reel konnte nicht gepostet werden (Addon Fehler/Timeout): {product_id}. Deal bleibt in Queue.")
            return False
    except Exception as e:
        log.error(f"[ERROR] Fehler bei {product_id}: {e}")
        if deal_id:
            try:
                deals_repo.mark_failed(deal_id, str(e))
            except Exception:
                pass
        return False


# ───────────────────────────────────────────────────────────────
# Reusable: nur das Rendering + Video-Download (ohne FB/IG/TG-Versand).
# Wird auch von telRouter genutzt, wenn Facebook ausgeschaltet ist.
# ───────────────────────────────────────────────────────────────
async def render_reel_for_deal(deal: dict) -> pathlib.Path | None:
    """Rendert (oder findet im Cache) ein Reel-Video für einen Queue-Deal.
    Markiert den Deal bei Validierungsfehler als failed. Rückgabe: Pfad zur
    MP4 oder None."""
    product_id = deal.get("product_id")
    deal_id = deal.get("id")
    data = deal.get("payload") or {}
    if data.get("type") != "reel":
        return None
    validation = validate_deal_data(data)
    if not validation["valid"]:
        log.error(f"[FILTER] 🗑️ {product_id}: {validation['reason']}. Mark failed in DB.")
        if deal_id:
            deals_repo.mark_failed(deal_id, validation["reason"])
        return None

    existing_video = VIDEOS_QUEUE_DIR / f"{product_id}.mp4"
    if existing_video.exists():
        log.info(f"[VIDEO] ♻️ Vorhandenes Video gefunden – Render übersprungen: {existing_video.name}")
        return existing_video

    # 💰 Resend-Schutz: nach erfolgreichem Versand liegt das Video in sent/.
    # Statt erneut bei Creatomate zu zahlen, schieben wir es zurück in queue/.
    sent_video = VIDEOS_SENT_DIR / f"{product_id}.mp4"
    if sent_video.exists():
        try:
            VIDEOS_QUEUE_DIR.mkdir(parents=True, exist_ok=True)
            sent_video.rename(existing_video)
            log.info(f"[VIDEO] ♻️ Cache-Hit im sent/-Ordner → wiederverwendet (kein Re-Render): {existing_video.name}")
            return existing_video
        except Exception as e:
            log.warning(f"[VIDEO] Konnte Cache-Video nicht aus sent/ zurückholen ({e}) – kopiere stattdessen")
            try:
                import shutil
                shutil.copy2(sent_video, existing_video)
                return existing_video
            except Exception as e2:
                log.error(f"[VIDEO] Auch Copy fehlgeschlagen: {e2} – fahre mit Re-Render fort")

    template_type, template_id = resolve_template_selection(data, default_template_type="typ3_audio")
    log.info(f"[TEMPLATE] type={template_type} id={template_id}")
    loop = asyncio.get_event_loop()
    if template_type == "typ3_audio":
        render_result = await loop.run_in_executor(None, render_typ3_audio, data, template_id)
    else:
        modifications = build_modifications_for_template(
            data, template_type=template_type, discount_value=validation["discount"],
        )
        render_result = await loop.run_in_executor(None, render_reel, modifications, template_id)
    log.info(f"[DONE] ✅ Reel gerendert: {product_id}, URL: {render_result.get('url')}")
    local_video = await loop.run_in_executor(None, download_video, render_result, product_id)
    if not local_video:
        log.error(f"[VIDEO] ❌ Video-Download fehlgeschlagen für {product_id}")
        return None
    return pathlib.Path(local_video)