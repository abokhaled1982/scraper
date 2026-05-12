"""
debug_reel_post.py
------------------
Sendet bereits heruntergeladene Videos direkt an die Chrome-Extension
– ohne Creatomate-Render. Verhält sich wie die Production-App:
  • liest fb_sent.json und überspringt bereits gesendete Videos
  • speichert nach jedem Erfolg in fb_sent.json
  • WebSocket-Server + Handshake identisch zu fb_watcher.py
  • Kommentar-Flow (Affiliate-Link + Gutscheincode) wird mitgesendet

Verwendung:
    python debug_reel_post.py                          # alle ungesendeten Videos
    python debug_reel_post.py --force                  # alle Videos, ignoriert fb_sent
    python debug_reel_post.py B0GYN7PK8P               # bestimmtes Produkt
    python debug_reel_post.py B0GYN7PK8P B0GY1CM3CJ   # mehrere
"""

import asyncio
import json
import pathlib
import sys
import time

HERE        = pathlib.Path(__file__).resolve().parent
VIDEOS_DIR  = HERE / "facebook" / "videos"
DATA_DIR    = HERE / "data" / "out"
SENT_FILE   = HERE / "data" / "fb_sent.json"

sys.path.insert(0, str(HERE))

from facebook import fb_service
from facebook.fb_service import _build_comment_text
from facebook.fb_message import create_facebook_message
from facebook.fb_watcher import get_sent_ids, save_sent_ids


def load_deal(product_id: str) -> dict:
    """Lädt die Deal-JSON-Datei aus data/out/."""
    path = DATA_DIR / f"{product_id}.json"
    if not path.exists():
        print(f"[WARN] Keine Deal-Datei für {product_id} in {DATA_DIR}")
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


async def send_video(product_id: str) -> bool:
    video_path = VIDEOS_DIR / f"{product_id}.mp4"
    if not video_path.exists():
        print(f"[ERROR] Video nicht gefunden: {video_path}")
        return False

    data         = load_deal(product_id)
    offer_url    = str(data.get("affiliate_url") or data.get("url") or "").strip()
    if offer_url in ("N/A", "null"):
        offer_url = ""
    comment_text = _build_comment_text(data, offer_url)
    fb_text      = create_facebook_message(data) if data else f"Deal: {product_id}"

    print(f"\n{'='*60}")
    print(f"[DEBUG] Produkt  : {product_id}")
    print(f"[DEBUG] Video    : {video_path} ({video_path.stat().st_size // 1024} KB)")
    print(f"[DEBUG] Text     : {fb_text[:80]}...")
    print(f"[DEBUG] Kommentar: {comment_text!r}")
    print(f"{'='*60}\n")

    sent = await fb_service.send_post(data, None, video_path)
    if sent:
        print(f"[OK] ✅ {product_id} an Extension gesendet (inkl. Kommentar-Payload).")
    else:
        print(f"[FAIL] ❌ Senden fehlgeschlagen – Extension verbunden?")
    return sent


async def main():
    force_mode = "--force" in sys.argv
    raw_args   = [a for a in sys.argv[1:] if not a.startswith("--")]

    # fb_sent.json laden (wie Production-App)
    sent_ids = get_sent_ids() if not force_mode else set()
    if force_mode:
        print("[INFO] --force aktiv: fb_sent.json wird ignoriert.")
    else:
        print(f"[INFO] fb_sent.json geladen: {len(sent_ids)} bereits gesendete Einträge.")

    # Produkt-IDs aus Kommandozeile ODER alle Videos außer dummy/test
    if raw_args:
        ids = raw_args
    else:
        exclude = {"dummy", "test_reel_deal"}
        videos  = sorted(VIDEOS_DIR.glob("*.mp4"))
        ids     = [v.stem for v in videos if v.stem not in exclude]
        if not ids:
            print("[ERROR] Keine Videos in", VIDEOS_DIR)
            sys.exit(1)

    # Bereits gesendete herausfiltern
    pending = [pid for pid in ids if pid not in sent_ids]
    skipped = [pid for pid in ids if pid in sent_ids]
    if skipped:
        print(f"[SKIP] {len(skipped)} bereits in fb_sent.json: {', '.join(skipped)}")
    if not pending:
        print("[INFO] Alle Videos bereits gesendet. Nichts zu tun.")
        sys.exit(0)

    print(f"\n[INFO] {len(pending)} Video(s) werden gesendet:")
    for i, pid in enumerate(pending, 1):
        print(f"       {i:2}. {pid}")
    print()

    # ── WebSocket-Server starten (identisch zu fb_watcher.run_init_phase) ──
    print("[WS] Starte WebSocket-Server auf ws://localhost:8080...")
    fb_service.init()
    await asyncio.sleep(1)  # Thread hochfahren lassen

    print("[WS] Warte auf Chrome-Extension + Handshake (max. 120s)...")
    print("     → Öffne facebook.com im Browser, Extension muss verbunden sein.\n")
    connected = await asyncio.get_event_loop().run_in_executor(
        None,
        lambda: fb_service.ensure_connected(timeout=120),
    )
    if not connected:
        print("[ERROR] Extension nicht verbunden. Abbruch.")
        sys.exit(1)
    print()

    # Zwischen Reels warten: Reel-Upload + Kommentar-Flow braucht ~3-4 min
    PAUSE_BETWEEN = 4 * 60  # 4 Minuten

    total   = len(pending)
    success = 0
    failed  = []

    for idx, product_id in enumerate(pending, 1):
        print(f"\n[{idx}/{total}] ▶ Starte: {product_id}")
        ok = await send_video(product_id)
        if ok:
            success += 1
            if not force_mode:
                sent_ids.add(product_id)
                save_sent_ids(sent_ids)
                print(f"[SENT] 💾 {product_id} in fb_sent.json gespeichert.")
        else:
            failed.append(product_id)

        if idx < total:
            print(f"\n[PAUSE] ⏳ Warte {PAUSE_BETWEEN // 60} Minuten vor nächstem Reel...\n")
            for remaining in range(PAUSE_BETWEEN, 0, -1):
                m, s = divmod(remaining, 60)
                print(f"\r[PAUSE] ⏳ Nächstes Reel in: {m:02d}:{s:02d}  ", end="", flush=True)
                await asyncio.sleep(1)
            print()

    print(f"\n{'='*60}")
    print(f"[FERTIG] ✅ {success}/{total} erfolgreich gesendet.")
    if failed:
        print(f"[FERTIG] ❌ Fehlgeschlagen: {', '.join(failed)}")
    print(f"{'='*60}")


if __name__ == "__main__":
    asyncio.run(main())
