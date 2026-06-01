# core/paths.py — nur noch Pfade für nicht-DB-Artefakte (Media, HTML-Cache, Lock).
#
# Ehemalige Pfade DEALS_QUEUE_DIR / DEALS_SENT_DIR / DEALS_FAILED_DIR / SENT_IDS_PATH /
# SENT_ASINS_PATH / PRODUCT_LIST_PATH / OPENED_PATH / REGISTRY_PATH / SUMMARY_PATH
# sind **entfernt** — diese Daten leben jetzt in der SQLite-DB (siehe core/db/).
#
# Migration: `python -m core.migrate` importiert noch vorhandene JSON-Dateien.
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = (BASE_DIR / "data").resolve()

# ---------------------------------------------------------------------------
# 🖼️ Media — Bilder und Videos (ASIN.jpg / ASIN.mp4)
# ---------------------------------------------------------------------------
MEDIA_DIR        = DATA_DIR / "media"
IMAGES_DIR       = MEDIA_DIR / "images"
VIDEOS_DIR       = MEDIA_DIR / "videos"
VIDEOS_QUEUE_DIR = VIDEOS_DIR / "queue"
VIDEOS_SENT_DIR  = VIDEOS_DIR / "sent"

# ---------------------------------------------------------------------------
# 📥 Inbox — rohe HTML-Eingaben + Cache
# ---------------------------------------------------------------------------
INBOX_DIR    = DATA_DIR / "inbox"
PRODUCKT_DIR = DATA_DIR / "produckt"   # HTML-Cache Amazon

# ---------------------------------------------------------------------------
# 🔒 Lock-Files (Datei-basiert für plattformneutrale Locks)
# ---------------------------------------------------------------------------
LOCK_FILE = DATA_DIR / "product_list.lock"

# ---------------------------------------------------------------------------
# 🌐 WebSocket-Server (Browser → Amazon-Parser)
# ---------------------------------------------------------------------------
WS_HOST = "127.0.0.1"
WS_PORT = 8765

# ---------------------------------------------------------------------------
# ⏱️ Worker-Intervalle
# ---------------------------------------------------------------------------
WATCH_INTERVAL_SECS = 10.0
INTERVAL_SECS = 13


def ensure_directories() -> None:
    """Erzeugt nur noch Media-/Inbox-Verzeichnisse — Deals/State leben in der DB."""
    for p in [IMAGES_DIR, VIDEOS_QUEUE_DIR, VIDEOS_SENT_DIR, INBOX_DIR, PRODUCKT_DIR]:
        p.mkdir(parents=True, exist_ok=True)
