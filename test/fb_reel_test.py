#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test/fb_reel_test.py — Test-CLI für fb_service + reels_service.

ÜBERBLICK
─────────
  • Kann interaktiv ODER per CLI-Argument gesteuert werden.
  • Zeigt LIVE-Logs (fb_service, websockets, ggf. asyncio) direkt im Terminal,
    damit man bei Fehlschlägen sofort sieht, wo es klemmt — zusätzlich landet
    alles weiterhin im normalen Datei-Log (.log/<DATUM>/<worker>.log).
  • Bei Fehlern wird ein vollständiger Diagnose-Block ausgegeben (PID des
    fb_service-Threads, verbundene Clients, letzte 30 Log-Zeilen, etc.).

QUICKSTART
──────────
  # Interaktiv (Menü + Auswahl):
  python test/fb_reel_test.py

  # Direkt ein Video aus der Queue per Nummer wählen + senden:
  python test/fb_reel_test.py --video 1

  # Per Dateiname oder absolutem Pfad:
  python test/fb_reel_test.py --video B0CSG46SSN.mp4
  python test/fb_reel_test.py --video /pfad/zu/clip.mp4

  # Mit eigenem Titel/URL/Coupon:
  python test/fb_reel_test.py --video 2 \
      --title "Anker Bluetooth Kopfhörer" \
      --url   https://www.amazon.de/dp/B0XXXXX \
      --coupon RABATT10

  # Nur Videos auflisten, nicht senden:
  python test/fb_reel_test.py --list

  # Creatomate-Render-Test (ohne Facebook):
  python test/fb_reel_test.py --action creatomate

  # Ausführliches DEBUG-Logging:
  python test/fb_reel_test.py --video 1 --verbose

VORAUSSETZUNGEN
───────────────
  • Chrome läuft mit dem Facebook-Profil + geladener Extension
    (einmalig:  python run_all.py --setup-profiles facebook)
  • Bei Facebook eingeloggt.
  • Für --action creatomate: CREATOMATE_API_KEY in .env oder Template-JSON
    mit "api_key"-Feld unter core/workers/facebook/templates/.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Optional

# ── Projekt-Root in sys.path aufnehmen ────────────────────────────────
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from core.logging import get_logger  # noqa: E402

log = get_logger("fb_reel_test")

VIDEO_DIR = ROOT / "media" / "videos" / "queue"
LOG_DIR = ROOT / ".log"


# ──────────────────────────────────────────────────────────────────────
# Farb-Helpers
# ──────────────────────────────────────────────────────────────────────
def _bold(s: str) -> str:    return f"\033[1m{s}\033[0m"
def _green(s: str) -> str:   return f"\033[32m{s}\033[0m"
def _cyan(s: str) -> str:    return f"\033[36m{s}\033[0m"
def _yellow(s: str) -> str:  return f"\033[33m{s}\033[0m"
def _red(s: str) -> str:     return f"\033[31m{s}\033[0m"
def _grey(s: str) -> str:    return f"\033[90m{s}\033[0m"


# ──────────────────────────────────────────────────────────────────────
# Live-Logging Setup
# ──────────────────────────────────────────────────────────────────────
def setup_live_console_logging(verbose: bool = False) -> None:
    """Hängt einen farbigen Stdout-Handler an alle relevanten Logger.

    `core.logging.get_logger(...)` setzt `propagate = False` und sendet
    primär an den TCP-Logserver / ein Logfile. Damit man im Test-Terminal
    SOFORT sieht, was passiert (vor allem Fehler), spiegeln wir die wichtigen
    Logs zusätzlich live auf die Konsole.
    """
    level = logging.DEBUG if verbose else logging.INFO

    class _ColorFmt(logging.Formatter):
        COLOR = {
            "DEBUG":    "\033[90m",   # grau
            "INFO":     "\033[36m",   # cyan
            "WARNING":  "\033[33m",   # gelb
            "ERROR":    "\033[31m",   # rot
            "CRITICAL": "\033[1;31m",
        }

        def format(self, record: logging.LogRecord) -> str:
            base = super().format(record)
            col = self.COLOR.get(record.levelname, "")
            return f"{col}{base}\033[0m"

    h = logging.StreamHandler(stream=sys.stdout)
    h.setLevel(level)
    h.setFormatter(_ColorFmt(
        fmt="[%(asctime)s] %(name)-14s %(levelname)-5s | %(message)s",
        datefmt="%H:%M:%S",
    ))

    targets = ("fb_service", "fb_reel_test", "reels_service",
               "fb_message", "websockets.server", "asyncio")
    for name in targets:
        lg = logging.getLogger(name)
        lg.setLevel(level)
        lg.addHandler(h)
        # WICHTIG: propagate=False (gesetzt von get_logger) lassen wir so —
        # sonst hätten wir Duplikate über den Root-Logger.


# ──────────────────────────────────────────────────────────────────────
# Datei-Helpers
# ──────────────────────────────────────────────────────────────────────
def _list_videos() -> list[Path]:
    if not VIDEO_DIR.exists():
        return []
    return sorted(p for p in VIDEO_DIR.iterdir() if p.suffix.lower() == ".mp4")


def _find_cover(video: Path) -> Optional[Path]:
    """Sucht nach passendem Cover-Bild: <video>.jpg / .png / <video>(ohne .mp4)+.jpg"""
    candidates = [
        video.with_suffix(video.suffix + ".jpg"),
        video.with_suffix(video.suffix + ".png"),
        video.with_suffix(".jpg"),
        video.with_suffix(".png"),
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


def _human_size(n: int) -> str:
    nf = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if nf < 1024:
            return f"{nf:6.1f} {unit}"
        nf /= 1024
    return f"{nf:.1f} TB"


def _resolve_video(arg: str | None) -> Optional[Path]:
    """Akzeptiert: Index (1-basiert), Dateiname, absoluter Pfad oder None."""
    if not arg:
        return None
    p = Path(arg)
    if p.is_absolute() and p.exists():
        return p
    videos = _list_videos()
    # 1) Index
    if arg.isdigit():
        idx = int(arg)
        if 1 <= idx <= len(videos):
            return videos[idx - 1]
        return None
    # 2) Exakter / partieller Match auf Dateinamen
    direct = VIDEO_DIR / arg
    if direct.exists():
        return direct
    matches = [v for v in videos if arg.lower() in v.name.lower()]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        log.warning(f"Mehrdeutig: '{arg}' passt auf {len(matches)} Videos.")
    return None


def _print_video_table(videos: list[Path], header: str = "Videos") -> None:
    print()
    print(_bold(f"📁 {header} in {VIDEO_DIR}:"))
    if not videos:
        print(_red("   (keine .mp4 gefunden)"))
        return
    for i, v in enumerate(videos, start=1):
        size = _human_size(v.stat().st_size)
        cov = _find_cover(v)
        cov_str = _green(f"  + Cover: {cov.name}") if cov else _yellow("  (kein Cover)")
        print(f"  [{i:>2}] {v.name:<55} {size}{cov_str}")


# ──────────────────────────────────────────────────────────────────────
# Diagnose bei Fehlern
# ──────────────────────────────────────────────────────────────────────
def _today_log(name: str) -> Path:
    return LOG_DIR / time.strftime("%Y-%m-%d") / f"{name}.log"


def _tail_file(path: Path, lines: int = 30) -> str:
    if not path.exists():
        return f"(Logdatei nicht gefunden: {path})"
    try:
        data = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(data[-lines:])
    except Exception as e:
        return f"(Konnte Logfile nicht lesen: {e})"


def _print_failure_diagnostics(prefix: str = "") -> None:
    """Druckt einen ausführlichen Fehler-Bericht — wird bei jedem Fehlschlag aufgerufen."""
    try:
        from core.workers.facebook import fb_service
        clients_total = len(getattr(fb_service, "_clients", set()) or [])
        ready_total = len(getattr(fb_service, "_ready_clients", set()) or [])
        srv_alive = fb_service.is_server_running() if hasattr(fb_service, "is_server_running") else "?"
    except Exception as e:
        clients_total = ready_total = "?"
        srv_alive = f"(import fehlgeschlagen: {e})"

    print()
    print(_red("─" * 64))
    print(_red(_bold(f"❌ DIAGNOSE {prefix}")))
    print(_red("─" * 64))
    print(f"  fb_service WS-Server läuft : {srv_alive}")
    print(f"  Verbundene Clients         : {clients_total}")
    print(f"  Davon Handshake-fertig     : {ready_total}")
    fb_log = _today_log("fb_service")
    test_log = _today_log("fb_reel_test")
    print(f"  Logdatei fb_service        : {fb_log}")
    print(f"  Logdatei fb_reel_test      : {test_log}")
    print()
    print(_yellow("── Letzte 30 Zeilen aus fb_service.log ──"))
    print(_grey(_tail_file(fb_log, lines=30)))
    print(_red("─" * 64))


# ──────────────────────────────────────────────────────────────────────
# Aktion 1: Facebook-Reel-Test
# ──────────────────────────────────────────────────────────────────────
def action_facebook_reel(
    video_arg: Optional[str],
    title: Optional[str],
    url: Optional[str],
    coupon: Optional[str],
    handshake_timeout: int,
    post_timeout: int,
    interactive: bool,
    skip_chrome: bool = False,
) -> int:
    """Sendet ein Video aus media/videos/queue als Reel ans FB-Addon."""
    from core.workers.facebook import fb_service

    videos = _list_videos()
    if not videos:
        log.error(f"Keine .mp4 in {VIDEO_DIR} gefunden.")
        return 2

    # --- Video auflösen ---
    video = _resolve_video(video_arg) if video_arg else None
    if video is None:
        if not interactive:
            log.error(f"Video '{video_arg}' konnte nicht aufgelöst werden.")
            _print_video_table(videos, "Verfügbare Videos")
            return 2
        _print_video_table(videos)
        print(f"  [{len(videos) + 1:>2}] {_red('Abbrechen')}")
        while True:
            raw = input(_cyan(f"Welches Video senden? [1-{len(videos) + 1}] (Enter=1): ")).strip()
            if not raw:
                pick = 1
            else:
                try:
                    pick = int(raw)
                except ValueError:
                    print(_red("  → Bitte eine Zahl."))
                    continue
            if pick == len(videos) + 1:
                print("Abgebrochen.")
                return 0
            if 1 <= pick <= len(videos):
                video = videos[pick - 1]
                break
            print(_red(f"  → Zwischen 1 und {len(videos) + 1}."))

    cover = _find_cover(video)
    size_mb = video.stat().st_size / (1024 * 1024)

    # --- Fallback-Werte ---
    title         = title or video.stem
    affiliate_url = url   or "https://www.amazon.de/dp/B0TEST"
    coupon        = coupon or ""

    # --- Banner ---
    print()
    print(_bold("─" * 64))
    print(_bold(f"  🎬  Reel-Test"))
    print(_bold("─" * 64))
    print(f"  Video       : {_cyan(str(video))}")
    print(f"  Größe       : {size_mb:.1f} MB")
    print(f"  Cover       : {(_green(str(cover)) if cover else _yellow('(keins)'))}")
    print(f"  Titel       : {title}")
    print(f"  URL         : {affiliate_url}")
    print(f"  Coupon      : {coupon or _grey('(leer)')}")
    print(f"  Handshake-Timeout : {handshake_timeout}s")
    print(f"  Post-Timeout      : {post_timeout}s  ({post_timeout // 60} min)")
    print(_bold("─" * 64))

    # --- Daten-Dict (wie im Produktivpfad) ---
    data = {
        "type": "reel",  # WICHTIG für fb_message.create_facebook_message
        "title": title,
        "affiliate_url": affiliate_url,
        "url": affiliate_url,
        "coupon": {"code": coupon} if coupon else None,
        "hashtags": ["#Test", "#Reel", "#Scraper", "#DealsBoss"],
    }

    # --- 1) WebSocket-Server starten ---
    print()
    log.info("Starte fb_service WebSocket-Server …")
    try:
        fb_service.init()
    except Exception:
        log.error("init() fehlgeschlagen:\n" + traceback.format_exc())
        _print_failure_diagnostics("(beim init)")
        return 3

    # --- 2) Chrome mit Facebook-Profil + Extension hochziehen ---
    #         Exakt dieselben Env-Vars + ChromeProfile-Aufruf wie in
    #         core/workers/facebook/fb_watcher.run_init_phase().
    if not skip_chrome:
        try:
            time.sleep(1)  # kurz warten bis WS-Thread offen ist
            from core.workers.chrome_launcher import ChromeProfile

            fb_profile_name = os.environ.get("FACEBOOK_CHROME_PROFILE", "facebook")
            fb_addon_dir    = os.environ.get("FACEBOOK_ADDON_DIR", "addons/facebook")
            fb_start_url    = os.environ.get("FACEBOOK_START_URL", "https://www.facebook.com/")
            log.info(
                f"🧭 Starte Chrome | Profil='{fb_profile_name}' | "
                f"Addon='{fb_addon_dir}' | URL={fb_start_url}"
            )
            fb_chrome = ChromeProfile(fb_profile_name, addons=[fb_addon_dir])
            launched = fb_chrome.launch_if_needed(start_url=fb_start_url)
            if not launched:
                log.warning(
                    "Chrome konnte nicht gestartet werden — falls Chrome bereits "
                    "mit diesem Profil offen ist, ist das OK."
                )
        except Exception:
            log.error("Chrome-Start fehlgeschlagen:\n" + traceback.format_exc())
            log.warning("Versuche trotzdem auf eine bereits laufende Extension zu warten …")
    else:
        log.info("⏭️  --no-chrome aktiv: kein Chrome-Start, "
                 "es wird nur auf eine bestehende Extension gewartet.")

    # --- 3) Auf Addon-Handshake warten ---
    log.info(f"Warte auf Addon-Handshake (max. {handshake_timeout}s) …")
    if not fb_service.ensure_connected(timeout=handshake_timeout):
        log.error("Kein Facebook-Addon verbunden.")
        print(_yellow("  → Tipp: prüfe, ob Chrome offen ist und du bei Facebook eingeloggt bist."))
        print(_yellow("  → Einmaliger Profil-Login:  python run_all.py --setup-profiles facebook"))
        _print_failure_diagnostics("(beim Handshake)")
        return 3

    # --- 4) Senden ---
    print()
    log.info(f"📤 Sende Reel an Addon … (warte bis zu {post_timeout}s auf Bestätigung)")
    t0 = time.time()
    ok = False
    err: Optional[BaseException] = None
    try:
        ok = asyncio.run(
            fb_service.send_post(
                data,
                local_image_path=str(cover) if cover else None,
                local_video_path=str(video),
            )
        )
    except BaseException as e:
        err = e
        log.error("send_post() warf eine Exception:\n" + traceback.format_exc())
    dt = time.time() - t0

    print()
    if ok:
        log.info(_green(f"✅ Reel erfolgreich gepostet ({dt:.1f}s, {dt / 60:.1f} min)."))
        return 0

    if err is not None:
        log.error(f"Reel-Post abgebrochen nach {dt:.1f}s — {type(err).__name__}: {err}")
    else:
        log.error(f"Reel-Post fehlgeschlagen nach {dt:.1f}s "
                  f"(send_post() → False; Details im Log).")
    _print_failure_diagnostics()
    return 4


# ──────────────────────────────────────────────────────────────────────
# Aktion 2: Creatomate-Render-Test
# ──────────────────────────────────────────────────────────────────────
def action_creatomate_render(
    title: Optional[str],
    image: Optional[str],
    price: Optional[str],
    old_price: Optional[str],
    discount: Optional[str],
) -> int:
    """Rendert ein Demo-Deal als typ3_audio-Template via Creatomate."""
    from core.workers.facebook import reels_service

    title    = title    or "Anker SoundCore Bluetooth-Kopfhörer"
    image    = image    or "https://m.media-amazon.com/images/I/61T3p7Pq5yL._AC_SL1500_.jpg"
    price    = price    or "29,99 €"
    old_price = old_price or "59,99 €"
    discount = discount or "50%"

    print()
    log.info(_bold("🎬 Creatomate-Render-Test (typ3_audio)"))
    print(f"  Titel       : {title}")
    print(f"  Bild        : {image}")
    print(f"  Preis jetzt : {price}")
    print(f"  Preis alt   : {old_price}")
    print(f"  Discount    : {discount}")

    data = {
        "title": title,
        "description": "Premium-Sound, 40h Akku, IPX5",
        "images": [image],
        "image_url": image,
        "price": {"raw": price},
        "original_price": {"raw": old_price},
        "discount_percent": discount,
        "affiliate_url": "https://www.amazon.de/dp/B0TEST",
        "reel_caption": "🔥 Discount Alert",
    }

    try:
        result = reels_service.render_typ3_audio(data)
    except Exception as e:
        log.error("Render fehlgeschlagen:\n" + traceback.format_exc())
        print(_red(f"❌ {e}"))
        return 5

    print()
    log.info(_green("✅ Render fertig!"))
    print(f"   ID    : {result.get('id')}")
    print(f"   URL   : {_cyan(str(result.get('url')))}")
    print(f"   Width : {result.get('width')}x{result.get('height')}")
    print(f"   Frames: {result.get('frame_count')}")
    return 0


# ──────────────────────────────────────────────────────────────────────
# Argparse + Menü
# ──────────────────────────────────────────────────────────────────────
def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="fb_reel_test",
        description="Test-CLI für Facebook-Reel-Posting + Creatomate-Render.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Beispiele:\n"
            "  python test/fb_reel_test.py                      # interaktiv\n"
            "  python test/fb_reel_test.py --list               # nur auflisten\n"
            "  python test/fb_reel_test.py --video 1            # 1. Video direkt\n"
            "  python test/fb_reel_test.py --video B0CSG46SSN.mp4\n"
            "  python test/fb_reel_test.py --video 2 --title 'Mein Test' \\\n"
            "                              --url https://amzn.eu/d/xxx --coupon RABATT10\n"
            "  python test/fb_reel_test.py --action creatomate  # nur Creatomate\n"
            "  python test/fb_reel_test.py --video 1 --verbose  # DEBUG-Logs\n"
        ),
    )
    p.add_argument(
        "--action", choices=["facebook", "creatomate", "menu"], default="menu",
        help="Welcher Test? (default: menu — interaktive Auswahl). "
             "facebook = Reel ans Addon senden, creatomate = nur API rendern.",
    )
    p.add_argument(
        "--video", type=str, default=None,
        help="Video-Auswahl. Akzeptiert: Index (1,2,3 …), Dateiname "
             "(B0CSG46SSN.mp4), Pfad-Substring oder absoluter Pfad.",
    )
    p.add_argument("--title",   default=None, help="Titel für den FB-Text.")
    p.add_argument("--url",     default=None, help="Affiliate-/Produkt-URL.")
    p.add_argument("--coupon",  default=None, help="Gutscheincode (optional).")
    p.add_argument(
        "--handshake-timeout", type=int, default=120,
        help="Max. Sekunden warten, bis Addon connected ist (default: 120).",
    )
    p.add_argument(
        "--post-timeout", type=int, default=1800,
        help="Max. Sekunden warten, bis Addon Erfolg meldet (default: 1800 = 30 min).",
    )
    p.add_argument(
        "--no-chrome", action="store_true",
        help="Chrome NICHT automatisch starten. Standard: Chrome wird wie im "
             "Produktivbetrieb (fb_watcher.run_init_phase) mit dem "
             "Facebook-Profil + Addon hochgefahren.",
    )
    p.add_argument("--list", action="store_true",
                   help="Nur Videos auflisten und beenden.")
    # Creatomate-spezifisch
    p.add_argument("--cm-image",     default=None, help="(creatomate) Produktbild-URL.")
    p.add_argument("--cm-price",     default=None, help="(creatomate) Preis 'jetzt'.")
    p.add_argument("--cm-old-price", default=None, help="(creatomate) Preis 'vorher'.")
    p.add_argument("--cm-discount",  default=None, help="(creatomate) Rabatt-Prozent.")
    # Diagnose
    p.add_argument("-v", "--verbose", action="store_true",
                   help="DEBUG-Level für alle Logger.")
    return p


def _menu(args: argparse.Namespace) -> int:
    """Klassisches Menü, wenn keine konkrete Aktion angegeben wurde."""
    print(_bold("═" * 60))
    print(_bold(_cyan("  🧪  FACEBOOK / REELS TEST")))
    print(_bold("═" * 60))
    print(f"  Video-Queue : {VIDEO_DIR}")
    print(f"  Cwd         : {Path.cwd()}")
    print()
    print(_bold("Was möchtest du testen?"))
    print("  [1] Facebook-Reel an Addon senden (echte Veröffentlichung)")
    print("  [2] Creatomate-Render-Test (nur API, kein FB-Posting)")
    print("  [3] Beenden")
    while True:
        raw = input(_cyan("Auswahl [1-3] (Enter=1): ")).strip() or "1"
        if raw in ("1", "2", "3"):
            break
        print(_red("  → Bitte 1, 2 oder 3."))
    if raw == "3":
        return 0
    if raw == "1":
        return action_facebook_reel(
            args.video, args.title, args.url, args.coupon,
            args.handshake_timeout, args.post_timeout,
            interactive=True,
            skip_chrome=args.no_chrome,
        )
    return action_creatomate_render(
        args.title, args.cm_image, args.cm_price, args.cm_old_price, args.cm_discount,
    )


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    setup_live_console_logging(verbose=args.verbose)

    if args.list:
        _print_video_table(_list_videos())
        return 0

    if args.action == "creatomate":
        return action_creatomate_render(
            args.title, args.cm_image, args.cm_price, args.cm_old_price, args.cm_discount,
        )

    if args.action == "facebook":
        return action_facebook_reel(
            args.video, args.title, args.url, args.coupon,
            args.handshake_timeout, args.post_timeout,
            interactive=False,
            skip_chrome=args.no_chrome,
        )

    # action == 'menu'  →  wenn --video angegeben wurde, direkt FB-Aktion ohne Menü.
    if args.video:
        return action_facebook_reel(
            args.video, args.title, args.url, args.coupon,
            args.handshake_timeout, args.post_timeout,
            interactive=False,
            skip_chrome=args.no_chrome,
        )
    return _menu(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print()
        print(_yellow("Abgebrochen durch Benutzer."))
        sys.exit(130)
    except SystemExit:
        raise
    except BaseException:
        # Letztes Auffangnetz — IMMER mit Stacktrace beenden, kein stummer Exit.
        print()
        print(_red("─" * 64))
        print(_red(_bold("❌ Unbehandelter Fehler in fb_reel_test:")))
        print(_red("─" * 64))
        traceback.print_exc()
        sys.exit(1)
