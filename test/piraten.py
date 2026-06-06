"""
Standalone-Test für den Piraten-Telegram-Observer.

Läuft OHNE die restliche App (kein run_all, keine DB-Writes, keine Worker-Registry).
Sobald ein Angebot in einem der konfigurierten Kanäle reinkommt, wird es in der
Konsole ausgegeben – inkl. aller extrahierten Links und (sofern Kurzlink) der
finalen Amazon-URL.

Start:
    source .venv/bin/activate
    python -m test.piraten_observer_test
        (oder direkt: python test/piraten_observer_test.py)

Konfiguration (.env):
    API_ID, API_HASH, TELEGRAM_PHONE, TELEGRAM_PASSWORD
    PIRATEN_CHANNEL_INVITE_URL=https://web.telegram.org/a/#-1001609138702,https://web.telegram.org/a/#-1001559620820
    PIRATEN_SESSION_NAME=piraten_session
    SESSION_DIR=.sessions

Optional:
    PIRATEN_TEST_CATCHUP=5   # beim Start die letzten 5 Nachrichten je Kanal zeigen
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

# Projektwurzel in sys.path
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv()

# Wiederverwendete Bausteine aus dem produktiven Observer
from core.workers.telegram.telObserver_piraten import (  # noqa: E402
    PIRATEN_CHANNEL_REFS,
    PIRATEN_CHANNEL_LABELS,
    PIRATEN_CFG,
    _ensure_join_and_resolve,
    extract_links_from_msg,
    extract_best_url,
)
from core.workers.telegram.login_once import ensure_logged_in  # noqa: E402
from telethon import events  # noqa: E402


CATCHUP = int(os.getenv("PIRATEN_TEST_CATCHUP", "0"))


def _print_offer(chat_name: str, msg, resolved_links: list[str]) -> None:
    # Kompakte Ausgabe: pro Link eine Zeile mit Kanal daneben
    if not resolved_links:
        return
    for url in resolved_links:
        print(f"🔗 {url}   ←  [{chat_name}]  (msg-id={msg.id})", flush=True)


async def _process_and_print(chat_name: str, msg) -> None:
    raw_links = extract_links_from_msg(msg)
    if not raw_links:
        return  # uninteressant – keine Angebote
    loop = asyncio.get_event_loop()
    resolved: list[str] = []
    for url in raw_links:
        try:
            final = await loop.run_in_executor(None, extract_best_url, url)
        except Exception as e:
            print(f"⚠️  Fehler beim Auflösen von {url}: {e}")
            final = url
        resolved.append(final)
    _print_offer(chat_name, msg, resolved)


async def _amain() -> None:
    if not PIRATEN_CHANNEL_REFS:
        raise SystemExit("❌ Keine Kanäle konfiguriert (PIRATEN_CHANNEL_INVITE_URL leer).")

    print(f"🔐 Login mit Session: {PIRATEN_CFG.session_name}")
    client = await ensure_logged_in(PIRATEN_CFG)

    async with client:
        me = await client.get_me()
        print(f"✅ Angemeldet als: @{me.username or me.phone}")
        print(f"🏴‍☠️ Konfigurierte Kanäle ({len(PIRATEN_CHANNEL_REFS)}): {PIRATEN_CHANNEL_REFS}")

        entities = []
        names: dict[int, str] = {}
        for ref in PIRATEN_CHANNEL_REFS:
            try:
                ent = await _ensure_join_and_resolve(client, ref)
                title = (
                    PIRATEN_CHANNEL_LABELS.get(ref)
                    or getattr(ent, "title", None)
                    or getattr(ent, "username", None)
                    or str(ref)
                )
                entities.append(ent)
                names[ent.id] = title
                print(f"   ✅ {ref}  →  {title} (id={ent.id})")
            except Exception as e:
                print(f"   ❌ {ref}  →  konnte nicht aufgelöst werden: {e}")

        if not entities:
            raise SystemExit("❌ Kein Kanal konnte aufgelöst werden – Abbruch.")

        # Catch-Up (optional): zeigt die letzten N Nachrichten je Kanal
        if CATCHUP > 0:
            print(f"\n⏪ Catch-Up: letzte {CATCHUP} Nachrichten je Kanal ...")
            for ent in entities:
                async for msg in client.iter_messages(ent, limit=CATCHUP):
                    await _process_and_print(names.get(ent.id, "Kanal"), msg)

        print("\n🔴 Live-Listener aktiv – warte auf neue Nachrichten (Strg+C zum Beenden) ...\n",
              flush=True)

        @client.on(events.NewMessage(chats=entities))
        async def _on(evt: events.NewMessage.Event):
            try:
                chat = await evt.get_chat()
                chat_name = getattr(chat, "title", None) or getattr(chat, "username", None) or "Kanal"
            except Exception:
                chat_name = names.get(getattr(evt.message.peer_id, "channel_id", 0), "Kanal")
            try:
                await _process_and_print(chat_name, evt.message)
            except Exception as e:
                print(f"❌ Fehler beim Verarbeiten: {e}", flush=True)

        await client.run_until_disconnected()


if __name__ == "__main__":
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        print("\n👋 Beendet.")
