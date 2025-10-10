# watcher.py
import time, shutil, traceback
from pathlib import Path
import sys

sys.path.append(str(Path(__file__).resolve().parent.parent))
from config import INBOX_DIR,WATCH_INTERVAL_SECS
from parser_worker import parse_and_merge

def _pick_oldest_html(inbox: Path) -> Path | None:
    files = [p for p in inbox.glob("*.html")]
    if not files:
        return None
    files.sort(key=lambda p: p.stat().st_mtime)
    return files[0]

def main():
    INBOX_DIR.mkdir(parents=True, exist_ok=True)  

    print(f"[watcher] started. polling {INBOX_DIR} every {WATCH_INTERVAL_SECS}s")
    while True:
        try:
            fp = _pick_oldest_html(INBOX_DIR)
            if not fp:
                time.sleep(WATCH_INTERVAL_SECS)
                continue

            print(f"[watcher] processing {fp.name}")
            try:
                parse_and_merge(fp)
            except Exception:
                # move bad file for later inspection
                print(f"[watcher] ERROR while parsing {fp.name} -> moving to bad/")
                traceback.print_exc()            
               
                continue

            # only delete after successful merge
            try:
                fp.unlink()
                print(f"[watcher] deleted {fp.name}")
            except Exception:
                print(f"[watcher] WARNING: could not delete {fp.name}")

            # immediately continue; if more backlog exists, process next file now
            continue

        except KeyboardInterrupt:
            print("[watcher] stopping (KeyboardInterrupt)")
            break
        except Exception:
            # never die permanently; log and keep looping
            print("[watcher] unexpected error, continuing in 1s")
            traceback.print_exc()
            time.sleep(1.0)

if __name__ == "__main__":
    main()
