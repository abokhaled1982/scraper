"""
Zentrale Konfiguration für core-Infrastruktur.
Liest aus Environment-Variablen, damit das später containerisierbar ist.
"""
from __future__ import annotations
import os
from pathlib import Path

# Projekt-Root (zwei Ebenen über dieser Datei)
BASE_DIR = Path(__file__).resolve().parent.parent

# ───────────────────────────────────────────────────────────────
# Datenbank
# ───────────────────────────────────────────────────────────────
# Format wie SQLAlchemy es erwartet. Default: SQLite-Datei unter .db/.
DB_DIR = Path(os.getenv("CORE_DB_DIR", str(BASE_DIR / ".db"))).resolve()
DB_DIR.mkdir(parents=True, exist_ok=True)
DB_URL = os.getenv(
    "CORE_DB_URL",
    f"sqlite:///{DB_DIR / 'core_data.db'}",
)

# ───────────────────────────────────────────────────────────────
# Logger-Service (TCP)
# ───────────────────────────────────────────────────────────────
LOG_HOST = os.getenv("CORE_LOG_HOST", "127.0.0.1")
LOG_PORT = int(os.getenv("CORE_LOG_PORT", "9020"))

# Verzeichnis für tagesrotierte Logfiles: .log/JJJJ-MM-TT/<worker>.log
LOG_DIR = Path(os.getenv("CORE_LOG_DIR", str(BASE_DIR / ".log"))).resolve()

# ───────────────────────────────────────────────────────────────
# Web-Dashboard
# ───────────────────────────────────────────────────────────────
DASHBOARD_HOST = os.getenv("CORE_DASHBOARD_HOST", "127.0.0.1")
DASHBOARD_PORT = int(os.getenv("CORE_DASHBOARD_PORT", "8000"))

# Heartbeat-Schwelle in Sekunden – darüber gilt ein Worker als „stale".
WORKER_STALE_AFTER_SECS = int(os.getenv("CORE_WORKER_STALE_SECS", "60"))

# ───────────────────────────────────────────────────────────────
# WebSocket-Server (Browser-Addon → Amazon-Parser)
# ───────────────────────────────────────────────────────────────
WS_HOST = os.getenv("CORE_WS_HOST", "127.0.0.1")
WS_PORT = int(os.getenv("CORE_WS_PORT", "8765"))

# ───────────────────────────────────────────────────────────────
# Worker-Intervalle (Sekunden)
# ───────────────────────────────────────────────────────────────
WATCH_INTERVAL_SECS = float(os.getenv("CORE_WATCH_INTERVAL_SECS", "10"))
INTERVAL_SECS       = int(os.getenv("CORE_INTERVAL_SECS", "13"))
