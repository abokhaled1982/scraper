"""
debug_reel_post.py
------------------
Sendet bereits heruntergeladene Videos direkt an die Chrome-Extension
– ohne Creatomate-Render. Verhält sich wie die Production-App:
  • liest fb_sent.json und überspringt bereits gesendete Videos
  • speichert nach jedem Erfolg in fb_sent.json
  • WebSocket-Server + Handshake identisch zu fb_watcher.py
  • Kommentar-Flow (Affiliate-Link + Gutscheincode) wird mitgesendet

Mit --render:
  • Rendert Video erst via Creatomate aus Queue-JSON, dann senden
  • Template wird automatisch aus Deal-JSON gewählt (default: offer_type2)
  • Mit --template <typ> kann das Template erzwungen werden

Verwendung:
    python debug_reel_post.py                              # alle ungesendeten Videos (sent/)
    python debug_reel_post.py --force                      # alle Videos, ignoriert fb_sent
    python debug_reel_post.py B0GYN7PK8P                   # bestimmtes Produkt
    python debug_reel_post.py B0GYN7PK8P B0GY1CM3CJ       # mehrere
    python debug_reel_post.py --render                     # erstes Queue-JSON rendern + senden
    python debug_reel_post.py --render B0BG383MDN          # bestimmtes Queue-JSON rendern + senden
    python debug_reel_post.py --render --template offer_type2 B0BG383MDN
"""

import asyncio
import json
import pathlib
import sys
import time

HERE        = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import config as _cfg
VIDEOS_DIR  = _cfg.VIDEOS_SENT_DIR    # data/media/videos/sent/
DATA_DIR    = _cfg.DEALS_QUEUE_DIR    # data/deals/queue/
SENT_FILE   = _cfg.SENT_IDS_PATH      # data/state/sent_ids.json

from facebook import fb_service
from facebook.fb_service import _build_comment_text
from facebook.fb_message import create_facebook_message
from facebook.fb_watcher import get_sent_ids, save_sent_ids
from facebook.template_interface import build_modifications_for_template, resolve_template_selection
from facebook.reels_service import render_reel, download_video


def load_deal(product_id: str) -> dict:
    """Lädt die Deal-JSON-Datei – sucht in queue/ dann sent/."""
    for search_dir in [DATA_DIR, _cfg.DEALS_SENT_DIR]:
        path = search_dir / f"{product_id}.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    print(f"[WARN] Keine Deal-Datei für {product_id} in {DATA_DIR} oder sent/")
    return {}


def _calc_discount(data: dict) -> float:
    raw = str(data.get("discount_percent") or "").replace("-", "").replace("%", "").replace(",", ".").strip()
    try:
        return float(raw) if raw else 0.0
    except ValueError:
        return 0.0


async def render_and_send(product_id: str, template_override: str | None = None) -> bool:
    """Rendert das Video via Creatomate und sendet es direkt an Facebook."""
    data = load_deal(product_id)
    if not data:
        print(f"[ERROR] Kein Deal-JSON für {product_id} gefunden.")
        return False

    if template_override:
        data["template_type"] = template_override
    template_type, template_id = resolve_template_selection(data, default_template_type="offer_type2")
    discount = _calc_discount(data)
    modifications = build_modifications_for_template(data, template_type=template_type, discount_value=discount)

    print(f"[RENDER] 🧩 Template: {template_type} ({template_id})")
    print(f"[RENDER] 📦 Modifications:")
    for k, v in modifications.items():
        print(f"         {k}: {v}")

    loop = asyncio.get_event_loop()
    print(f"[RENDER] 🚀 Starte Creatomate-Render für {product_id}...")
    try:
        render_result = await loop.run_in_executor(None, render_reel, modifications, template_id)
    except Exception as e:
        print(f"[RENDER] ❌ Render fehlgeschlagen: {e}")
        return False

    print(f"[RENDER] ✅ Render fertig. URL: {render_result.get('url')}")

    local_video = await loop.run_in_executor(None, download_video, render_result, product_id)
    if not local_video:
        print(f"[RENDER] ❌ Video-Download fehlgeschlagen.")
        return False
    print(f"[RENDER] ✅ Video gespeichert: {local_video}")

    sent = await fb_service.send_post(data, None, local_video)
    if sent:
        print(f"[OK] ✅ {product_id} erfolgreich gerendert und gepostet.")
    else:
        print(f"[FAIL] ❌ Senden fehlgeschlagen – Extension verbunden?")
    return sent


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
    force_mode    = "--force" in sys.argv
    render_mode   = "--render" in sys.argv
    template_flag = None
    if "--template" in sys.argv:
        idx = sys.argv.index("--template")
        if idx + 1 < len(sys.argv):
            template_flag = sys.argv[idx + 1]
    raw_args = [a for a in sys.argv[1:] if not a.startswith("--") and a != template_flag]

    # fb_sent.json laden (wie Production-App)
    sent_ids = get_sent_ids() if not force_mode else set()
    if force_mode:
        print("[INFO] --force aktiv: fb_sent.json wird ignoriert.")
    else:
        print(f"[INFO] fb_sent.json geladen: {len(sent_ids)} bereits gesendete Einträge.")

    # ── RENDER-Modus: Creatomate → Download → Senden ─────────────────────────
    if render_mode:
        if raw_args:
            ids = raw_args
        else:
            files = sorted(_cfg.DEALS_QUEUE_DIR.glob("*.json"))
            if not files:
                print(f"[ERROR] Keine JSON-Dateien in {_cfg.DEALS_QUEUE_DIR}")
                sys.exit(1)
            ids = [f.stem for f in files]

        pending = [pid for pid in ids if pid not in sent_ids]
        skipped = [pid for pid in ids if pid in sent_ids]
        if skipped:
            print(f"[SKIP] {len(skipped)} bereits gesendet: {', '.join(skipped)}")
        if not pending:
            print("[INFO] Alle bereits gesendet. Nichts zu tun.")
            sys.exit(0)

        print(f"\n[INFO] {len(pending)} Deal(s) werden gerendert + gesendet:")
        for i, pid in enumerate(pending, 1):
            print(f"       {i:2}. {pid}")
        print()

        print("[WS] Starte WebSocket-Server...")
        fb_service.init()
        await asyncio.sleep(1)
        print("[WS] Warte auf Chrome-Extension + Handshake (max. 120s)...")
        print("     → Öffne facebook.com im Browser, Extension muss verbunden sein.\n")
        connected = await asyncio.get_event_loop().run_in_executor(
            None, lambda: fb_service.ensure_connected(timeout=120)
        )
        if not connected:
            print("[ERROR] Extension nicht verbunden. Abbruch.")
            sys.exit(1)
        print()

        total = len(pending)
        success = 0
        failed = []
        for idx, product_id in enumerate(pending, 1):
            print(f"\n[{idx}/{total}] ▶ Render + Send: {product_id}")
            ok = await render_and_send(product_id, template_override=template_flag)
            if ok:
                success += 1
                if not force_mode:
                    sent_ids.add(product_id)
                    save_sent_ids(sent_ids)
                    print(f"[SENT] 💾 {product_id} in fb_sent.json gespeichert.")
            else:
                failed.append(product_id)
            if idx < total:
                print(f"\n[PAUSE] ⏳ Warte 4 Minuten vor nächstem Reel...\n")
                for remaining in range(4 * 60, 0, -1):
                    m, s = divmod(remaining, 60)
                    print(f"\r[PAUSE] ⏳ {m:02d}:{s:02d}  ", end="", flush=True)
                    await asyncio.sleep(1)
                print()

        print(f"\n{'='*60}")
        print(f"[FERTIG] ✅ {success}/{total} erfolgreich gerendert + gesendet.")
        if failed:
            print(f"[FERTIG] ❌ Fehlgeschlagen: {', '.join(failed)}")
        print(f"{'='*60}")
        return

    # ── NORMAL-Modus: vorhandene Videos aus sent/ senden ─────────────────────
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
