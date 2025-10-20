# telegram/teleObserver.py
import os
import sys
import asyncio
import re  
import json 
import hashlib 
import time 
from contextlib import contextmanager 
from pathlib import Path
from typing import Optional, Dict, Tuple, Any

from dotenv import load_dotenv
load_dotenv()

# Projektwurzel in sys.path aufnehmen
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from telethon import events, TelegramClient
from telethon.errors import UserAlreadyParticipantError
from telethon.tl.functions.messages import ImportChatInviteRequest

# Dein bestehender Login-Helper
from telegram.login_once import LoginConfig, ensure_logged_in

# Importiere zentrale Konfiguration (für Pfade zu product_list.json und .lock)
try:
    import config 
    PRODUCT_LIST_PATH = config.PRODUCT_LIST_PATH
    LOCK_FILE = config.LOCK_FILE
except ImportError:
    print("❌ Fehler: config.py konnte nicht gefunden werden. Abbruch.")
    sys.exit(1)


# ------------------------
# ENV / Konfiguration
# ------------------------
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
SESSION_DIR = os.getenv("SESSION_DIR", ".sessions")
OBS_SESSION_NAME = os.getenv("OBS_SESSION_NAME", "observer_session")
OBS_CHANNEL_REF = (os.getenv("OBS_CHANNEL_INVITE_URL") or "").strip()
PHONE = os.getenv("TELEGRAM_PHONE")        
PASSWORD = os.getenv("TELEGRAM_PASSWORD")  

if not API_ID or not API_HASH:
    raise SystemExit("API_ID/API_HASH fehlen in .env")
if not OBS_CHANNEL_REF:
    raise SystemExit("OBS_CHANNEL_INVITE_URL fehlt in .env")

OBS_CFG = LoginConfig(API_ID, API_HASH, OBS_SESSION_NAME, SESSION_DIR, PHONE, PASSWORD)


# --------------------------------------------------------------------------
# ATOMARE DATEI-HELPER (Kopiert und angepasst von parser_worker.py)
# --------------------------------------------------------------------------

# --- File Locking (POSIX preferred; no hard dep for Windows) ---
try:
    import fcntl # type: ignore
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
    # Fallback: no-op lock for non-POSIX systems (e.g., Windows)
    @contextmanager
    def _locked_file(lock_path: Path):
        yield

def load_store(path: Path) -> Dict[str, Dict]:
    """Läd die product_list.json."""
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"⚠️ Fehler beim Laden von {path} ({e}), erstelle neue leere Datenbank.")
        return {}
        
def save_store(path: Path, store: Dict[str, Dict]):
    """Speichert Store atomar über eine temporäre Datei."""
    temp_path = path.with_suffix(".tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    with temp_path.open('w', encoding='utf-8') as f:
        json.dump(store, f, ensure_ascii=False, indent=2, sort_keys=True)
    temp_path.replace(path)

def product_key(item: Dict[str, Any]) -> str:
    """
    Generiert einen stabilen Key (ASIN oder URL-Hash) für die Deduplizierung.
    Muss identisch zu dem im parser_worker.py sein!
    """
    url = item.get("product_url", "").strip().lower()
    if not url:
        return f"R-{hashlib.sha1(str(time.time()).encode()).hexdigest()[:10]}"
    
    # 1. ASIN für Amazon-Links extrahieren
    m = re.search(r'/(?:dp|gp/product|d|o)/([A-Z0-9]{10})(?:[\/?]|$)', url)
    if m:
        return f"A-{m.group(1)}"
        
    # 2. Ansonsten Hash der URL
    return f"U-{hashlib.sha1(url.encode()).hexdigest()[:10]}"


def add_link_to_product_list(url: str) -> Tuple[bool, str]:
    """Fügt einen gefundenen Link atomar der product_list.json hinzu."""
    
    # Minimales Produkt-Objekt, das product_opener verarbeiten kann
    minimal_product = {
        "product_url": url,
        "source": "telegram_observer", # Markiere die Quelle
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    
    key = product_key(minimal_product)
    
    # Atomares Update
    with _locked_file(LOCK_FILE):
        store = load_store(PRODUCT_LIST_PATH)
        
        if key in store:
            return False, "Link bereits in product_list.json"
        
        # Füge das neue Element hinzu
        store[key] = minimal_product
        save_store(PRODUCT_LIST_PATH, store)
        
    return True, f"Link erfolgreich hinzugefügt (Key: {key})"


# ------------------------
# Channel Management (Unverändert)
# ------------------------
async def _ensure_join_and_resolve(client: TelegramClient, ref: str):
    """Versucht, einem Kanal beizutreten und die Entität aufzulösen."""
    invite_match = re.search(r'(?:t\.me\/joinchat\/|t\.me\/\+|invite\/)([A-Za-z0-9_-]+)', ref)
    
    if invite_match:
        invite_hash = invite_match.group(1)
        try:
            await client(ImportChatInviteRequest(invite_hash))
            print(f"✅ (Observer) Kanal beigetreten via Invite-Hash: {invite_hash}")
        except UserAlreadyParticipantError:
            print(f"ℹ️ (Observer) Bereits im Kanal")
        except Exception as e:
            print(f"⚠️ (Observer) Invite fehlgeschlagen (Hash: {invite_hash}): {e}")
        
    try:
        return await client.get_entity(ref)
    except Exception as e:
        print(f"⚠️ (Observer) Resolve fehlgeschlagen (Ref: {ref}): {e}")
        # Wenn der Hash-Join erfolgreich war, könnte der Resolver fehlschlagen,
        # daher hier nicht direkt abbrechen.
        raise # Fehler weitergeben, falls es keine Entität gibt


# ------------------------
# Message Handling (MODIFIZIERT)
# ------------------------
async def handle_message(evt: events.NewMessage.Event):
    msg = evt.message
    try:
        chat = await evt.get_chat()
        chat_name = getattr(chat, "title", None) or getattr(chat, "username", None) or "Kanal"
    except Exception:
        chat_name = "Kanal"

    text = (msg.message or "").strip()
    
    # Regex, um nur HTTPS-Links zu finden, die nicht zu Telegram führen
    url_pattern = re.compile(
        r'(https?:\/\/(?!t\.me\/)[^\s/$.?#]+\.[^\s]*)', 
        re.IGNORECASE
    )
    links = url_pattern.findall(text)
    
    log_preview = text.replace('\n', ' ')[:100]
    
    if links:
        added_count = 0
        print(f"[Observer:{chat_name}] Nachricht (Links gefunden: {len(links)}) -> {log_preview}")
        
        for link in set(links): 
            success, reason = add_link_to_product_list(link)
            if success:
                added_count += 1
            # else: print(f"[Observer] {reason}: {link}") # Optional: Duplikat-Logging
            
        if added_count > 0:
            print(f"[Observer:{chat_name}] ✅ {added_count} neue Links zu product_list.json hinzugefügt.")
    else:
        # Standard-Log für Nachrichten ohne relevante Links
        if log_preview:
            print(f"[Observer:{chat_name}] {log_preview}")
        else:
            print(f"[Observer:{chat_name}] [Medien/Leer]")
        


# ------------------------
# Main (Unverändert)
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

        # Halte den Client am Laufen
        await client.run_until_disconnected()

if __name__ == "__main__":
    try:
        asyncio.run(_amain())
    except SystemExit:
        pass
    except Exception as e:
        print(f"❌ (Observer) Kritischer Fehler: {e}")
        sys.exit(1)