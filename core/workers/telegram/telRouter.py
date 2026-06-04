# telegram/telRouter.py
import os, sys, re, glob, json, asyncio, hashlib, time
from typing import Optional, Union, Iterable, Tuple, Any
from pathlib import Path

# NEU: aiohttp für den asynchronen Download
import aiohttp 

from telethon import TelegramClient, Button
from telethon.errors import UserAlreadyParticipantError
from telethon.tl.functions.messages import ImportChatInviteRequest

from dotenv import load_dotenv
load_dotenv()

# Projektwurzel für config.py
PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from core.logging import get_logger  # noqa: E402
log = get_logger("telRouter")  # noqa: E402

from core import paths as config
from core.db import deals_repo, state_repo, workers_repo, config_repo as cfg
from core.workers.telegram.login_once import LoginConfig, ensure_logged_in
from core.workers.telegram.offer_message import build_caption_html, pick_image_source, build_inline_keyboard
# NEU: Import der Bildverarbeitungs-Logik
from core.workers.telegram.image_processor import get_best_image_url, download_and_convert_to_jpg ,url_needs_local_processing

# Settings
INVITE_RE     = re.compile(r"(?:t\.me\/\+|joinchat\/)([A-Za-z0-9_-]+)")
CHANNEL_REF   = os.getenv("CHANNEL_INVITE_URL") or getattr(config, "CHANNEL_INVITE_URL", "")
MAX_TEXT_LEN  = 4096
AFFILIATE_URL = os.getenv("AFFILIATE_URL", "https://amzn.to/42vWlQM")
WATCH_SECS    = int(float(os.getenv("WATCH_INTERVAL_SECS", "10")))  # alle 10s

# DB-Keys
_SENT_ASINS_KEY = "sent_asins"
_WORKER = "telRouter"

# Helpers
def chunk_text(s: str, size: int = MAX_TEXT_LEN) -> list[str]:
    s = s or ""
    return [s[i:i+size] for i in range(0, len(s), size)]

def _extract_invite_hash(url: Optional[str]) -> Optional[str]:
    if not url: return None
    m = INVITE_RE.search(url); return m.group(1) if m else None

def _iter_queue_deals() -> list[dict]:
    return deals_repo.list_queue()

def _sha1_payload(payload: Any) -> str:
    h = hashlib.sha1()
    h.update(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8", errors="ignore"))
    return h.hexdigest()

def _extract_identity(payload: Union[dict, list, str]) -> Tuple[str, str]:
    """
    Liefert (key_type, key_value) für das 'Schon gesendet?'-Register.
    Unterstützt ASIN (Alt-Schema) und product_id (BO-Schema).
    """
    if isinstance(payload, dict):
        if payload.get("asin"):
            return ("asin", str(payload["asin"]))
        if payload.get("product_id"):
            return ("asin", str(payload["product_id"]))  # gleiche Liste wiederverwenden
    # Fallback: Fingerprint des Payloads
    return ("filehash", _sha1_payload(payload))

# Registry laden/speichern (in state_kv)
def _load_sent_registry() -> dict:
    data = state_repo.get_dict(_SENT_ASINS_KEY)
    if isinstance(data, dict):
        data.setdefault("asin", [])
        data.setdefault("filehash", [])
        return data
    return {"asin": [], "filehash": []}

def _save_sent_registry(reg: dict) -> None:
    state_repo.put(_SENT_ASINS_KEY, reg)

# Router
class TelegramOfferRouter:
    def __init__(self, channel_ref: str):
        self.channel_ref = channel_ref
        self.client: Optional[TelegramClient] = None

    async def _ensure_join_and_resolve(self, client: TelegramClient, channel_ref: str):
        invite = _extract_invite_hash(channel_ref)
        if invite:
            try:
                await client(ImportChatInviteRequest(invite))
                log.info("✅ Kanal via Invite betreten.")
            except UserAlreadyParticipantError:
                pass
            except Exception as e:
                log.warning(f"⚠️ Invite fehlgeschlagen: {e}")
        return await client.get_entity(channel_ref)

    async def _send_offer(self, entity, d: dict):
        assert self.client is not None

        caption = build_caption_html(d, AFFILIATE_URL)

        # Inline-Keyboard (optional)
        keyboard_data = build_inline_keyboard(d)
        buttons = None
        if keyboard_data and keyboard_data.get("inline_keyboard"):
            buttons = [
                [Button.url(b['text'], b['url']) for b in row]
                for row in keyboard_data['inline_keyboard']
            ]

        # 1. Bildquelle (Lokale Datei/Placeholder) suchen
        src: Optional[str] = pick_image_source(d, config.BASE_DIR)
        
        # Temp-Pfad für das heruntergeladene Bild initialisieren
        temp_img_path: Optional[Path] = None
        
        # 2. Wenn keine lokale Datei gefunden wurde, versuche Download & Konvertierung
        if not src:
            image_url = get_best_image_url(d)
            if image_url:
                if url_needs_local_processing(image_url): # <-- NEU: PRÜFEN, OB KONVERTIERUNG NÖTIG
                    # !!! ASYNCHRONER DOWNLOAD und KONVERTIERUNG (für WebP/GIF)
                    temp_img_path = await download_and_convert_to_jpg(image_url)
                    if temp_img_path:
                        src = str(temp_img_path)
                else:
                    # Für alle anderen Formate (JPG, PNG etc.): Direkt die URL als Quelle nutzen
                    # Telethon kann Bilder oft direkt von der URL senden, das ist schneller
                    src = image_url
                    
        # --- Bild Senden Logik ---
        
        try:
            if src:
                try:
                    # Sende die gefundene Quelle (lokaler Pfad oder Temp-JPG)
                    await self.client.send_file(
                        entity, src,
                        caption=caption, parse_mode="html", buttons=buttons
                    )
                    return
                except Exception as e:
                    # Wenn Senden fehlschlägt (z.B. wegen zu großer Datei), 
                    # loggen und zum Text-Fallback übergehen.
                    log.error(f"⚠️ Bildversand fehlgeschlagen (Quelle: {src}) – sende Text. Fehler: {e}")

            # Fallback: reine Textnachricht
            if not buttons:
                url = (
                    d.get("affiliate_url")
                    or d.get("product_url")
                    or (f"https://www.amazon.de/dp/{d['asin']}" if d.get("asin") else
                        f"https://www.amazon.de/dp/{d['product_id']}" if d.get("product_id") else
                        AFFILIATE_URL)
                )
                buttons = [[Button.url("🛒 Jetzt sichern", url)]]

            # Text-Nachricht aufteilen
            for i, part in enumerate(chunk_text(caption)):
                await self.client.send_message(
                    entity, part, parse_mode="html",
                    buttons=buttons if i == 0 else None
                )
                
        finally:
            # 3. AUFRÄUMEN: Temporäre Datei sicher löschen (wichtig!)
            if temp_img_path and temp_img_path.exists():
                try:
                    temp_img_path.unlink()
                except Exception as e:
                    log.error(f"❌ Fehler beim Löschen der temporären Datei {temp_img_path}: {e}")

    async def _send_one_new_item(self, entity) -> bool:
        reg = _load_sent_registry()
        deals = _iter_queue_deals()
        for deal in deals:
            payload = deal.get("payload")
            deal_id = deal.get("id")
            product_id = deal.get("product_id")
            if not isinstance(payload, dict):
                # Kaputter Payload → Deal als failed markieren, damit er die
                # Queue nicht für immer blockiert.
                if deal_id:
                    try:
                        deals_repo.mark_failed(deal_id, "invalid payload (no dict)")
                        log.warning(f"[QUEUE] 🗑️ Deal #{deal_id} ohne dict-Payload → failed")
                    except Exception:
                        pass
                continue

            is_reel = payload.get("type") == "reel"
            # Reels: Facebook ist Master, wenn aktiv. Dann lässt telRouter den
            # Eintrag für fb_watcher liegen — KEIN auto-mark-sent.
            if is_reel and cfg.is_enabled("facebook"):
                continue

            ktype, kval = _extract_identity(payload)
            # 🔧 KEIN stilles Skip mehr: Wenn der Eintrag bereits im
            # Duplikat-Register steht, wird der Deal jetzt aktiv aus der
            # Queue genommen (mark_sent mit detail), sonst klebt er ewig.
            if kval in reg.get(ktype, []):
                if deal_id:
                    try:
                        deals_repo.mark_sent(deal_id, detail="duplicate-skipped (sent_asins)")
                        log.info(f"[QUEUE] ⏭️ Deal #{deal_id} ({product_id}) bereits im sent_asins-Register → als sent markiert (Queue geleert)")
                    except Exception as e:
                        log.warning(f"[QUEUE] mark_sent failed für #{deal_id}: {e}")
                continue

            if is_reel:
                ok = await self._send_reel_standalone(deal)
                if ok:
                    reg.setdefault(ktype, []).append(kval)
                    _save_sent_registry(reg)
                    return True
                continue

            await self._send_offer(entity, payload)
            reg.setdefault(ktype, []).append(kval)
            _save_sent_registry(reg)
            # Standard-Offer wird im _send_offer nicht via mark_sent_by_product_id
            # markiert. Hier nachholen, damit der Deal aus 'queue' verschwindet.
            if deal_id:
                try:
                    deals_repo.mark_sent(deal_id, detail="telegram-offer")
                except Exception as e:
                    log.warning(f"[QUEUE] mark_sent (offer) failed für #{deal_id}: {e}")
            return True
        return False

    async def _send_reel_standalone(self, deal: dict) -> bool:
        """Rendert + sendet ein Reel ausschließlich an Telegram (FB aus)."""
        from pathlib import Path as _P
        from core.workers.facebook.reels_processor import render_reel_for_deal
        from core.paths import VIDEOS_SENT_DIR
        from core.workers.telegram.tel_video_sender import send_reel_video
        product_id = deal.get("product_id")
        deal_id    = deal.get("id")
        payload    = deal.get("payload") or {}
        try:
            video_path = await render_reel_for_deal(deal)
        except Exception as e:
            log.error(f"[REEL-TG] Render-Fehler {product_id}: {e}")
            return False
        if not video_path or not video_path.exists():
            log.warning(f"[REEL-TG] Kein Video für {product_id}")
            return False
        try:
            ok = await send_reel_video(video_path, payload)
        except Exception as e:
            log.error(f"[REEL-TG] Telegram-Versand fehlgeschlagen {product_id}: {e}")
            return False
        if not ok:
            log.warning(f"[REEL-TG] Telegram-Versand nicht bestätigt: {product_id}")
            return False
        log.info(f"[REEL-TG] ✅ Reel an Telegram gesendet: {product_id}")
        if deal_id:
            try:
                deals_repo.mark_sent(deal_id, detail="telegram-only")
            except Exception as e:
                log.warning(f"[REEL-TG] mark_sent fehlgeschlagen: {e}")
        try:
            VIDEOS_SENT_DIR.mkdir(parents=True, exist_ok=True)
            video_path.rename(VIDEOS_SENT_DIR / video_path.name)
        except Exception as e:
            log.warning(f"[REEL-TG] Video-Move fehlgeschlagen: {e}")
        return True

    async def run_watch(self):
        workers_repo.register(_WORKER)
        self.client = await ensure_logged_in(LoginConfig.from_env())
        async with self.client:
            entity = await self._ensure_join_and_resolve(self.client, self.channel_ref)
            log.info(f"🔎 Telegramm Watcher aktiv: prüfe DB-Queue alle {WATCH_SECS}s …")
            while True:
                try:
                    workers_repo.set_idle(_WORKER)
                    sent = await self._send_one_new_item(entity)
                    if not sent:
                        log.info("ℹ️ Nichts Neues gefunden.")
                except Exception as e:
                    log.error(f"❌ Fehler im Watcher Telegram: {e}")
                await asyncio.sleep(WATCH_SECS)

    async def run_once(self):
        self.client = await ensure_logged_in(LoginConfig.from_env())
        async with self.client:
            entity = await self._ensure_join_and_resolve(self.client, self.channel_ref)
            any_sent = False
            while await self._send_one_new_item(entity):
                any_sent = True
            if not any_sent:
                log.info("ℹ️ Keine neuen Einträge zum Senden.")

# CLI
async def _amain():
    if not CHANNEL_REF:
        raise SystemExit("Bitte CHANNEL_INVITE_URL in .env oder config.py setzen.")
    mode = os.getenv("ROUTER_MODE", "watch")    # "watch" oder "once"
    router = TelegramOfferRouter(CHANNEL_REF)
    if mode == "once":
        await router.run_once()
    else:
        await router.run_watch()

if __name__ == "__main__":
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        log.info("\nAbgebrochen.")