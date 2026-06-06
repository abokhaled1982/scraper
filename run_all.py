#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_all.py – Supervisor

Modi (--mode):
  full    (Standard) – alles: Amazon-Pipeline + Facebook + Telegram
  parser             – nur Amazon-Pipeline (ws_server, watcher, opener, parser)
                       kein Telegram-Login, kein Facebook

Beispiele:
  python run_all.py                  # full
  python run_all.py --mode parser    # nur AI-Parser-Stack
"""

from __future__ import annotations
import argparse
import os
import sys
import asyncio
import signal
from pathlib import Path
from typing import Optional, Dict, List, Tuple
from dotenv import load_dotenv

# ----------------------------------------------------------
# Args frühzeitig parsen (vor bedingten Imports)
# ----------------------------------------------------------
_arg_parser = argparse.ArgumentParser(add_help=True)
_arg_parser.add_argument(
    "--mode",
    choices=["full", "parser"],
    default="full",
    help="'full' startet alles; 'parser' startet nur den Amazon-Pipeline-Stack.",
)
_arg_parser.add_argument(
    "--tui",
    action="store_true",
    help="Zeigt eine kompakte Live-Konsole (statisches Layout) statt scrollender Logs.",
)
_arg_parser.add_argument(
    "--setup-profiles",
    nargs="?",
    const="all",
    choices=["all", "facebook", "amazon"],
    default=None,
    help="Einmaliger Profil-Login: \u00f6ffnet Chrome mit Worker-Profil + Addon, "
         "damit du dich einloggen kannst. Beendet sich danach.",
)
ARGS = _arg_parser.parse_args()
MODE = ARGS.mode
USE_TUI = ARGS.tui
SETUP_TARGET = ARGS.setup_profiles

# ----------------------------------------------------------
# Initial Setup & Pfade
# ----------------------------------------------------------
HERE = Path(__file__).parent.resolve()
PY = sys.executable  # Aktuelles venv-Python

# Projekt-Root in sys.path aufnehmen
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
from core.logging import get_logger  # noqa: E402
log = get_logger("run_all")  # noqa: E402

# ----------------------------------------------------------
# .env laden
# ----------------------------------------------------------
load_dotenv()

SESSION_DIR = os.getenv("SESSION_DIR", ".sessions")

# Telegram-Konfiguration + Import nur im full-Modus
if MODE == "full" and not SETUP_TARGET:
    try:
        from core.workers.telegram.login_once import LoginConfig, ensure_both_sessions_sequential
    except ImportError:
        log.error("❌ Fehler: login_once.py konnte nicht gefunden werden.")
        sys.exit(1)

    API_ID           = int(os.getenv("API_ID", "0"))
    API_HASH         = os.getenv("API_HASH", "")
    PHONE            = os.getenv("TELEGRAM_PHONE")
    PASSWORD         = os.getenv("TELEGRAM_PASSWORD")
    ROUTER_NAME      = os.getenv("SESSION_NAME", "main_session")
    OBSERVER_NAME    = os.getenv("OBS_SESSION_NAME", "observer_session")
    SENDER_NAME      = os.getenv("OBS_SEND_OBSERVER_NAME", "observer_sender_session")
    ROUTER_CHANNEL   = os.getenv("CHANNEL_INVITE_URL", "").strip()
    OBSERVER_CHANNEL = os.getenv("OBS_CHANNEL_INVITE_URL", "").strip()
    PIRATEN_NAME     = os.getenv("PIRATEN_SESSION_NAME", "piraten_session")
    PIRATEN_CHANNEL  = os.getenv("PIRATEN_CHANNEL_INVITE_URL", "").strip()

    if not all([API_ID, API_HASH, ROUTER_CHANNEL, OBSERVER_CHANNEL]):
        raise SystemExit("Fehler: Mindestens eine Telegram-Variable (API_ID, HASH, CHANNEL_INVITE_URL, OBS_CHANNEL_INVITE_URL) fehlt in .env.")

# ----------------------------------------------------------
# Supervisor Utilities
# ----------------------------------------------------------
def _ensure_dirs():
    from core.paths import ensure_directories
    ensure_directories()
    (HERE / SESSION_DIR).mkdir(parents=True, exist_ok=True)

async def spawn(name: str, *argv: str, env: Optional[Dict[str, str]] = None):
    log.info(f"[supervisor] spawn {name}: {' '.join(argv)}")
    # Stelle sicher, dass Worker-Subprozesse das Projekt-Root in PYTHONPATH haben,
    # damit `from core.logging import get_logger` immer funktioniert –
    # auch wenn der Worker direkt mit absolutem Pfad gestartet wird.
    existing_pp = os.environ.get("PYTHONPATH", "")
    pythonpath = str(HERE) + (os.pathsep + existing_pp if existing_pp else "")
    return await asyncio.create_subprocess_exec(
        *argv,
        env={**os.environ, "PYTHONPATH": pythonpath, **(env or {})},
    )

async def terminate(proc: asyncio.subprocess.Process | None, name: str, timeout: float = 5.0):
    if not proc or proc.returncode is not None:
        return
    log.info(f"[supervisor] terminate {name}")
    try:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            log.info(f"[supervisor] kill {name}")
            proc.kill()
            await asyncio.wait_for(proc.wait(), timeout=timeout)
    except ProcessLookupError:
        pass


# ----------------------------------------------------------
# Core-Services (Logger + Dashboard + DB-Init)
# ----------------------------------------------------------
async def _start_core_services() -> List[Tuple[str, asyncio.subprocess.Process]]:
    """
    Startet zuerst Logger-Server, dann Dashboard. Initialisiert die DB.
    Liefert die Liste der gestarteten Prozesse, damit der Supervisor sie
    beim Shutdown ebenfalls beendet.
    """
    # DB einmalig initialisieren (Tabellen anlegen, falls noch nicht vorhanden)
    try:
        from core.db import init_db
        init_db()
        log.info("[supervisor] core.db initialized")
    except Exception as e:
        log.error(f"[supervisor] WARN: core.db init failed: {e}")

    logger_module = "core.logging.tui_server" if USE_TUI else "core.logging.server"
    logger_proc = await spawn("logger", PY, "-m", logger_module)
    # kurz warten, damit der TCP-Port wirklich offen ist, bevor Worker starten
    await asyncio.sleep(0.5)
    dash_proc = await spawn("dashboard", PY, "-m", "core.dashboard")

    return [("logger", logger_proc), ("dashboard", dash_proc)]

# ----------------------------------------------------------
# Sequentieller Telegram Login (nur full-Modus)
# ----------------------------------------------------------
def print_login_step(msg: str):
    log.info(f"[Telegram Login] {msg}")

async def do_telegram_login_check():
    log.info("\n--- Starte sequentiellen Telegram-Login-Check (4 Sessions) ---")

    router_cfg   = LoginConfig(API_ID, API_HASH, ROUTER_NAME,   SESSION_DIR, PHONE, PASSWORD)
    observer_cfg = LoginConfig(API_ID, API_HASH, OBSERVER_NAME, SESSION_DIR, PHONE, PASSWORD)
    sender_cfg   = LoginConfig(API_ID, API_HASH, SENDER_NAME,   SESSION_DIR, PHONE, PASSWORD)
    piraten_cfg  = LoginConfig(API_ID, API_HASH, PIRATEN_NAME,  SESSION_DIR, PHONE, PASSWORD)

    ok1, ok2, ok3, ok4 = await ensure_both_sessions_sequential(
        router_cfg, observer_cfg, sender_cfg, piraten_cfg, on_step=print_login_step
    )

    if not (ok1 and ok2 and ok3 and ok4):
        raise SystemExit("❌ Einer der 4 Telegram-Logins fehlgeschlagen. Abbruch.")

    log.info("✅ Alle Telegram-Sessions (Router, Obs, Sender, Piraten) bereit.")
    log.info("---------------------------------------------------\n")

# ----------------------------------------------------------
# Main Supervisor
# ----------------------------------------------------------
def setup_profiles(target: str) -> int:
    """Einmaliger interaktiver Profil-Login (Facebook/Amazon).

    \u00d6ffnet Chrome mit dem jeweiligen Worker-Profil + geladener Extension,
    damit der Nutzer sich einloggen kann. Sessions bleiben anschlie\u00dfend
    persistent im Profil-Ordner.
    """
    import subprocess
    from core.workers.chrome_launcher import (
        ChromeProfile,
        _resolve_chrome_bin,
        _resolve_addon_paths,
    )

    targets = {
        "facebook": (
            os.environ.get("FACEBOOK_CHROME_PROFILE", "facebook"),
            os.environ.get("FACEBOOK_ADDON_DIR", "addons/facebook"),
            os.environ.get("FACEBOOK_START_URL", "https://www.facebook.com/"),
        ),
        "amazon": (
            os.environ.get("AMAZON_CHROME_PROFILE", "amazon"),
            os.environ.get("AMAZON_ADDON_DIR", "addons/proudct_parser"),
            os.environ.get("AMAZON_START_URL", "https://www.amazon.de/"),
        ),
    }
    order = ["facebook", "amazon"] if target == "all" else [target]

    try:
        chrome_bin = _resolve_chrome_bin()
    except FileNotFoundError as e:
        log.error(str(e))
        return 1

    for key in order:
        name, addon_rel, url = targets[key]
        prof = ChromeProfile(name, addons=[addon_rel])
        addon_abs = _resolve_addon_paths([addon_rel])
        if not addon_abs:
            log.error(f"[setup] Addon nicht gefunden: {addon_rel}")
            return 2

        log.info("\n" + "=" * 60)
        log.info(f"\u25b6  Setup f\u00fcr Profil: {name}")
        log.info(f"   Profile-Dir : {prof.user_data_dir}")
        log.info(f"   Extension   : {addon_abs[0]}")
        log.info(f"   URL         : {url}")
        log.info("=" * 60)
        log.info("\u23f3  Bitte einloggen / Berechtigungen erteilen,")
        log.info("   danach das Fenster SCHLIESSEN, um zum n\u00e4chsten Schritt zu kommen.\n")

        cmd = [
            chrome_bin,
            f"--user-data-dir={prof.user_data_dir}",
            f"--load-extension={addon_abs[0]}",
            "--no-first-run",
            "--no-default-browser-check",
            url,
        ]
        try:
            subprocess.run(cmd, check=False)
        except KeyboardInterrupt:
            log.info("[setup] Abgebrochen.")
            return 130

    log.info("\n\u2705 Setup abgeschlossen. Du kannst jetzt 'python run_all.py' starten.")
    return 0


async def main():
    os.chdir(HERE)
    _ensure_dirs()

    if MODE == "parser":
        log.info("[supervisor] Modus: parser — nur Amazon-Pipeline (kein Telegram, kein Facebook)")
        await _run_parser_only()
    else:
        log.info("[supervisor] Modus: full — alle Services")
        await _run_full()


async def _run_parser_only():
    """Startet nur den Amazon-Pipeline-Stack."""
    # 0. Core: Logger + Dashboard (Foundation)
    procs: List[Tuple[str, asyncio.subprocess.Process]] = []
    procs += await _start_core_services()

    ws_server      = await spawn("ws_server",      PY, "-m", "core.workers.amazon.ws_server")
    deals_watcher  = await spawn("deals_watcher",  PY, "-m", "core.workers.amazon.watcher")
    product_opener = await spawn("product_opener", PY, "-m", "core.workers.amazon.product_opener")
    product_parser = await spawn("product_parser", PY, "-m", "core.workers.amazon.product_parser")

    procs += [
        ("ws_server",      ws_server),
        ("deals_watcher",  deals_watcher),
        ("product_opener", product_opener),
        ("product_parser", product_parser),
    ]
    for n, p in procs:
        log.info(f"[supervisor] started {n} (pid={p.pid})")

    await _wait_and_shutdown(procs)


async def _run_full():
    """Startet alle Services inkl. Telegram-Login, Facebook und Telegram-Clients."""
    # 0. Core: Logger + Dashboard (Foundation) – muss als erstes laufen
    procs: List[Tuple[str, asyncio.subprocess.Process]] = []
    procs += await _start_core_services()

    # 1. Telegram Login
    await do_telegram_login_check()

    # 2. Instagram deaktiviert
    ig_session_ok = False  # Instagram ist deaktiviert

    # 3. Alle Services starten
    ws_server      = await spawn("ws_server",      PY, "-m", "core.workers.amazon.ws_server")
    deals_watcher  = await spawn("deals_watcher",  PY, "-m", "core.workers.amazon.watcher")
    product_opener = await spawn("product_opener", PY, "-m", "core.workers.amazon.product_opener")
    product_parser = await spawn("product_parser", PY, "-m", "core.workers.amazon.product_parser")
    fb_watcher     = await spawn("fb_watcher",     PY, "-m", "core.workers.facebook.fb_watcher")
    ig_watcher     = None  # Instagram deaktiviert
    tel_router     = await spawn("telegram_router",   PY, "-m", "core.workers.telegram.telRouter")
    tel_observer   = await spawn("telegram_observer", PY, "-m", "core.workers.telegram.telObserver")
    tel_sender     = await spawn("telegram_sender",   PY, "-m", "core.workers.telegram.telSender")
    tel_piraten    = await spawn("telegram_piraten",  PY, "-m", "core.workers.telegram.telObserver_piraten")

    procs += [
        ("ws_server",          ws_server),
        ("deals_watcher",      deals_watcher),
        ("product_opener",     product_opener),
        ("product_parser",     product_parser),
        ("fb_watcher",         fb_watcher),
        *([("ig_watcher", ig_watcher)] if ig_watcher else []),
        ("telegram_router",    tel_router),
        ("telegram_observer",  tel_observer),
        ("telegram_sender",    tel_sender),
        ("telegram_piraten",   tel_piraten),
    ]
    for n, p in procs:
        log.info(f"[supervisor] started {n} (pid={p.pid})")

    await _wait_and_shutdown(procs)


async def _wait_and_shutdown(procs: List[Tuple[str, asyncio.subprocess.Process]]):
    """Wartet auf erstes Exit oder Signal, dann geordnetes Shutdown."""
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    async def wait_any():
        tasks = [asyncio.create_task(p.wait()) for _, p in procs]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finished_task = next(iter(done))
        idx = tasks.index(finished_task)
        name, proc = procs[idx]
        return name, proc.returncode

    w_task = asyncio.create_task(wait_any())
    s_task = asyncio.create_task(stop_event.wait())

    done, _ = await asyncio.wait({w_task, s_task}, return_when=asyncio.FIRST_COMPLETED)
    if w_task in done:
        name, code = await w_task
        log.info(f"[supervisor] process {name} exited with code {code}; stopping others …")
    else:
        log.info("[supervisor] stop requested; shutting down …")

    for name, proc in reversed(procs):
        await terminate(proc, name)
    log.info("[supervisor] all stopped")

if __name__ == "__main__":
    # Setup-Modus läuft synchron, ohne DB/Telegram-Init und ohne Supervisor.
    if SETUP_TARGET:
        sys.exit(setup_profiles(SETUP_TARGET))
    try:
        asyncio.run(main())
    except Exception as e:
        log.error(f"❌ Critical Error in main runner: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        log.info("\n[supervisor] Abgebrochen durch Benutzer.")