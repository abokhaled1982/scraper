"""
Logger-Server — eigenständiger Prozess.

Empfängt LogRecords per TCP (Standard Python SocketHandler-Protokoll),
schreibt sie:
  1) farbig in stdout (info=cyan, warn=gelb, error=rot)
  2) in .log/<DATUM>/<worker>.log (eine Datei pro Worker und Tag)

Start als Modul:
    python -m core.logging.server
"""
from __future__ import annotations
import logging
import logging.handlers
import pickle
import socketserver
import struct
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict

from colorlog import ColoredFormatter

from core.config import LOG_HOST, LOG_PORT, LOG_DIR


# ───────────────────────────────────────────────────────────────
# Konsolen-Formatter (farbig)
# ───────────────────────────────────────────────────────────────
_CONSOLE_FORMATTER = ColoredFormatter(
    fmt=(
        "%(log_color)s%(asctime)s%(reset)s "
        "%(bold_blue)s%(name)-18s%(reset)s "
        "%(log_color)s%(levelname)-7s%(reset)s "
        "%(message_log_color)s%(message)s"
    ),
    datefmt="%H:%M:%S",
    log_colors={
        "DEBUG":    "white",
        "INFO":     "cyan",
        "WARNING":  "yellow",
        "ERROR":    "red",
        "CRITICAL": "bold_red,bg_white",
    },
    secondary_log_colors={
        "message": {
            "DEBUG":    "white",
            "INFO":     "white",
            "WARNING":  "yellow",
            "ERROR":    "red",
            "CRITICAL": "bold_red",
        }
    },
    reset=True,
)

_FILE_FORMATTER = logging.Formatter(
    fmt="%(asctime)s %(name)-18s %(levelname)-7s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


# ───────────────────────────────────────────────────────────────
# Dispatcher: pro Worker einen FileHandler + ein gemeinsamer Konsolen-Handler.
# Tagesgrenze: wenn das Datum wechselt, neues Verzeichnis nutzen.
# ───────────────────────────────────────────────────────────────
class _Dispatcher:
    def __init__(self) -> None:
        self._console_handler = logging.StreamHandler(stream=sys.stdout)
        self._console_handler.setFormatter(_CONSOLE_FORMATTER)

        self._file_handlers: Dict[str, logging.Handler] = {}
        self._current_day: str = ""

    def _today(self) -> str:
        return datetime.now().strftime("%Y-%m-%d")

    def _ensure_day_dir(self) -> Path:
        today = self._today()
        if today != self._current_day:
            # Tageswechsel: alle FileHandler schließen, damit neue Datei genutzt wird
            for h in self._file_handlers.values():
                try:
                    h.close()
                except Exception:
                    pass
            self._file_handlers.clear()
            self._current_day = today
        day_dir = LOG_DIR / today
        day_dir.mkdir(parents=True, exist_ok=True)
        return day_dir

    def _file_handler_for(self, worker: str) -> logging.Handler:
        day_dir = self._ensure_day_dir()
        if worker in self._file_handlers:
            return self._file_handlers[worker]
        path = day_dir / f"{worker}.log"
        h = logging.FileHandler(path, encoding="utf-8")
        h.setFormatter(_FILE_FORMATTER)
        self._file_handlers[worker] = h
        return h

    def dispatch(self, record: logging.LogRecord) -> None:
        # Konsole: immer
        self._console_handler.handle(record)
        # Datei: pro Worker (record.name == worker-name)
        worker = (record.name or "unknown").replace("/", "_")
        try:
            self._file_handler_for(worker).handle(record)
        except Exception as e:
            sys.stderr.write(f"[logger-server] file write failed: {e}\n")


# ───────────────────────────────────────────────────────────────
# TCP-Handler (Standard-Python-Protokoll: 4-Byte length + pickled record dict)
# ───────────────────────────────────────────────────────────────
class _LogHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        while True:
            chunk = self.connection.recv(4)
            if len(chunk) < 4:
                break
            slen = struct.unpack(">L", chunk)[0]
            chunk = self.connection.recv(slen)
            while len(chunk) < slen:
                more = self.connection.recv(slen - len(chunk))
                if not more:
                    break
                chunk += more
            try:
                obj = pickle.loads(chunk)
                record = logging.makeLogRecord(obj)
            except Exception as e:
                sys.stderr.write(f"[logger-server] bad record: {e}\n")
                continue
            self.server.dispatcher.dispatch(record)  # type: ignore[attr-defined]


class _LogServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, addr):
        super().__init__(addr, _LogHandler)
        self.dispatcher = _Dispatcher()


def main() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    server = _LogServer((LOG_HOST, LOG_PORT))
    boot_msg = (
        f"\033[36m[logger-server] listening on {LOG_HOST}:{LOG_PORT} "
        f"→ files: {LOG_DIR}/<DATE>/<worker>.log\033[0m"
    )
    print(boot_msg, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[logger-server] shutting down")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
