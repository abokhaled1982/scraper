"""
core.logging — Zentraler Logging-Service.

Konzept:
  * Jeder Worker holt sich einen Logger via `get_logger("ws_server")`.
  * Der Logger schickt Datensätze per TCP (SocketHandler) an den
    Logger-Server (core.logging.server) – falls erreichbar.
  * Lokaler Fallback: Wenn der Server nicht läuft, geht der Output auf stderr,
    damit nichts verloren geht.

Öffentliche API:
    from core.logging import get_logger
    log = get_logger("parser")
    log.info("...")
    log.warning("...")
    log.error("...")
"""
from .client import get_logger

__all__ = ["get_logger"]
