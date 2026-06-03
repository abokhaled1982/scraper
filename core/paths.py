# core/paths.py — NUR Pfade für Nicht-DB-Artefakte (Media + Amazon-HTML-Cache + Lock).
#
# Layout im Projekt-Root:
#   .db/     → SQLite (core_data.db* + backups/)  ← core.config.DB_URL
#   media/   → Bilder + Videos
#   cache/   → flüchtige Amazon-HTML-Dateien + Lock
#
# Deals/State leben in der SQLite-DB (siehe core/db/) — keine JSON-Files mehr.
# Hosts/Ports/Intervalle wohnen in core.config.
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# 🖼️ Media — Bilder und Videos (ASIN.jpg / ASIN.mp4)
# ---------------------------------------------------------------------------
MEDIA_DIR        = BASE_DIR / "media"
IMAGES_DIR       = MEDIA_DIR / "images"
VIDEOS_DIR       = MEDIA_DIR / "videos"
VIDEOS_QUEUE_DIR = VIDEOS_DIR / "queue"
VIDEOS_SENT_DIR  = VIDEOS_DIR / "sent"

# ---------------------------------------------------------------------------
# 📥 Cache — rohe HTML-Eingaben + Amazon-Produkt-HTML + Lock
# ---------------------------------------------------------------------------
CACHE_DIR    = BASE_DIR / "cache"
INBOX_DIR    = CACHE_DIR / "inbox"
PRODUCKT_DIR = CACHE_DIR / "produckt"
LOCK_FILE    = CACHE_DIR / "product_list.lock"


def ensure_directories() -> None:
    """Erzeugt media/ + cache/ Unterverzeichnisse. DB-Ordner legt core.db.engine an."""
    for p in [IMAGES_DIR, VIDEOS_QUEUE_DIR, VIDEOS_SENT_DIR, INBOX_DIR, PRODUCKT_DIR]:
        p.mkdir(parents=True, exist_ok=True)
