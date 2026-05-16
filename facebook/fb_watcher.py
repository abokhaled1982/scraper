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
from config import DEALS_QUEUE_DIR, SENT_IDS_PATH

WATCH_FOLDER = DEALS_QUEUE_DIR
SENT_FILE    = SENT_IDS_PATH


def get_sent_ids() -> set:
    try:
        return set(json.loads(SENT_FILE.read_text(encoding="utf-8")))
    except Exception:
        return set()


def save_sent_ids(sent_ids: set) -> None:
    SENT_FILE.parent.mkdir(parents=True, exist_ok=True)
    SENT_FILE.write_text(json.dumps(sorted(sent_ids), indent=2), encoding="utf-8")


def get_candidates(sent_ids: set) -> list[pathlib.Path]:
    try:
        return [p for p in WATCH_FOLDER.iterdir() if p.suffix == ".json" and p.stem not in sent_ids]
    except Exception as e:
        print(f"[FS] Fehler beim Lesen: {e}")
        return []


def print_status_block(current: int, total: int, context: str = "BATCH"):
    remaining = total - current
    percent   = round((current / total) * 100) if total else 0
    print("\n==================================================")
    print(f"✅  STATUS-REPORT ({context})")
    print("==================================================")
    print(f"📉  Fortschritt:    {current} von {total} erledigt ({percent}%)")
    print(f"🔮  Noch offen:     {remaining} Deals in der Warteschlange")
    print("==================================================\n")


async def safety_wait():
    wait      = random.randint(MIN_WAIT_SECS, MAX_WAIT_SECS)
    start_str = time.strftime("%H:%M:%S")
    remaining = wait
    while remaining > 0:
        m, s = divmod(remaining, 60)
        print(f"\r⏳ Letzter Deal: {start_str} Uhr | Nächster Start in: [ {m:02d}:{s:02d} ] ", end="", flush=True)
        await asyncio.sleep(1)
        remaining -= 1
    print("\n\n[SAFETY] 🟢 Pause beendet. Suche nach neuen Deals...")


async def run_init_phase(fb_service) -> None:
    print("1️⃣  [INIT] Prüfe Ordner und Dienste...")
    WATCH_FOLDER.mkdir(parents=True, exist_ok=True)

    # Schritt 1: WebSocket-Server starten
    fb_service.init()
    await asyncio.sleep(1)  # kurz warten bis Thread hochgefahren

    # Schritt 2: Verbindung sicherstellen — wie ensure_logged_in() bei Telegram
    print("[FACEBOOK] Warte auf Extension-Verbindung vor dem Start...")
    connected = await asyncio.get_event_loop().run_in_executor(
        None,
        lambda: fb_service.ensure_connected(timeout=120),
    )
    if not connected:
        raise SystemExit(
            "[FACEBOOK] ❌ Keine Extension verbunden nach 120s. "
            "Bitte 'addons/facebook' in Chrome laden und facebook.com öffnen."
        )

    print("✅ [INIT] Facebook-Verbindung gesichert. Bot startet jetzt mit dem Posten.")


async def route_single_deal(file: pathlib.Path, sent_ids: set, fb_service) -> bool:
    """Liest die JSON und leitet an Post- oder Reel-Processor weiter."""
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[ROUTE] Fehler beim Lesen {file.name}: {e}")
        return False

    deal_type = str(data.get("type") or "").strip().lower()

    if deal_type == "reel":
        from facebook.reels_processor import process_single_deal as reel_process
        return await reel_process(file, sent_ids)
    else:
        from facebook.fb_processor import process_single_deal as post_process
        return await post_process(file, sent_ids, fb_service)


async def run_batch_phase(sent_ids: set, fb_service) -> None:
    """Observer-Modus: bestehende Queue-Dateien werden NICHT verarbeitet.

    Stattdessen werden alle bereits beim Start vorhandenen Dateien als
    'baseline' in sent_ids aufgenommen, damit nur künftig neu eintreffende
    Deals verarbeitet werden.
    """
    print("\n2️⃣  [BASELINE] Markiere bestehende Queue-Dateien als ignoriert...")
    try:
        existing = [p for p in WATCH_FOLDER.iterdir() if p.suffix == ".json"]
    except Exception as e:
        print(f"[BASELINE] Fehler beim Lesen: {e}")
        existing = []

    added = 0
    for p in existing:
        if p.stem not in sent_ids:
            sent_ids.add(p.stem)
            added += 1

    if added:
        save_sent_ids(sent_ids)
        print(f"✅ [BASELINE] {added} bestehende Deals übersprungen (nur neue werden verarbeitet).")
    else:
        print("✅ [BASELINE] Keine alten Deals in der Queue.")


async def run_watch_loop(sent_ids: set, fb_service) -> None:
    print("\n3️⃣  [WATCHER] 👁️  Live-Modus aktiv...")
    while True:
        try:
            candidates = get_candidates(sent_ids)
            if candidates:
                total = len(candidates)
                print(f"\n[LIVE] 🎯 {total} neue Datei(en) entdeckt!")
                for i, file in enumerate(candidates):
                    was_sent = await route_single_deal(file, sent_ids, fb_service)
                    if was_sent:
                        save_sent_ids(sent_ids)
                        print_status_block(i + 1, total, "LIVE-INPUT")
                        await safety_wait()
        except Exception as e:
            print(f"[LOOP-ERROR] {e}", file=sys.stderr)
        await asyncio.sleep(CHECK_INTERVAL_SECS)


async def start_system():
    import facebook.fb_service as fb_service
    print("========================================")
    print("   🚀 FACEBOOK DEAL BOT SYSTEM          ")
    print("========================================")
    sent_ids = get_sent_ids()
    await run_init_phase(fb_service)
    await run_batch_phase(sent_ids, fb_service)
    await run_watch_loop(sent_ids, fb_service)


if __name__ == "__main__":
    asyncio.run(start_system())
