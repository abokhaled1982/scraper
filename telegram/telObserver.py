# telegram/teleObserver.py
import os
import sys
import asyncio
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
load_dotenv()

# Projektwurzel in sys.path aufnehmen (wie in telRouter)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from telethon import events, TelegramClient
from telethon.errors import UserAlreadyParticipantError
from telethon.tl.functions.messages import ImportChatInviteRequest

# Dein bestehender Login-Helper
from telegram.login_once import LoginConfig, ensure_logged_in

# -------------------------
# ENV / Konfiguration
# -------------------------
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
SESSION_DIR = os.getenv("SESSION_DIR", ".sessions")
OBS_SESSION_NAME = os.getenv("OBS_SESSION_NAME", "observer_session")
OBS_CHANNEL_REF = (os.getenv("OBS_CHANNEL_INVITE_URL") or "").strip()
PHONE = os.getenv("TELEGRAM_PHONE")        # nur beim ersten Login nötig
PASSWORD = os.getenv("TELEGRAM_PASSWORD")  # 2FA, falls aktiv

if not API_ID or not API_HASH:
    raise SystemExit("API_ID/API_HASH fehlen in .env")
if not OBS_CHANNEL_REF:
    raise SystemExit("OBS_CHANNEL_INVITE_URL fehlt in .env (Invite-URL oder @kanalname)")

OBS_CFG = LoginConfig(
    api_id=API_ID,
    api_hash=API_HASH,
    session_name=OBS_SESSION_NAME,
    session_dir=SESSION_DIR,
    phone=PHONE,
    password=PASSWORD,
)

# -------------------------
# Helpers
# -------------------------
def _extract_invite_hash(ref: str) -> Optional[str]:
    """
    Extrahiert den Invite-Hash aus Links wie:
    - https://t.me/+XXXXXXXXXXXX
    - https://t.me/joinchat/XXXXXXXXXXXX
    """
    ref = ref.strip()
    if not ref:
        return None
    if "t.me/+" in ref:
        return ref.split("t.me/+")[-1].split("?")[0].strip("/")
    if "joinchat/" in ref:
        return ref.split("joinchat/")[-1].split("?")[0].strip("/")
    return None

async def _ensure_join_and_resolve(client: TelegramClient, ref: str):
    """
    Tritt dem Kanal via Invite bei (falls nötig) und gibt das Entity zurück.
    Bei @handle / t.me/handle reicht get_entity.
    """
    invite = _extract_invite_hash(ref)
    if invite:
        try:
            await client(ImportChatInviteRequest(invite))
            print("✅ (Observer) Kanal via Invite beigetreten.")
        except UserAlreadyParticipantError:
            pass
        except Exception as e:
            print(f"⚠️ (Observer) Invite fehlgeschlagen: {e}")
    return await client.get_entity(ref)

# -------------------------
# Message Handling
# -------------------------
async def handle_message(evt: events.NewMessage.Event):
    msg = evt.message
    # Sender/Kanal-Namen möglichst sinnvoll auflösen
    try:
        chat = await evt.get_chat()
        chat_name = getattr(chat, "title", None) or getattr(chat, "username", None) or "Kanal"
    except Exception:
        chat_name = "Kanal"

    text = (msg.message or "").strip().replace("\n", " ")
    if not text:
        if msg.media:
            text = "[Media]"
        else:
            text = "[Leer]"

    print(f"[Observer:{chat_name}] {text}")

# -------------------------
# Main
# -------------------------
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
                print(f"❌ Handler-Fehler: {e}")

        # läuft dauerhaft
        await asyncio.Future()

if __name__ == "__main__":
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        print("\nAbgebrochen.")
