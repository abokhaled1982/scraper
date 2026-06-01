# telegram/telObserver_piraten.py  (angepasst)
import os
import sys
import asyncio
import re
import json
import hashlib
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Tuple, Any
from telethon.tl.functions.channels import JoinChannelRequest

from dotenv import load_dotenv
load_dotenv()

# Projektwurzel in sys.path aufnehmen
PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from core.logging import get_logger  # noqa: E402
log = get_logger("telObserver_piraten")  # noqa: E402

# Telethon Importe
try:
    from telethon import events, TelegramClient
    from telethon.errors import UserAlreadyParticipantError
    from telethon.tl.functions.messages import ImportChatInviteRequest
except Exception as e:
    log.error("❌ Fehlende Abhängigkeit: telethon.")
    raise

# Login Helper Import (falls vorhanden)
try:
    from core.workers.telegram.login_once import LoginConfig, ensure_logged_in
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
LOCK_FILE = None
try:
    from core import paths as config
    LOCK_FILE = Path(getattr(config, "LOCK_FILE", ".locks/product_list.lock"))
except Exception:
    LOCK_FILE = Path(os.getenv("LOCK_FILE", ".locks/product_list.lock"))

from core.db import state_repo
_PRODUCT_LIST_KEY = "product_list"


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

def load_store(path: Path | None = None) -> Dict[str, Dict]:
    return state_repo.get_dict(_PRODUCT_LIST_KEY)

def save_store(path: Path | None, store: Dict[str, Dict]):
    state_repo.put(_PRODUCT_LIST_KEY, store)

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
        store = load_store()
        if key in store:
            return False, "Link bereits vorhanden"
        store[key] = minimal
        save_store(None, store)
    return True, f"Hinzugefügt (Key: {key})"

# ------------------------
# Kurzlink → Amazon-URL auflösen
# ------------------------
def resolve_shortlink(url: str, timeout: int = 8) -> str:
    """Folgt Weiterleitungen und gibt die finale URL zurück. Gibt url zurück bei Fehler."""
    try:
        import urllib.request
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None

        opener = urllib.request.build_opener(NoRedirect())
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}, method="HEAD")
        try:
            opener.open(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            loc = e.headers.get("Location")
            if loc:
                # Relative URLs auflösen
                if loc.startswith("/"):
                    from urllib.parse import urlparse
                    p = urlparse(url)
                    loc = f"{p.scheme}://{p.netloc}{loc}"
                return loc
        # Fallback GET für Server die kein HEAD unterstützen
        req2 = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        response = urllib.request.urlopen(req2, timeout=timeout)
        final = response.geturl()
        if final and final != url:
            return final
    except Exception:
        pass
    return url

def extract_best_url(raw_url: str) -> str:
    """Löst Kurzlinks auf und gibt Amazon-URL zurück falls möglich."""
    # Wenn bereits Amazon → direkt zurück
    if re.search(r'amazon\.(de|com|co\.uk|fr|it|es)', raw_url, re.I):
        return raw_url
    # Bekannte Kurzlink-Domains → auflösen
    if re.search(r'(pirat\.deals|amzn\.to|amzn\.eu|t\.co|bit\.ly|tinyurl)', raw_url, re.I):
        log.info(f"[PIRATEN] 🔗 Löse Kurzlink auf: {raw_url}")
        resolved = resolve_shortlink(raw_url)
        if resolved != raw_url:
            log.info(f"[PIRATEN]    → {resolved}")
        return resolved
    return raw_url

# ------------------------
# Channel Management (robust)
# ------------------------
async def _ensure_join_and_resolve(client: TelegramClient, ref: str):
    log.info(f"ℹ️ Versuche Kanal/Entität zu lösen: {ref}")
    
    entity = None
    
    # 1. Versuch: Ist es ein Private Invite Link (t.me/+)
    invite_match = re.search(r'(?:t\.me\/joinchat\/|t\.me\/\+|invite\/)([A-Za-z0-9_-]+)', ref)
    if invite_match:
        invite_hash = invite_match.group(1)
        try:
            await client(ImportChatInviteRequest(invite_hash))
            log.info(f"✅ Kanal beigetreten via Invite-Hash: {invite_hash}")
        except UserAlreadyParticipantError:
            pass # Alles gut, schon drin
        except Exception as e:
            log.warning(f"⚠️ Invite via Hash fehlgeschlagen: {e}")

    # 2. Versuch: Öffentlicher Username / URL (z.B. t.me/PirateDeals)
    # Wir säubern den Link, um nur den Usernamen zu bekommen
    clean_ref = re.sub(r'https?:\/\/t\.me\/', '', ref).strip('/ ')
    if clean_ref.startswith('@'):
        clean_ref = clean_ref[1:]

    try:
        # Erst versuchen wir, die Entität zu finden
        entity = await client.get_entity(clean_ref)
        log.info(f"✅ Entity gefunden: {getattr(entity, 'title', clean_ref)}")
        
        # WICHTIG: Jetzt explizit beitreten, falls es ein öffentlicher Kanal ist
        # Bei privaten Chats würde das fehlschlagen, daher try/except
        try:
            await client(JoinChannelRequest(entity))
            log.info("✅ Erfolgreich dem öffentlichen Kanal beigetreten (oder war bereits drin).")
        except UserAlreadyParticipantError:
            pass
        except Exception as e_join:
            # Manche Entities (z.B. Chats) erlauben kein JoinChannelRequest, das ist okay
            log.info(f"ℹ️ Kein expliziter Join nötig oder möglich: {e_join}")
            
        return entity

    except Exception as e:
        log.error(f"❌ Kritischer Fehler: Konnte {ref} nicht auflösen oder beitreten.")
        log.error(f"   Fehler: {e}")
        raise
# ------------------------
# Message Handling (wie telObserver)
# ------------------------
def extract_links_from_msg(msg) -> list:
    """Extrahiert alle URLs aus Text, Entity-URLs und Inline-Button-URLs."""
    text = (msg.message or "").strip()
    url_pattern = re.compile(
        r'(https?://(?!t\.me/)[^\s<>"\'\)\]]+[^\s\.,;:!?\)\]\<>"\'])',
        re.IGNORECASE
    )
    links = list({f for f in url_pattern.findall(text)})

    # Entity-URLs (Text hinter "Zum Angebot" etc.)
    if msg.entities:
        for ent in msg.entities:
            url = getattr(ent, 'url', None)
            if url and not re.match(r'https?://t\.me/', url, re.I):
                if url not in links:
                    links.append(url)

    # Inline-Keyboard-Button-URLs (pirat.deals nutzt oft Buttons statt Text-Links)
    if msg.reply_markup:
        try:
            for row in msg.reply_markup.rows:
                for button in row.buttons:
                    url = getattr(button, 'url', None)
                    if url and not re.match(r'https?://t\.me/', url, re.I):
                        if url not in links:
                            links.append(url)
        except Exception:
            pass

    return links

async def process_message_links(links: list, chat_name: str, log_preview: str = ""):
    """Löst Kurzlinks auf und trägt sie in product_list.json ein."""
    added_count = 0
    if log_preview:
        log.info(f"[PIRATEN:{chat_name}] Links gefunden ({len(links)}): {log_preview[:120]}")
    for raw_url in links:
        try:
            # In Thread ausführen damit async loop nicht blockiert
            url = await asyncio.get_event_loop().run_in_executor(None, extract_best_url, raw_url)
            success, reason = add_link_to_product_list(url)
            if success:
                added_count += 1
                log.info(f"[PIRATEN] ✅ Hinzugefügt: {url}")
            else:
                log.info(f"[PIRATEN] ℹ️ {reason}: {url}")
        except Exception as e:
            log.error(f"[PIRATEN] Fehler bei {raw_url}: {e}")
    if added_count > 0:
        log.info(f"[PIRATEN:{chat_name}] ✅ {added_count} neue Links gespeichert.")
    return added_count

async def handle_message(evt: events.NewMessage.Event):
    msg = evt.message
    try:
        chat = await evt.get_chat()
        chat_name = getattr(chat, "title", None) or getattr(chat, "username", None) or "Kanal"
    except Exception:
        chat_name = "Kanal"

    text = (msg.message or "").strip()
    log_preview = text.replace('\n', ' ')
    links = extract_links_from_msg(msg)

    if links:
        await process_message_links(links, chat_name, log_preview)
    else:
        if log_preview:
            log.info(f"[PIRATEN:{chat_name}] {log_preview[:120]}")
        else:
            log.info(f"[PIRATEN:{chat_name}] [Medien/Leer]")

# ------------------------
# Main Loop
# ------------------------
CATCHUP_MESSAGES = int(os.getenv("PIRATEN_CATCHUP", "0"))  # 0 = nur zukünftige Nachrichten verarbeiten

async def _amain():
    from core.db import workers_repo
    _WORKER = "tel_observer_piraten"
    workers_repo.register(_WORKER)
    log.info(f"🏴‍☠️ Starte Piraten-Observer Session: {PIRATEN_SESSION_NAME}")
    client = await ensure_logged_in(PIRATEN_CFG)
    async with client:
        entity = await _ensure_join_and_resolve(client, PIRATEN_CHANNEL_REF)
        chat_name = getattr(entity, 'title', PIRATEN_CHANNEL_REF)
        log.info(f"🏴‍☠️ Überwache Kanal: {chat_name}")
        workers_repo.set_task(_WORKER, f"watching {chat_name}")

        # ── Catch-Up: letzte N Nachrichten beim Start verarbeiten ──────────
        if CATCHUP_MESSAGES > 0:
            log.info(f"⏪ Catch-Up: verarbeite letzte {CATCHUP_MESSAGES} Nachrichten...")
            total_added = 0
            async for msg in client.iter_messages(entity, limit=CATCHUP_MESSAGES):
                links = extract_links_from_msg(msg)
                if links:
                    added = await process_message_links(links, chat_name)
                    total_added += added
            log.info(f"⏪ Catch-Up abgeschlossen: {total_added} neue Links hinzugefügt.\n")

        # ── Live-Listener: neue Nachrichten ───────────────────────────────
        @client.on(events.NewMessage(chats=entity))
        async def _on(evt):
            try:
                await handle_message(evt)
                workers_repo.set_task(_WORKER, "processed message")
            except Exception as e:
                workers_repo.set_error(_WORKER, str(e)[:200])
                log.error(f"❌ Piraten-Error: {e}")

        log.info("🔴 Live-Listener aktiv – warte auf neue Nachrichten...")
        await client.run_until_disconnected()

if __name__ == "__main__":
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        pass
