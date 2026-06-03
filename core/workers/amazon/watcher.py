# watcher.py
import time, shutil, traceback
from pathlib import Path
import sys

sys.path.append(str(Path(__file__).resolve().parent.parent))

from core.logging import get_logger  # noqa: E402
log = get_logger("watcher")  # noqa: E402
from core.paths import INBOX_DIR
from core.config import WATCH_INTERVAL_SECS
from core.workers.amazon.parser_worker import parse_and_merge
from core.db import workers_repo

_WORKER = "amazon_watcher"

def _pick_oldest_html(inbox: Path) -> Path | None:
    files = [p for p in inbox.glob("*.html")]
    if not files:
        return None
    files.sort(key=lambda p: p.stat().st_mtime)
    return files[0]

def main():
    workers_repo.register(_WORKER)
    log.info(f"[watcher] started. polling {INBOX_DIR} every {WATCH_INTERVAL_SECS}s")
    while True:
        try:
            fp = _pick_oldest_html(INBOX_DIR)
            if not fp:
                workers_repo.set_idle(_WORKER)
                time.sleep(WATCH_INTERVAL_SECS)
                continue

            workers_repo.set_task(_WORKER, f"parsing {fp.name}")
            log.info(f"[watcher] processing {fp.name}")
            try:
                parse_and_merge(fp)
            except Exception:
                # move bad file for later inspection
                log.error(f"[watcher] ERROR while parsing {fp.name} -> moving to bad/")
                traceback.print_exc()            
               
                continue

            # only delete after successful merge
            try:
                fp.unlink()
                log.info(f"[watcher] deleted {fp.name}")
            except Exception:
                log.info(f"[watcher] WARNING: could not delete {fp.name}")

            # immediately continue; if more backlog exists, process next file now
            continue

        except KeyboardInterrupt:
            log.info("[watcher] stopping (KeyboardInterrupt)")
            break
        except Exception:
            # never die permanently; log and keep looping
            log.error("[watcher] unexpected error, continuing in 1s")
            traceback.print_exc()
            time.sleep(1.0)

if __name__ == "__main__":
    main()
