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
        print(f"[PIRATEN] 🔗 Löse Kurzlink auf: {raw_url}")
        resolved = resolve_shortlink(raw_url)
        if resolved != raw_url:
            print(f"[PIRATEN]    → {resolved}")
        return resolved
    return raw_url

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
        print(f"[PIRATEN:{chat_name}] Links gefunden ({len(links)}): {log_preview[:120]}")
    for raw_url in links:
        try:
            # In Thread ausführen damit async loop nicht blockiert
            url = await asyncio.get_event_loop().run_in_executor(None, extract_best_url, raw_url)
            success, reason = add_link_to_product_list(url)
            if success:
                added_count += 1
                print(f"[PIRATEN] ✅ Hinzugefügt: {url}")
            else:
                print(f"[PIRATEN] ℹ️ {reason}: {url}")
        except Exception as e:
            print(f"[PIRATEN] Fehler bei {raw_url}: {e}")
    if added_count > 0:
        print(f"[PIRATEN:{chat_name}] ✅ {added_count} neue Links gespeichert.")
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
            print(f"[PIRATEN:{chat_name}] {log_preview[:120]}")
        else:
            print(f"[PIRATEN:{chat_name}] [Medien/Leer]")

# ------------------------
# Main Loop
# ------------------------
CATCHUP_MESSAGES = int(os.getenv("PIRATEN_CATCHUP", "0"))  # 0 = nur zukünftige Nachrichten verarbeiten

async def _amain():
    print(f"🏴‍☠️ Starte Piraten-Observer Session: {PIRATEN_SESSION_NAME}")
    client = await ensure_logged_in(PIRATEN_CFG)
    async with client:
        entity = await _ensure_join_and_resolve(client, PIRATEN_CHANNEL_REF)
        chat_name = getattr(entity, 'title', PIRATEN_CHANNEL_REF)
        print(f"🏴‍☠️ Überwache Kanal: {chat_name}")

        # ── Catch-Up: letzte N Nachrichten beim Start verarbeiten ──────────
        if CATCHUP_MESSAGES > 0:
            print(f"⏪ Catch-Up: verarbeite letzte {CATCHUP_MESSAGES} Nachrichten...")
            total_added = 0
            async for msg in client.iter_messages(entity, limit=CATCHUP_MESSAGES):
                links = extract_links_from_msg(msg)
                if links:
                    added = await process_message_links(links, chat_name)
                    total_added += added
            print(f"⏪ Catch-Up abgeschlossen: {total_added} neue Links hinzugefügt.\n")

        # ── Live-Listener: neue Nachrichten ───────────────────────────────
        @client.on(events.NewMessage(chats=entity))
        async def _on(evt):
            try:
                await handle_message(evt)
            except Exception as e:
                print(f"❌ Piraten-Error: {e}")

        print("🔴 Live-Listener aktiv – warte auf neue Nachrichten...")
        await client.run_until_disconnected()

if __name__ == "__main__":
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        pass
