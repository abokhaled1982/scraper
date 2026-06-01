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
PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from core.logging import get_logger  # noqa: E402
log = get_logger("telObserver")  # noqa: E402

# Telethon
try:
    from telethon import events, TelegramClient
    from telethon.errors import UserAlreadyParticipantError
    from telethon.tl.functions.messages import ImportChatInviteRequest
except Exception as e:
    log.error("❌ Fehlende Abhängigkeit: telethon. Bitte installieren: pip install telethon")
    raise

# Dein bestehender Login-Helper (falls vorhanden)
try:
    from core.workers.telegram.login_once import LoginConfig, ensure_logged_in
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
LOCK_FILE = None
try:
    from core import paths as config
    LOCK_FILE = Path(getattr(config, "LOCK_FILE", ".locks/product_list.lock"))
except Exception:
    LOCK_FILE = Path(os.getenv("LOCK_FILE", ".locks/product_list.lock"))

from core.db import state_repo
_PRODUCT_LIST_KEY = "product_list"

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

def load_store(path: Path | None = None) -> Dict[str, Dict]:
    return state_repo.get_dict(_PRODUCT_LIST_KEY)

def save_store(path: Path | None, store: Dict[str, Dict]):
    state_repo.put(_PRODUCT_LIST_KEY, store)

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
        store = load_store()
        if key in store:
            return False, "Link bereits in product_list (DB)"
        store[key] = minimal_product
        save_store(None, store)
    return True, f"Link erfolgreich hinzugefügt (Key: {key})"

# ------------------------
# Channel Management
# ------------------------
async def _ensure_join_and_resolve(client: TelegramClient, ref: str):
    log.info(f"ℹ️ Versuche Kanal/Entität zu lösen: {ref}")
    # Wenn ein Invite-Hash vorhanden ist (z.B. t.me/+ABC...)
    invite_match = re.search(r'(?:t\.me\/joinchat\/|t\.me\/\+|invite\/)([A-Za-z0-9_-]+)', ref)
    if invite_match:
        invite_hash = invite_match.group(1)
        try:
            await client(ImportChatInviteRequest(invite_hash))
            log.info(f"✅ Kanal beigetreten via Invite-Hash: {invite_hash}")
        except UserAlreadyParticipantError:
            log.info("ℹ️ Bereits Teilnehmer des Kanals (Invite-Hash).")
        except Exception as e:
            log.warning(f"⚠️ Invite fehlgeschlagen: {e}")

    # Versuche direkte Auflösung (z.B. t.me/Username oder @Username oder URL)
    try:
        ent = await client.get_entity(ref)
        log.info("✅ Entity aufgelöst (get_entity).")
        return ent
    except Exception as e1:
        log.warning(f"⚠️ get_entity(ref) fehlgeschlagen: {e1} — versuche alternative Auflösungen.")
        # Entferne https://t.me/ Präfix falls vorhanden, versuche Username
        try:
            simple = re.sub(r'https?:\/\/t\.me\/', '', ref).strip('/')
            if simple.startswith('@'):
                simple = simple[1:]
            ent = await client.get_entity(simple)
            log.info(f"✅ Entity mit einfachem Namen aufgelöst: {simple}")
            return ent
        except Exception as e2:
            log.error(f"❌ Entität konnte nicht aufgelöst werden: {e2}")
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
        log.info(f"[Observer:{chat_name}] Nachricht (Links gefunden: {len(links)}) -> {log_preview}")
        for link in set(links):
            try:
                success, reason = add_link_to_product_list(link)
                if success:
                    added_count += 1
                else:
                    # Optional: duplikat/logging
                    log.info(f"[Observer] {reason}: {link}")
            except Exception as e:
                log.error(f"[Observer] Fehler beim Hinzufügen des Links {link}: {e}")
        if added_count > 0:
            log.info(f"[Observer:{chat_name}] ✅ {added_count} neue Links zur DB (product_list) hinzugefügt.")
    else:
        if log_preview:
            log.info(f"[Observer:{chat_name}] {log_preview}")
        else:
            log.info(f"[Observer:{chat_name}] [Medien/Leer]")

# ------------------------
# Main
# ------------------------
async def _amain():
    from core.db import workers_repo
    _WORKER = "tel_observer"
    workers_repo.register(_WORKER)
    client = await ensure_logged_in(OBS_CFG)
    async with client:
        entity = await _ensure_join_and_resolve(client, OBS_CHANNEL_REF)
        log.info(f"🔎 Observer aktiv – überwache: {OBS_CHANNEL_REF}")
        workers_repo.set_task(_WORKER, f"watching {OBS_CHANNEL_REF}")

        @client.on(events.NewMessage(chats=entity))
        async def _on(evt):
            try:
                await handle_message(evt)
                workers_repo.set_task(_WORKER, "processed message")
            except Exception as e:
                workers_repo.set_error(_WORKER, str(e)[:200])
                log.error(f"❌ (Observer) Fehler in handle_message: {e}")

        await client.run_until_disconnected()

if __name__ == "__main__":
    try:
        asyncio.run(_amain())
    except SystemExit as se:
        log.info(se)
        sys.exit(1)
    except Exception as e:
        log.error(f"❌ (Observer) Kritischer Fehler: {e}")
        sys.exit(1)
