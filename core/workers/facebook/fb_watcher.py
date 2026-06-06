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
from core.db import deals_repo, state_repo, workers_repo, config_repo as cfg

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
    # Vom Dashboard auslösbar: skip-wait-Token überspringen die Pause komplett.
    if cfg.consume_skip_wait_token("facebook"):
        log.info("⏭️  [SAFETY] Wait-Token konsumiert — überspringe Pause")
        return
    # Min/Max-Wartezeit aus Dashboard-Config (fallback auf Modul-Defaults)
    min_w = int(cfg.get("facebook.min_wait_secs", MIN_WAIT_SECS) or MIN_WAIT_SECS)
    max_w = int(cfg.get("facebook.max_wait_secs", MAX_WAIT_SECS) or MAX_WAIT_SECS)
    if max_w < min_w:
        max_w = min_w
    wait      = random.randint(min_w, max_w)
    start_str = time.strftime("%H:%M:%S")
    # Initialen Countdown für Dashboard/TUI publizieren
    workers_repo.set_next_run(_WORKER, wait, label="safety wait")
    remaining = wait
    _last_pub = wait
    while remaining > 0:
        # Falls Dashboard mittlerweile Token gesetzt hat → sofort raus
        if cfg.consume_skip_wait_token("facebook"):
            log.info("⏭️  [SAFETY] Pause durch Dashboard abgebrochen")
            return
        m, s = divmod(remaining, 60)
        log.info(f"⏳ Letzter Deal: {start_str} Uhr | Nächster Start in: [ {m:02d}:{s:02d} ] ")
        # Countdown alle 5s in die DB schreiben (nicht jede Sekunde → DB-Last)
        if _last_pub - remaining >= 5 or remaining < 5:
            workers_repo.set_next_run(_WORKER, remaining, label="safety wait")
            _last_pub = remaining
        await asyncio.sleep(1)
        remaining -= 1
    log.info("\n\n[SAFETY] 🟢 Pause beendet. Suche nach neuen Deals...")


async def run_init_phase(fb_service) -> None:
    log.info("1⃣  [INIT] Prüfe Dienste...")
    workers_repo.register(_WORKER)

    # Schritt 1: WebSocket-Server starten
    fb_service.init()
    await asyncio.sleep(1)  # kurz warten bis Thread hochgefahren

    # Schritt 2: Chrome mit eigenem Profil + Facebook-Addon auto-starten,
    # damit sich die Extension automatisch beim WebSocket-Server meldet
    # (kein manuelles Chrome-Öffnen / Addon-Laden mehr nötig).
    import os as _os
    from core.workers.chrome_launcher import ChromeProfile
    fb_profile_name = _os.environ.get("FACEBOOK_CHROME_PROFILE", "facebook")
    fb_addon_dir    = _os.environ.get("FACEBOOK_ADDON_DIR", "addons/facebook")
    fb_start_url    = _os.environ.get("FACEBOOK_START_URL", "https://www.facebook.com/")
    fb_chrome = ChromeProfile(fb_profile_name, addons=[fb_addon_dir])
    fb_chrome.launch_if_needed(start_url=fb_start_url)

    # Schritt 3: Verbindung sicherstellen — wie ensure_logged_in() bei Telegram
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
    # Hard-Toggle: Facebook komplett aus
    if not cfg.is_enabled("facebook"):
        log.info("⏸️  [FACEBOOK] deaktiviert (Dashboard) — überspringe Deal")
        return False
    data = deal.get("payload") or {}
    deal_type = str(data.get("type") or "").strip().lower()

    if deal_type == "reel":
        if not cfg.get("facebook.post_reels", True):
            log.info("⏸️  [FACEBOOK] Reels deaktiviert (Dashboard) — überspringe")
            return False
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
            # Worker-Pause respektieren (vom Dashboard gesetzt)
            if cfg.is_worker_paused(_WORKER) or not cfg.is_enabled("facebook"):
                workers_repo.set_next_run(_WORKER, CHECK_INTERVAL_SECS, label="queue check (paused)")
                await asyncio.sleep(CHECK_INTERVAL_SECS)
                continue
            # Dashboard-Resend-Sync: sent_ids aus der DB neu laden, damit
            # vom Dashboard entfernte product_ids auch im laufenden Worker
            # sofort wieder als Kandidat erkannt werden.
            db_sent = get_sent_ids()
            removed = sent_ids - db_sent
            if removed:
                sent_ids.difference_update(removed)
                log.info(f"♻️  [SYNC] {len(removed)} sent_ids vom Dashboard entfernt → erneut verarbeitbar")
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
        # Live-Countdown bis zum nächsten Tick (für Dashboard/TUI)
        workers_repo.set_next_run(_WORKER, CHECK_INTERVAL_SECS, label="queue check")
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
