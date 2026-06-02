# telegram/tel_video_sender.py
"""
One-shot Telegram-Video-Sender für Reels.

Sendet ein fertig gerendertes MP4 als Video-Post (Caption + Inline-Buttons)
direkt an den Haupt-Offer-Kanal (CHANNEL_INVITE_URL) — unabhängig vom
polling telRouter, damit Reels **sofort** ankommen ohne auf den 10s-Tick zu warten.

Nach dem Senden wird die product_id/ASIN in dieselbe sent-Registry geschrieben,
die telRouter nutzt, damit der polling Watcher den gleichen Deal nicht
nochmal als Foto verschickt.

Eigene Telethon-Session ("video_sender_session"), damit kein SQLite-Lock-
Konflikt mit dem parallel laufenden telRouter entsteht.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Optional, Any

from dotenv import load_dotenv
from telethon import TelegramClient, Button
from telethon.errors import UserAlreadyParticipantError
from telethon.tl.functions.messages import ImportChatInviteRequest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.logging import get_logger  # noqa: E402
log = get_logger("tel_video_sender")  # noqa: E402

from core.db import state_repo  # noqa: E402
from core.workers.telegram.login_once import LoginConfig, ensure_logged_in  # noqa: E402
from core.workers.telegram.offer_message import build_caption_html, build_inline_keyboard  # noqa: E402

load_dotenv()

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
SESSION_DIR = os.getenv("SESSION_DIR", ".sessions")
VIDEO_SENDER_SESSION = os.getenv("VIDEO_SENDER_SESSION_NAME", "video_sender_session")
PHONE = os.getenv("TELEGRAM_PHONE")
PASSWORD = os.getenv("TELEGRAM_PASSWORD")
CHANNEL_REF = (os.getenv("CHANNEL_INVITE_URL") or "").strip()
AFFILIATE_URL = os.getenv("AFFILIATE_URL", "https://amzn.to/42vWlQM")

_SENT_ASINS_KEY = "sent_asins"
_INVITE_RE = re.compile(r"(?:t\.me\/joinchat\/|t\.me\/\+|invite\/)([A-Za-z0-9_-]+)")


def _registry_load() -> dict:
    data = state_repo.get_dict(_SENT_ASINS_KEY)
    if isinstance(data, dict):
        data.setdefault("asin", [])
        data.setdefault("filehash", [])
        return data
    return {"asin": [], "filehash": []}


def _registry_save(reg: dict) -> None:
    state_repo.put(_SENT_ASINS_KEY, reg)


def _mark_sent(payload: dict) -> None:
    """Markiert den Deal als gesendet, damit telRouter ihn nicht nochmal verschickt."""
    key = payload.get("asin") or payload.get("product_id")
    if not key:
        return
    reg = _registry_load()
    if str(key) not in reg.get("asin", []):
        reg.setdefault("asin", []).append(str(key))
        _registry_save(reg)


async def _resolve_channel(client: TelegramClient, ref: str):
    m = _INVITE_RE.search(ref)
    if m:
        try:
            await client(ImportChatInviteRequest(m.group(1)))
        except UserAlreadyParticipantError:
            pass
        except Exception as e:
            log.warning(f"⚠️ Invite fehlgeschlagen: {e}")
    return await client.get_entity(ref)


async def send_reel_video(video_path: Path, payload: dict) -> bool:
    """
    Sendet ein Reel-Video sofort an den Haupt-Offer-Kanal.

    Args:
        video_path: lokaler Pfad zum fertig gerenderten MP4.
        payload: das Deal-Dict (gleicher Inhalt wie in der DB-Queue).

    Returns:
        True bei Erfolg, sonst False.
    """
    if not API_ID or not API_HASH:
        log.error("[VideoSend] API_ID/API_HASH fehlen — Telegram-Video übersprungen.")
        return False
    if not CHANNEL_REF:
        log.error("[VideoSend] CHANNEL_INVITE_URL fehlt — Telegram-Video übersprungen.")
        return False
    video_path = Path(video_path)
    if not video_path.exists():
        log.error(f"[VideoSend] Video-Datei nicht gefunden: {video_path}")
        return False

    cfg = LoginConfig(API_ID, API_HASH, VIDEO_SENDER_SESSION, SESSION_DIR, PHONE, PASSWORD)
    client = await ensure_logged_in(cfg)

    try:
        if not client.is_connected():
            await client.connect()

        entity = await _resolve_channel(client, CHANNEL_REF)

        await client.send_file(
            entity,
            str(video_path),
            supports_streaming=True,
        )
        product_id = payload.get("product_id") or payload.get("asin") or "?"
        log.info(f"[VideoSend] ✅ Reel an Telegram gesendet: {product_id} ({video_path.name})")

        _mark_sent(payload)
        return True

    except Exception as e:
        log.error(f"[VideoSend] ❌ Fehler beim Senden des Reels: {e}")
        return False
    finally:
        try:
            if client.is_connected():
                await client.disconnect()
        except Exception:
            pass
