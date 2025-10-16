# config.py
from pathlib import Path

# Basis: Projektordner (wo config.py liegt)
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = (BASE_DIR / "data").resolve()

# Unterordner
INBOX_DIR = DATA_DIR / "inbox"
OUT_DIR = DATA_DIR / "out"
PRODUCKT_DIR = DATA_DIR / "produckt"   # <- NEBEN inbox

# Input- und Output-Pfade
HTML_SOURCE_FILE = DATA_DIR / "html" / "amazon.html" 
TEMP_LLM_INPUT_FILE = DATA_DIR / "llm_input" / "llm_input.json"



# Dateien
PRODUCT_LIST_PATH = DATA_DIR / "product_list.json"
LOCK_FILE = DATA_DIR / "product_list.lock"

FAILED_DIR = (INBOX_DIR / "_failed").resolve()
SUMMARY_PATH  = (OUT_DIR / "summary.jsonl").resolve()
REGISTRY_PATH = (OUT_DIR / ".registry.json").resolve()
OPENED_PATH = DATA_DIR / ".opened.json"    # state file we extend/maintain
REGISTRY_PATH = DATA_DIR  / ".registry.json"  # not required for dedupe, but kept for completeness


# WebSocket-Server
WS_HOST = "127.0.0.1"
WS_PORT = 8765

# Watcher
WATCH_INTERVAL_SECS = 10.0
INTERVAL_SECS = 13


# ---------------------------------------------------------------------------
# 🧩 Directory Helper — zentrale Funktion zum Initialisieren aller Ordner
# ---------------------------------------------------------------------------

def ensure_directories() -> None:
    """
    Erstellt alle benötigten Ordner, falls sie nicht existieren.
    Kann überall importiert und aufgerufen werden.
    """
    for p in [DATA_DIR, INBOX_DIR, PRODUCKT_DIR]:
        p.mkdir(parents=True, exist_ok=True)

    # Optional: Rückmeldung in der Konsole
    print(f"[config] ensured directories:\n  {INBOX_DIR}\n  {PRODUCKT_DIR}\n")
