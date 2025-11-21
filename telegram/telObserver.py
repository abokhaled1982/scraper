# telegram/telObserver.py  (ersetzt die bisherige Datei)
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

from dotenv import load_dotenv
load_dotenv()

# Projektwurzel in sys.path aufnehmen (falls nötig)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Telethon
try:
    from telethon import events, TelegramClient
    from telethon.errors import UserAlreadyParticipantError
    from telethon.tl.functions.messages import ImportChatInviteRequest
except Exception as e:
    print("❌ Fehlende Abhängigkeit: telethon. Bitte installieren: pip install telethon")
    raise

# Dein bestehender Login-Helper (falls vorhanden)
try:
    from telegram.login_once import LoginConfig, ensure_logged_in
except Exception:
    # Fallback simple LoginConfig-Standin, falls login_once nicht vorhanden ist.
    class LoginConfig:
        def __init__(self, api_id, api_hash, session_name, session_dir, phone, password):
            self.api_id = api_id
            self.api_hash = api_hash
            self.session_name = session_name
            self.session_dir = session_dir
            self.phone = phone
            self.password = password

    async def ensure_logged_in(cfg):
        # einfacher TelegramClient-Fallback (interactive login required on first run)
        session_file = os.path.join(cfg.session_dir, cfg.session_name)
        client = TelegramClient(session_file, cfg.api_id, cfg.api_hash)
        await client.start(phone=cfg.phone, password=cfg.password)
        return client

# config.py (optional). Wenn nicht vorhanden: Fallback auf ENV
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

# ------------------------
# ENV / Konfiguration
# ------------------------
def env_or_exit(key: str):
    v = os.getenv(key)
    if not v:
        raise SystemExit(f"❌ Fehlende Umgebungsvariable: {key} (bitte in deiner .env setzen)")
    return v

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
SESSION_DIR = os.getenv("SESSION_DIR", ".sessions")
OBS_SESSION_NAME = os.getenv("OBS_SESSION_NAME", "observer_session")
OBS_CHANNEL_REF = (os.getenv("OBS_CHANNEL_INVITE_URL") or "").strip()
PHONE = os.getenv("TELEGRAM_PHONE")
PASSWORD = os.getenv("TELEGRAM_PASSWORD")

if not API_ID or not API_HASH:
    raise SystemExit("❌ API_ID und API_HASH müssen in der .env stehen.")
if not OBS_CHANNEL_REF:
    raise SystemExit("❌ OBS_CHANNEL_INVITE_URL fehlt in .env (z.B. https://t.me/PirateDeals)")

OBS_CFG = LoginConfig(API_ID, API_HASH, OBS_SESSION_NAME, SESSION_DIR, PHONE, PASSWORD)

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
        # no-op fallback (Windows)
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
    # Versuche ASIN (nutze Original-URL, NICHT lowercased)
    m = re.search(r'/(?:dp|gp/product|d|o)/([A-Z0-9]{10})(?:[\/?]|$)', url, re.IGNORECASE)
    if m:
        asin = m.group(1).upper()
        return f"A-{asin}"
    return f"U-{hashlib.sha1(url.encode()).hexdigest()[:10]}"

def add_link_to_product_list(url: str) -> Tuple[bool, str]:
    minimal_product = {
        "product_url": url,
        "source": "telegram_observer",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    key = product_key(minimal_product)
    with _locked_file(LOCK_FILE):
        store = load_store(PRODUCT_LIST_PATH)
        if key in store:
            return False, "Link bereits in product_list.json"
        store[key] = minimal_product
        save_store(PRODUCT_LIST_PATH, store)
    return True, f"Link erfolgreich hinzugefügt (Key: {key})"

# ------------------------
# Channel Management
# ------------------------
async def _ensure_join_and_resolve(client: TelegramClient, ref: str):
    print(f"ℹ️ Versuche Kanal/Entität zu lösen: {ref}")
    # Wenn ein Invite-Hash vorhanden ist (z.B. t.me/+ABC...)
    invite_match = re.search(r'(?:t\.me\/joinchat\/|t\.me\/\+|invite\/)([A-Za-z0-9_-]+)', ref)
    if invite_match:
        invite_hash = invite_match.group(1)
        try:
            await client(ImportChatInviteRequest(invite_hash))
            print(f"✅ Kanal beigetreten via Invite-Hash: {invite_hash}")
        except UserAlreadyParticipantError:
            print("ℹ️ Bereits Teilnehmer des Kanals (Invite-Hash).")
        except Exception as e:
            print(f"⚠️ Invite fehlgeschlagen: {e}")

    # Versuche direkte Auflösung (z.B. t.me/Username oder @Username oder URL)
    try:
        ent = await client.get_entity(ref)
        print("✅ Entity aufgelöst (get_entity).")
        return ent
    except Exception as e1:
        print(f"⚠️ get_entity(ref) fehlgeschlagen: {e1} — versuche alternative Auflösungen.")
        # Entferne https://t.me/ Präfix falls vorhanden, versuche Username
        try:
            simple = re.sub(r'https?:\/\/t\.me\/', '', ref).strip('/')
            if simple.startswith('@'):
                simple = simple[1:]
            ent = await client.get_entity(simple)
            print(f"✅ Entity mit einfachem Namen aufgelöst: {simple}")
            return ent
        except Exception as e2:
            print(f"❌ Entität konnte nicht aufgelöst werden: {e2}")
            raise

# ------------------------
# Message Handling
# ------------------------
async def handle_message(evt: events.NewMessage.Event):
    msg = evt.message
    try:
        chat = await evt.get_chat()
        chat_name = getattr(chat, "title", None) or getattr(chat, "username", None) or "Kanal"
    except Exception:
        chat_name = "Kanal"

    text = (msg.message or "").strip()
    # Regex: HTTPS Links, schneidet trailing punctuation ab
    url_pattern = re.compile(r'(https?:\/\/(?!t\.me\/)[^\s<>"\'\)\]]+[^\s\.,;:!?\)\]\<>"\'])', re.IGNORECASE)
    found = url_pattern.findall(text)
    # findall liefert Tupel (wenn Gruppen) – wir nehmen das erste Element jeder Gruppe falls nötig
    links = []
    for f in found:
        if isinstance(f, tuple):
            links.append(f[0])
        else:
            links.append(f)

    log_preview = text.replace('\n', ' ')[:200]
    if links:
        added_count = 0
        print(f"[Observer:{chat_name}] Nachricht (Links gefunden: {len(links)}) -> {log_preview}")
        for link in set(links):
            try:
                success, reason = add_link_to_product_list(link)
                if success:
                    added_count += 1
                else:
                    # Optional: duplikat/logging
                    print(f"[Observer] {reason}: {link}")
            except Exception as e:
                print(f"[Observer] Fehler beim Hinzufügen des Links {link}: {e}")
        if added_count > 0:
            print(f"[Observer:{chat_name}] ✅ {added_count} neue Links zu {PRODUCT_LIST_PATH} hinzugefügt.")
    else:
        if log_preview:
            print(f"[Observer:{chat_name}] {log_preview}")
        else:
            print(f"[Observer:{chat_name}] [Medien/Leer]")

# ------------------------
# Main
# ------------------------
async def _amain():
    client = await ensure_logged_in(OBS_CFG)
    async with client:
        entity = await _ensure_join_and_resolve(client, OBS_CHANNEL_REF)
        print(f"🔎 Observer aktiv – überwache: {OBS_CHANNEL_REF}")

        @client.on(events.NewMessage(chats=entity))
        async def _on(evt):
            try:
                await handle_message(evt)
            except Exception as e:
                print(f"❌ (Observer) Fehler in handle_message: {e}")

        await client.run_until_disconnected()

if __name__ == "__main__":
    try:
        asyncio.run(_amain())
    except SystemExit as se:
        print(se)
        sys.exit(1)
    except Exception as e:
        print(f"❌ (Observer) Kritischer Fehler: {e}")
        sys.exit(1)
