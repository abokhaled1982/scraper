"""
debug_reel_post.py
------------------
Sendet ein bereits heruntergeladenes Video direkt an die Chrome-Extension
– ohne Creatomate-Render. Nützlich zum Debuggen des Posting- und Kommentar-Flows.

Verwendung:
    python debug_reel_post.py                          # nimmt erstes verfügbares Video
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

sys.path.insert(0, str(HERE))

from facebook import fb_service
from facebook.fb_service import _build_comment_text
from facebook.fb_message import create_facebook_message


def load_deal(product_id: str) -> dict | None:
    """Lädt die Deal-JSON-Datei aus data/out/."""
    path = DATA_DIR / f"{product_id}.json"
    if not path.exists():
        print(f"[WARN] Keine Deal-Datei für {product_id} in {DATA_DIR}")
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


async def send_video(product_id: str):
    video_path = VIDEOS_DIR / f"{product_id}.mp4"
    if not video_path.exists():
        print(f"[ERROR] Video nicht gefunden: {video_path}")
        return False

    data = load_deal(product_id)
    offer_url    = str(data.get("affiliate_url") or data.get("url") or "").strip()
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
        print(f"[OK] ✅ {product_id} an Extension gesendet.")
    else:
        print(f"[FAIL] ❌ Senden fehlgeschlagen – Extension verbunden?")
    return sent


async def main():
    # Produkt-IDs aus Kommandozeile ODER alle Videos außer dummy/test
    if len(sys.argv) > 1:
        ids = sys.argv[1:]
    else:
        exclude = {"dummy", "test_reel_deal"}
        videos  = sorted(VIDEOS_DIR.glob("*.mp4"))
        ids     = [v.stem for v in videos if v.stem not in exclude]
        if not ids:
            print("[ERROR] Keine Videos in", VIDEOS_DIR)
            sys.exit(1)
        print(f"[INFO] Alle Videos ({len(ids)}):")
        for i, vid_id in enumerate(ids, 1):
            print(f"       {i:2}. {vid_id}")
        print()

    # WebSocket-Server starten
    print("[WS] Starte WebSocket-Server...")
    fb_service.init()
    time.sleep(1)

    print("[WS] Warte auf Chrome-Extension (max. 120s)...")
    print("     → Öffne Facebook im Browser, Extension muss verbunden sein.\n")
    connected = fb_service.ensure_connected(timeout=120)
    if not connected:
        print("[ERROR] Extension nicht verbunden. Abbruch.")
        sys.exit(1)

    # Zwischen Reels warten: Reel-Upload + Kommentar-Flow braucht ~3-4 min
    # Wir warten nach jedem Senden 4 Minuten, damit der komplette Flow
    # (60-120s Warten + Seitenneulad + Kommentar) fertig ist, bevor das nächste startet.
    PAUSE_BETWEEN = 4 * 60  # 4 Minuten

    total   = len(ids)
    success = 0
    failed  = []

    for idx, product_id in enumerate(ids, 1):
        print(f"\n[{idx}/{total}] ▶ Starte: {product_id}")
        ok = await send_video(product_id)
        if ok:
            success += 1
        else:
            failed.append(product_id)

        if idx < total:
            print(f"\n[PAUSE] ⏳ Warte {PAUSE_BETWEEN // 60} Minuten vor nächstem Reel...\n")
            await asyncio.sleep(PAUSE_BETWEEN)

    print(f"\n{'='*60}")
    print(f"[FERTIG] ✅ {success}/{total} erfolgreich gesendet.")
    if failed:
        print(f"[FERTIG] ❌ Fehlgeschlagen: {', '.join(failed)}")
    print(f"{'='*60}")


if __name__ == "__main__":
    asyncio.run(main())
