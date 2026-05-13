# config.py
from pathlib import Path

# Basis: Projektordner (wo config.py liegt)
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = (BASE_DIR / "data").resolve()

# ---------------------------------------------------------------------------
# 📂 Deals — JSON-Dateien (ASIN.json)
# ---------------------------------------------------------------------------
DEALS_DIR        = DATA_DIR / "deals"
DEALS_QUEUE_DIR  = DEALS_DIR / "queue"   # wartet auf Posting (war: data/out/)
DEALS_SENT_DIR   = DEALS_DIR / "sent"    # erfolgreich gepostet
DEALS_FAILED_DIR = DEALS_DIR / "failed"  # fehlgeschlagene Deals

# Rückwärtskompatibilität (alte Importe nicht brechen)
OUT_DIR = DEALS_QUEUE_DIR

# ---------------------------------------------------------------------------
# 🖼️ Media — Bilder und Videos (ASIN.jpg / ASIN.mp4)
# ---------------------------------------------------------------------------
MEDIA_DIR          = DATA_DIR / "media"
IMAGES_DIR         = MEDIA_DIR / "images"          # (war: facebook/images/)
VIDEOS_DIR         = MEDIA_DIR / "videos"
VIDEOS_QUEUE_DIR   = VIDEOS_DIR / "queue"           # gerendert, noch nicht gepostet
VIDEOS_SENT_DIR    = VIDEOS_DIR / "sent"            # bereits gepostet (war: facebook/videos1/)

# ---------------------------------------------------------------------------
# 💾 State — Zustandsdateien
# ---------------------------------------------------------------------------
STATE_DIR         = DATA_DIR / "state"
SENT_IDS_PATH     = STATE_DIR / "sent_ids.json"     # (war: data/fb_sent.json)IG_SENT_IDS_PATH  = STATE_DIR / "ig_sent_ids.json"  # InstagramIG_SENT_IDS_PATH  = STATE_DIR / "ig_sent_ids.json"  # Instagram
SENT_ASINS_PATH   = STATE_DIR / "sent_asins.json"   # (war: data/sent_asins.json)
PRODUCT_LIST_PATH = STATE_DIR / "product_list.json" # (war: data/product_list.json)
OPENED_PATH       = STATE_DIR / ".opened.json"
REGISTRY_PATH     = STATE_DIR / ".registry.json"

# ---------------------------------------------------------------------------
# 📥 Inbox — rohe HTML-Eingaben vom Scraper
# ---------------------------------------------------------------------------
INBOX_DIR    = DATA_DIR / "inbox"
FAILED_DIR   = INBOX_DIR / "_failed"
PRODUCKT_DIR = DATA_DIR / "produckt"    # HTML-Cache Amazon

# Weitere Pfade
LOCK_FILE           = DATA_DIR / "product_list.lock"
HTML_SOURCE_FILE    = DATA_DIR / "html" / "playstation.html"
TEMP_LLM_INPUT_FILE = DATA_DIR / "llm_input" / "llm_input.json"
SUMMARY_PATH        = DEALS_QUEUE_DIR / "summary.jsonl"

# ---------------------------------------------------------------------------
# 🌐 WebSocket-Server
# ---------------------------------------------------------------------------
WS_HOST = "127.0.0.1"
WS_PORT = 8765

# Watcher
WATCH_INTERVAL_SECS = 10.0
INTERVAL_SECS = 13


# ---------------------------------------------------------------------------
# 🧩 Directory Helper — zentrale Funktion zum Initialisieren aller Ordner
# ---------------------------------------------------------------------------

def ensure_directories() -> None:
    for p in [
        DEALS_QUEUE_DIR, DEALS_SENT_DIR, DEALS_FAILED_DIR,
        IMAGES_DIR, VIDEOS_QUEUE_DIR, VIDEOS_SENT_DIR,
        STATE_DIR, INBOX_DIR, FAILED_DIR, PRODUCKT_DIR,
    ]:
        p.mkdir(parents=True, exist_ok=True)
    print(
        f"[config] directories ready:\n"
        f"  deals/queue  → {DEALS_QUEUE_DIR}\n"
        f"  deals/sent   → {DEALS_SENT_DIR}\n"
        f"  media/images → {IMAGES_DIR}\n"
        f"  media/videos → {VIDEOS_QUEUE_DIR}\n"
        f"  state        → {STATE_DIR}\n"
    )
