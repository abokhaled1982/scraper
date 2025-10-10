# config.py
from pathlib import Path

# Basis: Projektordner (wo config.py liegt)
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = (BASE_DIR / "data").resolve()

# Unterordner
INBOX_DIR = DATA_DIR / "inbox"
PRODUCKT_DIR = DATA_DIR / "produckt"   # <- NEBEN inbox


# Dateien
PRODUCT_LIST_PATH = DATA_DIR / "product_list.json"
LOCK_FILE = DATA_DIR / "product_list.lock"

# WebSocket-Server
WS_HOST = "127.0.0.1"
WS_PORT = 8765

# Watcher
WATCH_INTERVAL_SECS = 10.0


# ---------------------------------------------------------------------------
# 🧩 Directory Helper — zentrale Funktion zum Initialisieren aller Ordner
# ---------------------------------------------------------------------------

def ensure_directories() -> None:
    """
    Erstellt alle benötigten Ordner, falls sie nicht existieren.
    Kann überall importiert und aufgerufen werden.
    """
    for p in [DATA_DIR, INBOX_DIR, PRODUCKT_DIR, BAD_SUBDIR]:
        p.mkdir(parents=True, exist_ok=True)

    # Optional: Rückmeldung in der Konsole
    print(f"[config] ensured directories:\n  {INBOX_DIR}\n  {PRODUCKT_DIR}\n  {BAD_SUBDIR}")
