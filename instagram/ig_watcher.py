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

from config import DEALS_QUEUE_DIR

WATCH_FOLDER        = DEALS_QUEUE_DIR
CHECK_INTERVAL_SECS = 45
MIN_WAIT_SECS       = 300   # 5 min zwischen Posts (Instagram-Limits!)
MAX_WAIT_SECS       = 600   # 10 min

# Separate Sent-IDs für Instagram (unabhängig von Facebook)
try:
    from config import IG_SENT_IDS_PATH
except ImportError:
    IG_SENT_IDS_PATH = HERE / "data" / "state" / "ig_sent_ids.json"


def get_sent_ids() -> set:
    try:
        return set(json.loads(IG_SENT_IDS_PATH.read_text(encoding="utf-8")))
    except Exception:
        return set()


def save_sent_ids(sent_ids: set) -> None:
    IG_SENT_IDS_PATH.parent.mkdir(parents=True, exist_ok=True)
    IG_SENT_IDS_PATH.write_text(
        json.dumps(sorted(sent_ids), indent=2), encoding="utf-8"
    )


def get_candidates(sent_ids: set) -> list[pathlib.Path]:
    try:
        return [
            p for p in WATCH_FOLDER.iterdir()
            if p.suffix == ".json" and p.stem not in sent_ids
        ]
    except Exception as e:
        print(f"[IG-FS] Fehler beim Lesen: {e}")
        return []


async def safety_wait():
    wait      = random.randint(MIN_WAIT_SECS, MAX_WAIT_SECS)
    start_str = time.strftime("%H:%M:%S")
    remaining = wait
    while remaining > 0:
        m, s = divmod(remaining, 60)
        print(
            f"\r[IG] ⏳ Letzter Post: {start_str} | Nächster in: [ {m:02d}:{s:02d} ] ",
            end="", flush=True
        )
        await asyncio.sleep(1)
        remaining -= 1
    print("\n[IG] 🟢 Pause beendet. Suche nach neuen Deals...")


async def run_batch_phase(sent_ids: set) -> None:
    print("\n[IG] 📦 Prüfe Rückstand...")
    candidates = get_candidates(sent_ids)
    if not candidates:
        print("[IG] ✅ Kein Rückstand.")
        return

    from instagram.ig_processor import process_single_deal

    total = len(candidates)
    print(f"[IG] Starte {total} Deals.")
    for i, file in enumerate(candidates):
        was_sent = await process_single_deal(file, sent_ids)
        if was_sent:
            save_sent_ids(sent_ids)
            print(f"[IG] ✅ {i + 1}/{total} erledigt: {file.stem}")
            if i + 1 < total:
                await safety_wait()
        else:
            print(f"[IG] ⏭️ {file.name} übersprungen.")
    print("[IG] ✅ Rückstand abgearbeitet.")


async def run_watch_loop(sent_ids: set) -> None:
    print("\n[IG] 👁️ Live-Watcher aktiv...")
    from instagram.ig_processor import process_single_deal

    while True:
        try:
            candidates = get_candidates(sent_ids)
            if candidates:
                print(f"\n[IG] 🎯 {len(candidates)} neue Datei(en) entdeckt!")
                for i, file in enumerate(candidates):
                    was_sent = await process_single_deal(file, sent_ids)
                    if was_sent:
                        save_sent_ids(sent_ids)
                        print(f"[IG] ✅ Gepostet: {file.stem}")
                        await safety_wait()
        except Exception as e:
            print(f"[IG-LOOP-ERROR] {e}", file=sys.stderr)
        await asyncio.sleep(CHECK_INTERVAL_SECS)


async def start_system():
    print("=" * 45)
    print("   📸 INSTAGRAM DEAL BOT")
    print("=" * 45)

    # Login prüfen beim Start
    print("[IG] Prüfe Instagram-Login...")
    try:
        import instagram.ig_service as ig_service
        ig_service._get_client()
        print("[IG] ✅ Login OK.")
    except Exception as e:
        raise SystemExit(f"[IG] ❌ Login fehlgeschlagen: {e}")

    sent_ids = get_sent_ids()
    await run_batch_phase(sent_ids)
    await run_watch_loop(sent_ids)


if __name__ == "__main__":
    asyncio.run(start_system())
