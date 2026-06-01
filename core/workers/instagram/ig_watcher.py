# instagram/ig_watcher.py
# Queue-Watcher für Instagram-Posts — spiegelt fb_watcher.py

import asyncio
import json
import pathlib
import random
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
from core.logging import get_logger  # noqa: E402
log = get_logger("ig_watcher")  # noqa: E402

from core.db import deals_repo, state_repo, workers_repo

_SENT_KEY = "sent_ids:instagram"
_WORKER = "ig_watcher"

CHECK_INTERVAL_SECS = 45
MIN_WAIT_SECS       = 300   # 5 min zwischen Posts (Instagram-Limits!)
MAX_WAIT_SECS       = 600   # 10 min


def get_sent_ids() -> set:
    return state_repo.get_set(_SENT_KEY)


def save_sent_ids(sent_ids: set) -> None:
    state_repo.put(_SENT_KEY, sorted(sent_ids))


def get_candidates(sent_ids: set) -> list[dict]:
    try:
        return [d for d in deals_repo.list_queue() if d["product_id"] not in sent_ids]
    except Exception as e:
        log.error(f"[IG-DB] Fehler beim Lesen der Queue: {e}")
        return []


async def safety_wait():
    wait      = random.randint(MIN_WAIT_SECS, MAX_WAIT_SECS)
    start_str = time.strftime("%H:%M:%S")
    remaining = wait
    while remaining > 0:
        m, s = divmod(remaining, 60)
        print(
            f"\r[IG] ⏳ Letzter Post: {start_str} | Nächster in: [ {m:02d}:{s:02d} ] "
        )
        await asyncio.sleep(1)
        remaining -= 1
    log.info("\n[IG] 🟢 Pause beendet. Suche nach neuen Deals...")


async def run_batch_phase(sent_ids: set) -> None:
    log.info("\n[IG] 📦 Prüfe Rückstand...")
    candidates = get_candidates(sent_ids)
    if not candidates:
        log.info("[IG] ✅ Kein Rückstand.")
        return

    from core.workers.instagram.ig_processor import process_single_deal

    total = len(candidates)
    log.info(f"[IG] Starte {total} Deals.")
    for i, deal in enumerate(candidates):
        pid = deal.get("product_id")
        was_sent = await process_single_deal(deal, sent_ids)
        if was_sent:
            save_sent_ids(sent_ids)
            log.info(f"[IG] ✅ {i + 1}/{total} erledigt: {pid}")
            if i + 1 < total:
                await safety_wait()
        else:
            log.info(f"[IG] ⏭️ {pid} übersprungen.")
    log.info("[IG] ✅ Rückstand abgearbeitet.")


async def run_watch_loop(sent_ids: set) -> None:
    log.info("\n[IG] 👁️ Live-Watcher aktiv...")
    from core.workers.instagram.ig_processor import process_single_deal

    while True:
        try:
            workers_repo.set_idle(_WORKER)
            candidates = get_candidates(sent_ids)
            if candidates:
                log.info(f"\n[IG] 🎯 {len(candidates)} neuer Deal(s) entdeckt!")
                for i, deal in enumerate(candidates):
                    pid = deal.get("product_id")
                    workers_repo.set_task(_WORKER, f"posting {pid}")
                    was_sent = await process_single_deal(deal, sent_ids)
                    if was_sent:
                        save_sent_ids(sent_ids)
                        log.info(f"[IG] ✅ Gepostet: {pid}")
                        await safety_wait()
        except Exception as e:
            log.error(f"[IG-LOOP-ERROR] {e}")
        await asyncio.sleep(CHECK_INTERVAL_SECS)


async def start_system():
    log.info("=" * 45)
    log.info("   📸 INSTAGRAM DEAL BOT")
    log.info("=" * 45)

    # Login prüfen beim Start
    log.info("[IG] Prüfe Instagram-Login...")
    try:
        import core.workers.instagram.ig_service as ig_service
        ig_service._get_client()
        log.info("[IG] ✅ Login OK.")
    except Exception as e:
        raise SystemExit(f"[IG] ❌ Login fehlgeschlagen: {e}")

    sent_ids = get_sent_ids()
    workers_repo.register(_WORKER)
    await run_batch_phase(sent_ids)
    await run_watch_loop(sent_ids)


if __name__ == "__main__":
    asyncio.run(start_system())
