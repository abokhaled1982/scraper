# telegram/telSender.py
import os
import re
import sys
import asyncio
from pathlib import Path

# Projektwurzel in sys.path aufnehmen
PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from core.logging import get_logger  # noqa: E402
log = get_logger("telSender")  # noqa: E402

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.errors import UserAlreadyParticipantError
from telethon.tl.functions.messages import ImportChatInviteRequest

# Dein bestehender Login-Helper
from core.workers.telegram.login_once import LoginConfig, ensure_logged_in

load_dotenv()

# ------------------------
# ENV / Konfiguration
# ------------------------
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
SESSION_DIR = os.getenv("SESSION_DIR", ".sessions")
# DEDIZIERTER SENDER-NAME
OBS_SENDER_SESSION_NAME = os.getenv("OBS_SEND_OBSERVER_NAME", "observer_sender_session")
OBS_CHANNEL_REF = (os.getenv("OBS_CHANNEL_INVITE_URL") or "").strip()
PHONE = os.getenv("TELEGRAM_PHONE")
PASSWORD = os.getenv("TELEGRAM_PASSWORD")

if not API_ID or not API_HASH:
    raise SystemExit("API_ID/API_HASH fehlen in .env")
if not OBS_CHANNEL_REF:
    raise SystemExit("OBS_CHANNEL_INVITE_URL fehlt in .env")

# Dediziertes Konfigurationsobjekt
SENDER_CFG = LoginConfig(API_ID, API_HASH, OBS_SENDER_SESSION_NAME, SESSION_DIR, PHONE, PASSWORD)

# Caches
_SENDER_CLIENT = None
_ENTITY = None


async def _get_client_and_entity():
    """Stellt sicher, dass der Client existiert und die Ziel-Entity bekannt ist."""
    global _SENDER_CLIENT, _ENTITY
    
    if _SENDER_CLIENT is None:
        # Client erstellen (ensure_logged_in verbindet sich nur für den Login-Flow)
        _SENDER_CLIENT = await ensure_logged_in(SENDER_CFG)
        log.info("[Sender] 🔗 Client erstellt")
        
        # Einmalige Entity-Auflösung
        if _ENTITY is None: 
             # Muss kurz verbinden, um Entity aufzulösen (falls noch nicht passiert)
             if not _SENDER_CLIENT.is_connected():
                await _SENDER_CLIENT.connect()
             
             # Die Entity-Auflösung ist in telObserver.py definiert, 
             # wird aber hier zur Einfachheit dupliziert oder muss in login_once verschoben werden.
             # Da Sie nur die Dateien selbst korrigieren wollten, 
             # verschiebe ich _ensure_join_and_resolve nach login_once.py (siehe unten).
             
             # WICHTIG: Temporär in dieser Datei, bis login_once.py korrigiert ist.
             async def _ensure_join_and_resolve_local(client: TelegramClient, ref: str):
                 # ... (Logik von telObserver.py/_ensure_join_and_resolve hierher kopieren)
                 # Da das Skript sonst fehlschlägt, verwenden wir die Logik von telObserver.py
                 invite_match = re.search(r"(?:t\.me\/joinchat\/|t\.me\/\+|invite\/)([A-Za-z0-9_-]+)", ref)
                 if invite_match:
                     invite_hash = invite_match.group(1)
                     try:
                         await client(ImportChatInviteRequest(invite_hash))
                     except UserAlreadyParticipantError:
                         pass
                     except Exception as e:
                         log.warning(f"⚠️ (Sender) Invite fehlgeschlagen: {e}")
                 return await client.get_entity(ref)
                 
             _ENTITY = await _ensure_join_and_resolve_local(_SENDER_CLIENT, OBS_CHANNEL_REF)
             
             # Client sofort trennen, da er bei jedem Sendevorgang neu verbindet
             if _SENDER_CLIENT.is_connected():
                 await _SENDER_CLIENT.disconnect() 

    return _SENDER_CLIENT, _ENTITY


async def send_url_to_observer(url: str):
    """
    Öffentliche Schnittstelle, die in ws_server.py verwendet wird.
    Verbindet, sendet, trennt.
    """
    # Dashboard-Kill-Switch
    try:
        from core.db import config_repo as _cfg
        if not _cfg.is_enabled("telegram"):
            log.info(f"[Sender] ⏸️  Telegram deaktiviert (Dashboard) — übersprungen: {url}")
            return False
        if _cfg.is_dry_run("telegram"):
            log.info(f"[Sender] 🧪 Telegram DRY-RUN — würde senden: {url}")
            return True
    except Exception:
        pass
    try:
        sender_client, entity = await _get_client_and_entity() 
        
        # WICHTIG: Client verbinden
        if not sender_client.is_connected():
            await sender_client.connect()
            
        text = f"🛒 Neue Produkt-URL\n{url}"
        await sender_client.send_message(entity, text) 
        log.info(f"[Sender] ✅ URL an Kanal gesendet: {url}")
        
        # WICHTIG: Verbindung trennen, um die SQLite-Session sofort freizugeben.
        if sender_client.is_connected():
            await sender_client.disconnect() 
            
        return True
    except Exception as e:
        log.error(f"[Sender] ❌ Fehler beim Senden der URL: {e}")
        return False

# Optional: Main-Loop für den Fall, dass es über run_all.py ohne Argumente gestartet wird
async def _amain():
    from core.db import workers_repo
    workers_repo.register("tel_sender")
    workers_repo.set_idle("tel_sender")
    log.info("[Sender] Starte im Standby-Modus (verbindet nur bei Bedarf).")
    # Es ist kein run_until_disconnected notwendig, da der Client nur sendet.
    # Der Prozess bleibt am Leben, bis er beendet wird.
    while True:
        workers_repo.set_idle("tel_sender")
        await asyncio.sleep(60)

if __name__ == "__main__":
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        log.info("\nAbgebrochen.")
    except Exception as e:
        log.error(f"❌ (Sender) Kritischer Fehler: {e}")
        sys.exit(1)