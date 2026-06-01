# facebook/fb_watcher.py
# Ordner-Watcher + Batch-Phase für Facebook-Posts

import asyncio
import json
import pathlib
import random
import sys
import time

HERE         = pathlib.Path(__file__).resolve().parent.parent

CHECK_INTERVAL_SECS = 30
MIN_WAIT_SECS       = 250
MAX_WAIT_SECS       = 500
DEFAULT_REEL_TEMPLATE_TYPE = "offer_type2"

sys.path.insert(0, str(HERE))

from core.logging import get_logger  # noqa: E402
log = get_logger("fb_watcher")  # noqa: E402
from core.db import deals_repo, state_repo, workers_repo

_SENT_KEY = "sent_ids:facebook"
_WORKER = "fb_watcher"


def get_sent_ids() -> set:
    return state_repo.get_set(_SENT_KEY)


def save_sent_ids(sent_ids: set) -> None:
    state_repo.put(_SENT_KEY, sorted(sent_ids))


def get_candidates(sent_ids: set) -> list[dict]:
    """Liefert alle Queue-Deals (als Dicts), die noch nicht von FB versendet wurden."""
    try:
        return [d for d in deals_repo.list_queue() if d["product_id"] not in sent_ids]
    except Exception as e:
        log.error(f"[DB] Fehler beim Lesen der Queue: {e}")
        return []


def print_status_block(current: int, total: int, context: str = "BATCH"):
    remaining = total - current
    percent   = round((current / total) * 100) if total else 0
    log.info("\n==================================================")
    log.info(f"✅  STATUS-REPORT ({context})")
    log.info("==================================================")
    log.info(f"📉  Fortschritt:    {current} von {total} erledigt ({percent}%)")
    log.info(f"🔮  Noch offen:     {remaining} Deals in der Warteschlange")
    log.info("==================================================\n")


async def safety_wait():
    wait      = random.randint(MIN_WAIT_SECS, MAX_WAIT_SECS)
    start_str = time.strftime("%H:%M:%S")
    remaining = wait
    while remaining > 0:
        m, s = divmod(remaining, 60)
        log.info(f"⏳ Letzter Deal: {start_str} Uhr | Nächster Start in: [ {m:02d}:{s:02d} ] ")
        await asyncio.sleep(1)
        remaining -= 1
    log.info("\n\n[SAFETY] 🟢 Pause beendet. Suche nach neuen Deals...")


async def run_init_phase(fb_service) -> None:
    log.info("1⃣  [INIT] Prüfe Dienste...")
    workers_repo.register(_WORKER)

    # Schritt 1: WebSocket-Server starten
    fb_service.init()
    await asyncio.sleep(1)  # kurz warten bis Thread hochgefahren

    # Schritt 2: Verbindung sicherstellen — wie ensure_logged_in() bei Telegram
    log.info("[FACEBOOK] Warte auf Extension-Verbindung vor dem Start...")
    connected = await asyncio.get_event_loop().run_in_executor(
        None,
        lambda: fb_service.ensure_connected(timeout=120),
    )
    if not connected:
        raise SystemExit(
            "[FACEBOOK] ❌ Keine Extension verbunden nach 120s. "
            "Bitte 'addons/facebook' in Chrome laden und facebook.com öffnen."
        )

    log.info("✅ [INIT] Facebook-Verbindung gesichert. Bot startet jetzt mit dem Posten.")


async def route_single_deal(deal: dict, sent_ids: set, fb_service) -> bool:
    """Leitet einen Deal-Datensatz an Post- oder Reel-Processor weiter."""
    data = deal.get("payload") or {}
    deal_type = str(data.get("type") or "").strip().lower()

    if deal_type == "reel":
        from core.workers.facebook.reels_processor import process_single_deal as reel_process
        return await reel_process(deal, sent_ids)
    else:
        from core.workers.facebook.fb_processor import process_single_deal as post_process
        return await post_process(deal, sent_ids, fb_service)


async def run_batch_phase(sent_ids: set, fb_service) -> None:
    """Observer-Modus: bestehende Queue-Dateien werden NICHT verarbeitet.

    Stattdessen werden alle bereits beim Start vorhandenen Dateien als
    'baseline' in sent_ids aufgenommen, damit nur künftig neu eintreffende
    Deals verarbeitet werden.
    """
    log.info("\n2⃣  [BASELINE] Markiere bestehende Queue-Deals als ignoriert...")
    try:
        existing = deals_repo.list_queue()
    except Exception as e:
        log.error(f"[BASELINE] Fehler beim Lesen der DB: {e}")
        existing = []

    added = 0
    for d in existing:
        pid = d.get("product_id")
        if pid and pid not in sent_ids:
            sent_ids.add(pid)
            added += 1

    if added:
        save_sent_ids(sent_ids)
        log.info(f"✅ [BASELINE] {added} bestehende Deals übersprungen (nur neue werden verarbeitet).")
    else:
        log.info("✅ [BASELINE] Keine alten Deals in der Queue.")


async def run_watch_loop(sent_ids: set, fb_service) -> None:
    log.info("\n3️⃣  [WATCHER] 👁️  Live-Modus aktiv...")
    while True:
        try:
            workers_repo.set_idle(_WORKER)
            candidates = get_candidates(sent_ids)
            if candidates:
                total = len(candidates)
                log.info(f"\n[LIVE] 🎯 {total} neuer Deal(s) entdeckt!")
                for i, deal in enumerate(candidates):
                    pid = deal.get("product_id")
                    workers_repo.set_task(_WORKER, f"posting {pid}")
                    was_sent = await route_single_deal(deal, sent_ids, fb_service)
                    if was_sent:
                        save_sent_ids(sent_ids)
                        print_status_block(i + 1, total, "LIVE-INPUT")
                        await safety_wait()
        except Exception as e:
            log.error(f"[LOOP-ERROR] {e}")
        await asyncio.sleep(CHECK_INTERVAL_SECS)


async def start_system():
    import core.workers.facebook.fb_service as fb_service
    log.info("========================================")
    log.info("   🚀 FACEBOOK DEAL BOT SYSTEM          ")
    log.info("========================================")
    sent_ids = get_sent_ids()
    await run_init_phase(fb_service)
    await run_batch_phase(sent_ids, fb_service)
    await run_watch_loop(sent_ids, fb_service)


if __name__ == "__main__":
    asyncio.run(start_system())
