# telegram/telRouter.py
import os, sys, re, glob, json, asyncio, hashlib, time
from typing import Optional, Union, Iterable, Tuple
from pathlib import Path

from telethon import TelegramClient, Button
from telethon.errors import UserAlreadyParticipantError
from telethon.tl.functions.messages import ImportChatInviteRequest

from dotenv import load_dotenv
load_dotenv()

# Projektwurzel für config.py
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import config
from telegram.login_once import LoginConfig, ensure_logged_in
# NEU: Importiere build_inline_keyboard für flexible Buttons
from telegram.offer_message import build_caption_html, pick_image_source, build_inline_keyboard # Hinzugefügt: build_inline_keyboard

# Settings
INVITE_RE     = re.compile(r"(?:t\.me\/\+|joinchat\/)([A-Za-z0-9_-]+)")
CHANNEL_REF   = os.getenv("CHANNEL_INVITE_URL") or getattr(config, "CHANNEL_INVITE_URL", "")
OUT_DIR: Path = config.OUT_DIR
DATA_DIR: Path = config.DATA_DIR
MAX_TEXT_LEN  = 4096
AFFILIATE_URL = os.getenv("AFFILIATE_URL", "https://amzn.to/42vWlQM")
WATCH_SECS    = int(float(os.getenv("WATCH_INTERVAL_SECS", "10")))  # jede Minute

# Datei im data/-Ordner mit gesendeten ASINs
SENT_LIST_PATH: Path = DATA_DIR / "sent_asins.json"

# Helpers (UNVERÄNDERT)
def chunk_text(s: str, size: int = MAX_TEXT_LEN) -> list[str]:
    s = s or ""
    return [s[i:i+size] for i in range(0, len(s), size)]

def _extract_invite_hash(url: Optional[str]) -> Optional[str]:
    if not url: return None
    m = INVITE_RE.search(url); return m.group(1) if m else None

def _iter_json_files() -> list[str]:
    return sorted(glob.glob(str(OUT_DIR / "*.json")))

def _load_json(fp: str) -> Union[dict, list, str]:
    with open(fp, "r", encoding="utf-8") as f:
        return json.load(f)

def _sha1_file(path: str) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()

def _extract_identity(fp: str, payload: Union[dict, list, str]) -> Tuple[str, str]:
    """
    Liefert (key_type, key_value) für das 'Schon gesendet?'-Register.
    Bevorzugt ASIN aus dict; sonst Hash der Datei.
    """
    if isinstance(payload, dict) and payload.get("asin"):
        return ("asin", str(payload["asin"]))
    # Fallback: Fingerprint der Datei (stabil, falls kein asin vorhanden)
    return ("filehash", _sha1_file(fp))

# Registry (gesendete ASINs / Hashes) laden/speichern (UNVERÄNDERT)
def _load_sent_registry() -> dict:
    if SENT_LIST_PATH.exists():
        try:
            with open(SENT_LIST_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
    return {"asin": [], "filehash": []}

def _save_sent_registry(reg: dict) -> None:
    SENT_LIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(SENT_LIST_PATH, "w", encoding="utf-8") as f:
        json.dump(reg, f, ensure_ascii=False, indent=2)

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
                print("✅ Kanal via Invite betreten.")
            except UserAlreadyParticipantError:
                pass
            except Exception as e:
                print(f"⚠️ Invite fehlgeschlagen: {e}")
        return await client.get_entity(channel_ref)

    async def _send_offer(self, entity, d: dict):
        assert self.client is not None

        caption = build_caption_html(d, AFFILIATE_URL)
        
        # NEU: Flexible Inline-Buttons bauen
        keyboard_data = build_inline_keyboard(d)
        # Telethon erwartet hier eine spezielle Inline-Button-Struktur, wenn es ein Bild ist
        # Konvertierung von Dict in Telethon Button-Struktur
        # Konvertiere das Inline-Keyboard-Dict in eine Telethon-List-of-List-Struktur
        # Nur wenn keyboard_data vorhanden ist
        if keyboard_data and keyboard_data.get("inline_keyboard"):
             buttons = [
                [Button.url(b['text'], b['url']) for b in row]
                for row in keyboard_data['inline_keyboard']
            ]
        else:
            buttons = None # Kein Keyboard
        
        src = pick_image_source(d, config.BASE_DIR)    # lokale Pfade, URLs, Platzhalter

        if src:
            try:
                await self.client.send_file(
                    entity, src,
                    caption=caption, parse_mode="html", buttons=buttons
                )
                return
            except Exception as e:
                # Bessere Fehlermeldung
                print(f"⚠️ Bildversand fehlgeschlagen (Quelle: {src}) – sende Text. Fehler: {e}")

        # Fallback: reine Textnachricht (ein Post, Buttons einmal anhängen)
        if keyboard_data and keyboard_data.get("inline_keyboard"):
             # Konvertiere das Inline-Keyboard-Dict in eine Telethon-List-of-List-Struktur
            buttons_fallback = [
                [Button.url(b['text'], b['url']) for b in row]
                for row in keyboard_data['inline_keyboard']
            ]
        else:
            # Fallback auf den Standard-Button, falls build_inline_keyboard None liefert
            url = (
                d.get("affiliate_url")
                or d.get("product_url")
                or (f"https://www.amazon.de/dp/{d['asin']}" if d.get("asin") else AFFILIATE_URL)
            )
            buttons_fallback = [[Button.url("🛒 Jetzt sichern", url)]]
        
        for i, part in enumerate(chunk_text(caption)):
            await self.client.send_message(
                entity, part, parse_mode="html",
                # Buttons nur beim ersten Teil mitsenden
                buttons=buttons_fallback if i == 0 else None
            )

    async def _send_one_new_item(self, entity) -> bool:
        """ (UNVERÄNDERT) """
        reg = _load_sent_registry()
        files = _iter_json_files()
        for fp in files:
            try:
                payload = _load_json(fp)
            except Exception as e:
                print(f"⚠️ Lesefehler {fp}: {e}")
                continue

            # Nur dict oder list sinnvoll – string wird als Text behandelt
            candidates: Iterable[Union[dict, str]] = []
            if isinstance(payload, dict):
                candidates = [payload]
            elif isinstance(payload, list):
                # nur dict-Einträge posten; strings überspringen
                candidates = [x for x in payload if isinstance(x, dict)]
            else:
                # reine Textdatei – identität per filehash
                ktype, kval = _extract_identity(fp, payload)
                if kval not in reg.get(ktype, []):
                    await self.client.send_message(entity, str(payload), parse_mode="html")
                    reg[ktype].append(kval)
                    _save_sent_registry(reg)
                    return True
                continue

            for item in candidates:
                ktype, kval = _extract_identity(fp, item)
                if kval in reg.get(ktype, []):
                    continue
                # senden
                await self._send_offer(entity, item)
                reg[ktype].append(kval)
                _save_sent_registry(reg)
                return True

        return False    # nichts Neues

    async def run_watch(self):
        """ (UNVERÄNDERT) """
        self.client = await ensure_logged_in(LoginConfig.from_env())
        async with self.client:
            entity = await self._ensure_join_and_resolve(self.client, self.channel_ref)
            print(f"🔎 Watcher aktiv: prüfe {OUT_DIR} alle {WATCH_SECS}s …")
            while True:
                try:
                    sent = await self._send_one_new_item(entity)
                    # Optional: kleines Status-Log
                    if not sent:
                        print("ℹ️ Nichts Neues gefunden.")
                except Exception as e:
                    print(f"❌ Fehler im Watcher: {e}")
                await asyncio.sleep(WATCH_SECS)

    # Alte Einmal-Funktion bleibt verfügbar (falls du sie brauchst)
    async def run_once(self):
        """ (UNVERÄNDERT) """
        self.client = await ensure_logged_in(LoginConfig.from_env())
        async with self.client:
            entity = await self._ensure_join_and_resolve(self.client, self.channel_ref)
            # sendet ALLES (ohne Verschieben), markiert in Registry
            any_sent = False
            while await self._send_one_new_item(entity):
                any_sent = True
            if not any_sent:
                print("ℹ️ Keine neuen Einträge zum Senden.")

# CLI (UNVERÄNDERT)
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
        print("\nAbgebrochen.")