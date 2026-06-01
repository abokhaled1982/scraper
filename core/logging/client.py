"""
Logger-Client für Worker.

Anwendung:
    from core.logging import get_logger
    log = get_logger("ws_server")
    log.info("server started on :8765")
    log.warning("queue is empty")
    log.error("decode error", exc_info=True)
"""
from __future__ import annotations
import logging
import logging.handlers
import sys
import socket
from typing import Dict

from core.config import LOG_HOST, LOG_PORT

# Cache, damit wir pro Worker-Name nur EINEN Logger aufbauen.
_LOGGERS: Dict[str, logging.Logger] = {}


def _make_stderr_handler() -> logging.Handler:
    """Fallback-Handler, der direkt auf stderr schreibt (klar gekennzeichnet)."""
    h = logging.StreamHandler(stream=sys.stderr)
    h.setFormatter(logging.Formatter(
        fmt="[%(asctime)s] [%(name)s] [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    ))
    return h


def _make_socket_handler() -> logging.Handler | None:
    """Versucht eine TCP-Verbindung zum Logger-Server aufzubauen."""
    try:
        with socket.create_connection((LOG_HOST, LOG_PORT), timeout=0.5):
            pass
    except OSError:
        return None
    handler = logging.handlers.SocketHandler(LOG_HOST, LOG_PORT)
    # SocketHandler picklet das LogRecord selbst – kein Formatter nötig
    return handler


def get_logger(worker: str, level: int = logging.INFO) -> logging.Logger:
    """
    Liefert einen konfigurierten Logger für den angegebenen Worker.
    Der Worker-Name landet als logger.name und wird vom Server zum
    Routen ins richtige Logfile genutzt.
    """
    if worker in _LOGGERS:
        return _LOGGERS[worker]

    log = logging.getLogger(worker)
    log.setLevel(level)
    log.propagate = False  # nicht an Root weitergeben

    # Bestehende Handler entfernen (z.B. nach Hot-Reload)
    for h in list(log.handlers):
        log.removeHandler(h)

    sock = _make_socket_handler()
    if sock is not None:
        log.addHandler(sock)
    else:
        # Fallback nur, wenn der zentrale Server nicht läuft.
        log.addHandler(_make_stderr_handler())

    _LOGGERS[worker] = log
    return log
