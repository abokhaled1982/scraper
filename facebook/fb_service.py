# facebook/fb_service.py
# WebSocket-Server für die Chrome Extension
# Läuft in einem eigenen Thread mit eigenem Event-Loop — ungestört vom Watcher.
#
# Port 8080  — Extension verbindet sich hier
# Heartbeat  — alle 15s ein "ping" an alle Clients
# send_post() — thread-safe, kann aus jedem Kontext aufgerufen werden

import asyncio
import base64
import json
import pathlib
import threading
import time
from typing import Set

import websockets
from websockets.server import WebSocketServerProtocol

HOST = "localhost"
PORT = 8080

_clients: Set[WebSocketServerProtocol] = set()
_server_loop: asyncio.AbstractEventLoop | None = None
_server_thread: threading.Thread | None = None


async def _handle(ws: WebSocketServerProtocol):
    _clients.add(ws)
    print(f"[FACEBOOK] ✅ Chrome Extension verbunden! ({ws.remote_address})")
    try:
        async for _ in ws:
            pass
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        _clients.discard(ws)
        print(f"[FACEBOOK] ❌ Extension getrennt ({ws.remote_address})")


async def _heartbeat():
    while True:
        await asyncio.sleep(15)
        if _clients:
            dead = set()
            for ws in list(_clients):
                try:
                    await ws.send(json.dumps({"type": "ping"}))
                except Exception:
                    dead.add(ws)
            _clients.difference_update(dead)


async def _run_server():
    async with websockets.serve(_handle, HOST, PORT, max_size=None):
        print(f"[FACEBOOK] ✅ WebSocket-Server läuft auf ws://{HOST}:{PORT}")
        asyncio.create_task(_heartbeat())
        await asyncio.Future()


def _thread_main():
    global _server_loop
    _server_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_server_loop)
    try:
        _server_loop.run_until_complete(_run_server())
    finally:
        _server_loop.close()


def init():
    """Startet den WebSocket-Server in einem eigenen Daemon-Thread."""
    global _server_thread
    if _server_thread and _server_thread.is_alive():
        return
    print(f"[FACEBOOK] 📡 Starte WebSocket-Server (eigener Thread, Port {PORT})...")
    _server_thread = threading.Thread(
        target=_thread_main,
        name="fb-ws-server",
        daemon=True,
    )
    _server_thread.start()


def ensure_connected(timeout: int = 120, check_interval: float = 2.0) -> bool:
    """
    Blockiert bis mindestens eine Chrome-Extension verbunden ist.
    Genau wie ensure_logged_in() bei Telegram.

    Returns: True wenn verbunden, False bei Timeout.
    """
    print(f"[FACEBOOK] ⏳ Warte auf Extension-Verbindung (max. {timeout}s)...")
    print(f"[FACEBOOK]    → 'addons/facebook' muss in Chrome als Erweiterung geladen sein")
    print(f"[FACEBOOK]    → facebook.com muss in einem Tab offen sein")

    elapsed = 0.0
    while elapsed < timeout:
        if _clients:
            print(f"\n[FACEBOOK] ✅ Extension verbunden! ({len(_clients)} Client(s))")
            return True
        time.sleep(check_interval)
        elapsed += check_interval
        remaining = int(timeout - elapsed)
        m, s = divmod(remaining, 60)
        print(
            f"\r[FACEBOOK]    ⏳ Warte... {m:02d}:{s:02d} verbleibend  ",
            end="", flush=True,
        )

    print()  # neue Zeile
    print(f"[FACEBOOK] ❌ Timeout: Keine Extension verbunden nach {timeout}s.")
    return False


def ensure_connected(
    timeout: int = 120,
    check_interval: float = 2.0,
    on_step=None,
) -> bool:
    """
    Blockiert bis mindestens eine Extension verbunden ist.
    Genau wie ensure_logged_in() bei Telegram.

    Args:
        timeout:        Maximale Wartezeit in Sekunden (default: 120s)
        check_interval: Prüfintervall in Sekunden
        on_step:        Optionaler Callback(str) für Statusmeldungen

    Returns:
        True wenn verbunden, False bei Timeout.
    """
    def _log(msg: str):
        print(msg)
        if on_step:
            on_step(msg)

    _log(f"[FACEBOOK] ⏳ Warte auf Extension-Verbindung (max. {timeout}s)...")
    _log(f"[FACEBOOK]    → Addon 'addons/facebook' muss in Chrome geladen sein")
    _log(f"[FACEBOOK]    → facebook.com muss in einem Tab offen sein")

    elapsed = 0.0
    while elapsed < timeout:
        if _clients:
            count = len(_clients)
            _log(f"[FACEBOOK] ✅ Extension verbunden! ({count} Client(s))")
            return True
        time.sleep(check_interval)
        elapsed += check_interval
        remaining = int(timeout - elapsed)
        m, s = divmod(remaining, 60)
        print(
            f"\r[FACEBOOK]    Warte... {m:02d}:{s:02d} verbleibend ",
            end="", flush=True,
        )

    print()  # neue Zeile nach dem \r
    _log(f"[FACEBOOK] ❌ Timeout: Keine Extension verbunden nach {timeout}s.")
    return False


def _file_to_base64(path: str | pathlib.Path) -> str | None:
    try:
        return base64.b64encode(pathlib.Path(path).read_bytes()).decode("utf-8")
    except Exception as e:
        print(f"[FACEBOOK] ❌ Fehler Base64: {e}")
        return None


async def _do_send(payload: dict) -> bool:
    if not _clients:
        print("[FACEBOOK] ⚠️ Kein Client verbunden.")
        return False
    dead = set()
    sent = 0
    for ws in list(_clients):
        try:
            await ws.send(json.dumps(payload))
            sent += 1
        except Exception:
            dead.add(ws)
    _clients.difference_update(dead)
    if sent > 0:
        print(f"[FACEBOOK] 📤 Post an {sent} Extension(s) gesendet.")
        return True
    print("[FACEBOOK] ⚠️ Kein Client erreichbar.")
    return False


async def send_post(data: dict, local_image_path: str | pathlib.Path | None = None) -> bool:
    """Sendet einen Facebook-Post — thread-safe."""
    from facebook.fb_message import create_facebook_message

    fb_text   = create_facebook_message(data)
    offer_url = str(data.get("affiliate_url") or data.get("url") or "").strip()
    if offer_url in ("N/A", "null"):
        offer_url = ""

    payload: dict = {
        "type":    "post",
        "text":    fb_text,
        "image":   None,
        "comment": offer_url,
    }

    if local_image_path:
        b64 = _file_to_base64(local_image_path)
        if b64:
            payload["image"] = b64

    if _server_loop and _server_loop.is_running():
        future = asyncio.run_coroutine_threadsafe(_do_send(payload), _server_loop)
        return future.result(timeout=30)

    print("[FACEBOOK] ❌ WS-Server-Loop nicht verfügbar.")
    return False
