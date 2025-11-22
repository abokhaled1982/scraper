# telegram/telObserver_piraten.py  (angepasst)
import os
import sys
import asyncio
import re
import json
import hashlib
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Tuple, Any
from telethon.tl.functions.channels import JoinChannelRequest

from dotenv import load_dotenv
load_dotenv()

# Projektwurzel in sys.path aufnehmen
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Telethon Importe
try:
    from telethon import events, TelegramClient
    from telethon.errors import UserAlreadyParticipantError
    from telethon.tl.functions.messages import ImportChatInviteRequest
except Exception as e:
    print("❌ Fehlende Abhängigkeit: telethon.")
    raise

# Login Helper Import (falls vorhanden)
try:
    from telegram.login_once import LoginConfig, ensure_logged_in
except Exception:
    # Fallback-Implementierung (einfacher)
    class LoginConfig:
        def __init__(self, api_id, api_hash, session_name, session_dir, phone, password):
            self.api_id = api_id
            self.api_hash = api_hash
            self.session_name = session_name
            self.session_dir = session_dir
            self.phone = phone
            self.password = password

    async def ensure_logged_in(cfg):
        session_file = os.path.join(cfg.session_dir, cfg.session_name)
        client = TelegramClient(session_file, cfg.api_id, cfg.api_hash)
        await client.start(phone=cfg.phone, password=cfg.password)
        return client

# ------------------------
# ENV / Konfiguration (PIRATEN SPEZIFISCH)
# ------------------------
PRODUCT_LIST_PATH = None
LOCK_FILE = None
try:
    import config
    PRODUCT_LIST_PATH = Path(getattr(config, "PRODUCT_LIST_PATH", "product_list.json"))
    LOCK_FILE = Path(getattr(config, "LOCK_FILE", ".locks/product_list.lock"))
except Exception:
    # Fallback auf Umgebungsvariablen / defaults
    PRODUCT_LIST_PATH = Path(os.getenv("PRODUCT_LIST_PATH", "product_list.json"))
    LOCK_FILE = Path(os.getenv("LOCK_FILE", ".locks/product_list.lock"))


API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
SESSION_DIR = os.getenv("SESSION_DIR", ".sessions")
PHONE = os.getenv("TELEGRAM_PHONE")
PASSWORD = os.getenv("TELEGRAM_PASSWORD")

# WICHTIG: Env-Variable für den Piraten-Kanal, z.B. PIRATEN_CHANNEL_INVITE_URL=https://t.me/PirateDeals
PIRATEN_CHANNEL_REF = (os.getenv("PIRATEN_CHANNEL_INVITE_URL") or "").strip()
PIRATEN_SESSION_NAME = os.getenv("PIRATEN_SESSION_NAME", "piraten_session")

if not API_ID or not API_HASH:
    raise SystemExit("❌ API_ID und API_HASH fehlen in .env")
if not PIRATEN_CHANNEL_REF:
    raise SystemExit("❌ PIRATEN_CHANNEL_INVITE_URL fehlt in .env! (z.B. https://t.me/PirateDeals)")

PIRATEN_CFG = LoginConfig(API_ID, API_HASH, PIRATEN_SESSION_NAME, SESSION_DIR, PHONE, PASSWORD)

# ------------------------
# ATOMARE DATEI-HELPER
# ------------------------
try:
    import fcntl
    @contextmanager
    def _locked_file(lock_path: Path):
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "a+") as fp:
            fcntl.flock(fp.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fp.fileno(), fcntl.LOCK_UN)
except Exception:
    @contextmanager
    def _locked_file(lock_path: Path):
        # Windows / fallback: no-op
        yield

def load_store(path: Path) -> Dict[str, Dict]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"⚠️ Fehler beim Laden von {path} ({e}), erstelle neue leere Datenbank.")
        return {}

def save_store(path: Path, store: Dict[str, Dict]):
    temp_path = path.with_suffix(".tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    with temp_path.open('w', encoding='utf-8') as f:
        json.dump(store, f, ensure_ascii=False, indent=2, sort_keys=True)
    temp_path.replace(path)

def product_key(item: Dict[str, Any]) -> str:
    url = item.get("product_url", "").strip()
    if not url:
        return f"R-{hashlib.sha1(str(time.time()).encode()).hexdigest()[:10]}"
    # Versuch ASIN-Extraktion (Amazon)
    m = re.search(r'/(?:dp|gp/product|d|o)/([A-Z0-9]{10})(?:[\/?]|$)', url, re.IGNORECASE)
    if m:
        asin = m.group(1).upper()
        return f"A-{asin}"
    return f"U-{hashlib.sha1(url.encode()).hexdigest()[:10]}"

def add_link_to_product_list(url: str) -> Tuple[bool, str]:
    minimal = {
        "product_url": url,
        "source": "telegram_piraten",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    key = product_key(minimal)
    with _locked_file(LOCK_FILE):
        store = load_store(PRODUCT_LIST_PATH)
        if key in store:
            return False, "Link bereits vorhanden"
        store[key] = minimal
        save_store(PRODUCT_LIST_PATH, store)
    return True, f"Hinzugefügt (Key: {key})"

# ------------------------
# Channel Management (robust)
# ------------------------
async def _ensure_join_and_resolve(client: TelegramClient, ref: str):
    print(f"ℹ️ Versuche Kanal/Entität zu lösen: {ref}")
    
    entity = None
    
    # 1. Versuch: Ist es ein Private Invite Link (t.me/+)
    invite_match = re.search(r'(?:t\.me\/joinchat\/|t\.me\/\+|invite\/)([A-Za-z0-9_-]+)', ref)
    if invite_match:
        invite_hash = invite_match.group(1)
        try:
            await client(ImportChatInviteRequest(invite_hash))
            print(f"✅ Kanal beigetreten via Invite-Hash: {invite_hash}")
        except UserAlreadyParticipantError:
            pass # Alles gut, schon drin
        except Exception as e:
            print(f"⚠️ Invite via Hash fehlgeschlagen: {e}")

    # 2. Versuch: Öffentlicher Username / URL (z.B. t.me/PirateDeals)
    # Wir säubern den Link, um nur den Usernamen zu bekommen
    clean_ref = re.sub(r'https?:\/\/t\.me\/', '', ref).strip('/ ')
    if clean_ref.startswith('@'):
        clean_ref = clean_ref[1:]

    try:
        # Erst versuchen wir, die Entität zu finden
        entity = await client.get_entity(clean_ref)
        print(f"✅ Entity gefunden: {getattr(entity, 'title', clean_ref)}")
        
        # WICHTIG: Jetzt explizit beitreten, falls es ein öffentlicher Kanal ist
        # Bei privaten Chats würde das fehlschlagen, daher try/except
        try:
            await client(JoinChannelRequest(entity))
            print("✅ Erfolgreich dem öffentlichen Kanal beigetreten (oder war bereits drin).")
        except UserAlreadyParticipantError:
            pass
        except Exception as e_join:
            # Manche Entities (z.B. Chats) erlauben kein JoinChannelRequest, das ist okay
            print(f"ℹ️ Kein expliziter Join nötig oder möglich: {e_join}")
            
        return entity

    except Exception as e:
        print(f"❌ Kritischer Fehler: Konnte {ref} nicht auflösen oder beitreten.")
        print(f"   Fehler: {e}")
        raise
# ------------------------
# Message Handling (wie telObserver)
# ------------------------
async def handle_message(evt: events.NewMessage.Event):
    msg = evt.message
    try:
        chat = await evt.get_chat()
        chat_name = getattr(chat, "title", None) or getattr(chat, "username", None) or "Kanal"
    except Exception:
        chat_name = "Kanal"

    text = (msg.message or "").strip()
    # Regex: HTTPS Links, schneidet trailing punctuation ab; t.me-Links werden absichtlich nicht als Produkt-Links behandelt
    url_pattern = re.compile(r'(https?:\/\/(?!t\.me\/)[^\s<>"\'\)\]]+[^\s\.,;:!?\)\]\<>"\'])', re.IGNORECASE)
    found = url_pattern.findall(text)
    links = []
    for f in found:
        if isinstance(f, tuple):
            links.append(f[0])
        else:
            links.append(f)

    log_preview = text.replace('\n', ' ')[:200]
    if links:
        added_count = 0
        print(f"[PIRATEN:{chat_name}] Nachricht (Links gefunden: {len(links)}) -> {log_preview}")
        for link in set(links):
            try:
                success, reason = add_link_to_product_list(link)
                if success:
                    added_count += 1
                    print(f"[PIRATEN] ✅ Hinzugefügt: {link} ({reason})")
                else:
                    print(f"[PIRATEN] ℹ️ {reason}: {link}")
            except Exception as e:
                print(f"[PIRATEN] Fehler beim Hinzufügen des Links {link}: {e}")
        if added_count > 0:
            print(f"[PIRATEN:{chat_name}] ✅ {added_count} neue Links zu {PRODUCT_LIST_PATH} hinzugefügt.")
    else:
        if log_preview:
            print(f"[PIRATEN:{chat_name}] {log_preview}")
        else:
            print(f"[PIRATEN:{chat_name}] [Medien/Leer]")

# ------------------------
# Main Loop
# ------------------------
async def _amain():
    print(f"🏴‍☠️ Starte Piraten-Observer Session: {PIRATEN_SESSION_NAME}")
    client = await ensure_logged_in(PIRATEN_CFG)
    async with client:
        entity = await _ensure_join_and_resolve(client, PIRATEN_CHANNEL_REF)
        print(f"🏴‍☠️ Überwache Kanal: {getattr(entity, 'title', PIRATEN_CHANNEL_REF)}")

        @client.on(events.NewMessage(chats=entity))
        async def _on(evt):
            try:
                await handle_message(evt)
            except Exception as e:
                print(f"❌ Piraten-Error: {e}")

        await client.run_until_disconnected()

if __name__ == "__main__":
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        pass
