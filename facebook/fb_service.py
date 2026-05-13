# facebook/fb_service.py
# WebSocket-Server für die Chrome Extension
# Port 8080  — Extension verbindet sich hier
# Handshake  — Addon sendet handshake, Server antwortet mit handshake_ack
# Heartbeat  — alle 15s ein "ping" an alle Clients, Clients antworten mit "pong"

import asyncio
import base64
import json
import logging
import pathlib
import threading
import time
from typing import Set

import websockets
from websockets.server import WebSocketServerProtocol

HOST = "localhost"
PORT = 8080

_clients: Set[WebSocketServerProtocol]       = set()
_ready_clients: Set[WebSocketServerProtocol] = set()  # Handshake completed
_server_loop: asyncio.AbstractEventLoop | None = None
_server_thread: threading.Thread | None        = None

# ── Pending-Task-Result (one post at a time) ───────────────────────────────
_pending_result_event: asyncio.Event | None = None
_pending_result_data:  dict | None          = None

# ── Logger ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [WS] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("fb_service")


async def _handle(ws: WebSocketServerProtocol):
    addr = ws.remote_address
    _clients.add(ws)
    logger.info(f"🔌 Extension verbunden: {addr}")

    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except Exception:
                logger.warning(f"Ungültige Nachricht von {addr}: {raw[:80]}")
                continue

            mtype = msg.get("type", "")

            if mtype == "handshake":
                version = msg.get("version", "?")
                client  = msg.get("client", "unknown")
                logger.info(f"🤝 Handshake von {addr} – client={client} version={version}")
                await ws.send(json.dumps({
                    "type":   "handshake_ack",
                    "server": "fb-service",
                    "status": "ready",
                }))
                _ready_clients.add(ws)
                logger.info(f"✅ Handshake abgeschlossen mit {addr}")

            elif mtype == "pong":
                pass  # heartbeat response, ignore

            elif mtype == "addon_status":
                state    = msg.get("state", "?")
                activity = msg.get("activity", "")
                logger.info(f"📊 Addon: {state} – {activity}")

            elif mtype == "busy":
                reason = msg.get("reason", "")
                queued = msg.get("queued", 0)
                logger.warning(f"⏳ Addon busy: {reason} ({queued} in Queue)")

            elif mtype == "task_result":
                global _pending_result_event, _pending_result_data
                success = msg.get("success", False)
                error   = msg.get("error", "–")
                logger.info(f"📋 Task-Ergebnis: success={success} error={error}")
                if _pending_result_event and not _pending_result_event.is_set():
                    _pending_result_data = msg
                    _pending_result_event.set()

            else:
                logger.debug(f"Unbekannte Nachricht von {addr}: {mtype}")

    except websockets.exceptions.ConnectionClosed as e:
        logger.warning(f"❌ Verbindung getrennt: {addr} – {e}")
    finally:
        _clients.discard(ws)
        _ready_clients.discard(ws)
        logger.info(f"🔌 Extension weg: {addr} | Verbleibend: {len(_clients)}")


async def _heartbeat():
    while True:
        await asyncio.sleep(15)
        if not _clients:
            continue
        dead = set()
        for ws in list(_clients):
            try:
                await ws.send(json.dumps({"type": "ping"}))
            except Exception:
                dead.add(ws)
        if dead:
            logger.warning(f"💀 {len(dead)} tote Client(s) entfernt.")
            _clients.difference_update(dead)
            _ready_clients.difference_update(dead)


async def _run_server():
    async with websockets.serve(_handle, HOST, PORT, max_size=None):
        logger.info(f"✅ WebSocket-Server läuft auf ws://{HOST}:{PORT}")
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
    logger.info(f"📡 Starte WebSocket-Server (Port {PORT})...")
    _server_thread = threading.Thread(target=_thread_main, name="fb-ws-server", daemon=True)
    _server_thread.start()


def is_server_running() -> bool:
    """Prüft ob der WebSocket-Server bereits läuft."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex((HOST, PORT)) == 0


def ensure_connected(timeout: int = 120, check_interval: float = 2.0, on_step=None) -> bool:
    """Blockiert bis mindestens eine Extension verbunden ist (Handshake bevorzugt, Fallback auf einfache Verbindung)."""
    def _log(msg: str):
        logger.info(msg)
        if on_step:
            on_step(msg)

    _log(f"⏳ Warte auf Extension-Verbindung + Handshake (max. {timeout}s)...")

    elapsed = 0.0
    while elapsed < timeout:
        if _ready_clients:
            _log(f"✅ Handshake OK! ({len(_ready_clients)} Client(s))")
            return True
        # Fallback: normaler Client da aber Handshake noch nicht abgeschlossen
        if _clients and elapsed > 10:
            _log(f"⚠️ Client verbunden aber kein Handshake nach {int(elapsed)}s – trotzdem fortfahren.")
            return True
        time.sleep(check_interval)
        elapsed += check_interval
        remaining = int(timeout - elapsed)
        m, s = divmod(remaining, 60)
        print(f"\r[WS] ⏳ Warte auf Handshake... {m:02d}:{s:02d} verbleibend  ", end="", flush=True)

    print()
    _log(f"❌ Timeout: Kein Handshake nach {timeout}s.")
    return False


def _file_to_base64(path: str | pathlib.Path) -> str | None:
    try:
        return base64.b64encode(pathlib.Path(path).read_bytes()).decode("utf-8")
    except Exception as e:
        logger.error(f"Base64-Fehler: {e}")
        return None


async def _do_send(payload: dict) -> bool:
    targets = _ready_clients if _ready_clients else _clients
    if not targets:
        logger.warning("⚠️ Kein verbundener Client.")
        return False
    dead = set()
    sent = 0
    for ws in list(targets):
        try:
            await ws.send(json.dumps(payload))
            sent += 1
        except Exception as e:
            logger.warning(f"Senden fehlgeschlagen: {e}")
            dead.add(ws)
    _clients.difference_update(dead)
    _ready_clients.difference_update(dead)
    if sent > 0:
        ptype = payload.get("type", "payload")
        logger.info(f"📤 {ptype} gesendet an {sent} Extension(s).")
        return True
    logger.warning("⚠️ Kein Client erreichbar.")
    return False


def _build_comment_text(data: dict, offer_url: str) -> str:
    """Baut den Kommentar-Text mit Affiliate-Link und optionalem Gutscheincode."""
    parts = []
    if offer_url:
        parts.append(f"🔗 Zum Angebot: {offer_url}")

    # Coupon aus verschachteltem Dict oder flachem Feld lesen
    coupon_raw = data.get("coupon") or {}
    if isinstance(coupon_raw, dict):
        coupon = str(coupon_raw.get("code") or "").strip()
    else:
        coupon = str(coupon_raw or "").strip()
    if not coupon:
        coupon = str(data.get("coupon_code") or data.get("voucher_code") or "").strip()

    if coupon and coupon.lower() not in ("n/a", "null", "none", ""):
        parts.append(f"🏷️ Gutscheincode: {coupon}")
    return "\n".join(parts) if parts else ""


async def send_post(data: dict, local_image_path=None, local_video_path=None) -> bool:
    """Sendet einen Facebook-Post oder Reel."""
    from facebook.fb_message import create_facebook_message

    fb_text   = create_facebook_message(data)
    offer_url = str(data.get("affiliate_url") or data.get("url") or "").strip()
    if offer_url in ("N/A", "null"):
        offer_url = ""

    comment_text = _build_comment_text(data, offer_url)

    payload = {
        "type":    "remote_post",
        "text":    fb_text,
        "image":   None,
        "video":   None,
        "comment": comment_text,
    }

    if local_image_path:
        b64 = _file_to_base64(local_image_path)
        if b64:
            payload["image"] = b64
        logger.info(f"🖼️ Bild geladen ({len(b64 or '') // 1024} KB)")

    if local_video_path:
        b64 = _file_to_base64(local_video_path)
        if b64:
            payload["video"] = b64
        logger.info(f"🎥 Video geladen ({len(b64 or '') // 1024} KB)")

    if _server_loop and _server_loop.is_running():
        # Reels brauchen deutlich länger — 30 min für Videos, 10 min für Posts
        wait_timeout = 1800.0 if local_video_path else 600.0
        future = asyncio.run_coroutine_threadsafe(
            _do_send_and_wait(payload, timeout=wait_timeout), _server_loop
        )
        try:
            return future.result(timeout=wait_timeout + 30)
        except Exception as e:
            logger.error(f"send_post Fehler: {e}")
            return False

    logger.error("WS-Server-Loop nicht verfügbar.")
    return False


async def _do_send_and_wait(payload: dict, timeout: float = 180.0) -> bool:
    """Sendet Payload an Addon und wartet auf tatsächliche Fertig-Bestätigung."""
    global _pending_result_event, _pending_result_data
    _pending_result_event = asyncio.Event()
    _pending_result_data  = None
    try:
        sent = await _do_send(payload)
        if not sent:
            return False
        ptype = payload.get("type", "post")
        logger.info(f"⏳ Warte auf Addon-Bestätigung für {ptype} (max. {int(timeout)}s)...")
        await asyncio.wait_for(_pending_result_event.wait(), timeout=timeout)
        success = bool((_pending_result_data or {}).get("success", False))
        if success:
            logger.info("✅ Addon-Bestätigung: Post/Reel erfolgreich gepostet.")
        else:
            err = (_pending_result_data or {}).get("error", "unbekannt")
            logger.warning(f"⚠️ Addon-Bestätigung: Fehler – {err}")
        return success
    except asyncio.TimeoutError:
        logger.error(f"⏰ Timeout: Kein Ergebnis vom Addon nach {int(timeout)}s")
        return False
    finally:
        _pending_result_event = None
        _pending_result_data  = None

