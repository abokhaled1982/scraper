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
ARGS = _arg_parser.parse_args()
MODE = ARGS.mode

# ----------------------------------------------------------
# Initial Setup & Pfade
# ----------------------------------------------------------
HERE = Path(__file__).parent.resolve()
AMAZON = HERE / "amazon"
PY = sys.executable  # Aktuelles venv-Python

# Projekt-Root in sys.path aufnehmen
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

# ----------------------------------------------------------
# .env laden
# ----------------------------------------------------------
load_dotenv()

SESSION_DIR = os.getenv("SESSION_DIR", ".sessions")

# Telegram-Konfiguration + Import nur im full-Modus
if MODE == "full":
    try:
        from telegram.login_once import LoginConfig, ensure_both_sessions_sequential
    except ImportError:
        print("❌ Fehler: login_once.py konnte nicht gefunden werden.")
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
    (HERE / "data" / "inbox").mkdir(parents=True, exist_ok=True)
    (HERE / "data" / "produckt").mkdir(parents=True, exist_ok=True)
    (HERE / "data" / "out").mkdir(parents=True, exist_ok=True)
    (HERE / SESSION_DIR).mkdir(parents=True, exist_ok=True)
    (HERE / "assets").mkdir(parents=True, exist_ok=True)

async def spawn(name: str, *argv: str, env: Optional[Dict[str, str]] = None):
    print(f"[supervisor] spawn {name}: {' '.join(argv)}")
    return await asyncio.create_subprocess_exec(
        *argv,
        env={**os.environ, **(env or {})},
    )

async def terminate(proc: asyncio.subprocess.Process | None, name: str, timeout: float = 5.0):
    if not proc or proc.returncode is not None:
        return
    print(f"[supervisor] terminate {name}")
    try:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            print(f"[supervisor] kill {name}")
            proc.kill()
            await asyncio.wait_for(proc.wait(), timeout=timeout)
    except ProcessLookupError:
        pass

# ----------------------------------------------------------
# Sequentieller Telegram Login (nur full-Modus)
# ----------------------------------------------------------
def print_login_step(msg: str):
    print(f"[Telegram Login] {msg}")

async def do_telegram_login_check():
    print("\n--- Starte sequentiellen Telegram-Login-Check (4 Sessions) ---")

    router_cfg   = LoginConfig(API_ID, API_HASH, ROUTER_NAME,   SESSION_DIR, PHONE, PASSWORD)
    observer_cfg = LoginConfig(API_ID, API_HASH, OBSERVER_NAME, SESSION_DIR, PHONE, PASSWORD)
    sender_cfg   = LoginConfig(API_ID, API_HASH, SENDER_NAME,   SESSION_DIR, PHONE, PASSWORD)
    piraten_cfg  = LoginConfig(API_ID, API_HASH, PIRATEN_NAME,  SESSION_DIR, PHONE, PASSWORD)

    ok1, ok2, ok3, ok4 = await ensure_both_sessions_sequential(
        router_cfg, observer_cfg, sender_cfg, piraten_cfg, on_step=print_login_step
    )

    if not (ok1 and ok2 and ok3 and ok4):
        raise SystemExit("❌ Einer der 4 Telegram-Logins fehlgeschlagen. Abbruch.")

    print("✅ Alle Telegram-Sessions (Router, Obs, Sender, Piraten) bereit.")
    print("---------------------------------------------------\n")

# ----------------------------------------------------------
# Main Supervisor
# ----------------------------------------------------------
async def main():
    os.chdir(HERE)
    _ensure_dirs()

    if MODE == "parser":
        print("[supervisor] Modus: parser — nur Amazon-Pipeline (kein Telegram, kein Facebook)")
        await _run_parser_only()
    else:
        print("[supervisor] Modus: full — alle Services")
        await _run_full()


async def _run_parser_only():
    """Startet nur den Amazon-Pipeline-Stack."""
    ws_server      = await spawn("ws_server",      PY, str(AMAZON / "ws_server.py"))
    deals_watcher  = await spawn("deals_watcher",  PY, str(AMAZON / "watcher.py"))
    product_opener = await spawn("product_opener", PY, str(AMAZON / "product_opener.py"))
    product_parser = await spawn("product_parser", PY, str(AMAZON / "product_parser.py"))

    procs: List[Tuple[str, asyncio.subprocess.Process]] = [
        ("ws_server",      ws_server),
        ("deals_watcher",  deals_watcher),
        ("product_opener", product_opener),
        ("product_parser", product_parser),
    ]
    for n, p in procs:
        print(f"[supervisor] started {n} (pid={p.pid})")

    await _wait_and_shutdown(procs)


async def _run_full():
    """Startet alle Services inkl. Telegram-Login, Facebook und Telegram-Clients."""
    # 1. Telegram Login
    await do_telegram_login_check()

    # 2. Alle Services starten
    ws_server      = await spawn("ws_server",      PY, str(AMAZON / "ws_server.py"))
    deals_watcher  = await spawn("deals_watcher",  PY, str(AMAZON / "watcher.py"))
    product_opener = await spawn("product_opener", PY, str(AMAZON / "product_opener.py"))
    product_parser = await spawn("product_parser", PY, str(AMAZON / "product_parser.py"))
    fb_watcher     = await spawn("fb_watcher",     PY, "-m", "facebook.fb_watcher")
    tel_router     = await spawn("telegram_router",   PY, "-m", "telegram.telRouter")
    tel_observer   = await spawn("telegram_observer", PY, "-m", "telegram.telObserver")
    tel_sender     = await spawn("telegram_sender",   PY, "-m", "telegram.telSender")
    tel_piraten    = await spawn("telegram_piraten",  PY, "-m", "telegram.telObserver_piraten")

    procs: List[Tuple[str, asyncio.subprocess.Process]] = [
        ("ws_server",          ws_server),
        ("deals_watcher",      deals_watcher),
        ("product_opener",     product_opener),
        ("product_parser",     product_parser),
        ("fb_watcher",         fb_watcher),
        ("telegram_router",    tel_router),
        ("telegram_observer",  tel_observer),
        ("telegram_sender",    tel_sender),
        ("telegram_piraten",   tel_piraten),
    ]
    for n, p in procs:
        print(f"[supervisor] started {n} (pid={p.pid})")

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
        print(f"[supervisor] process {name} exited with code {code}; stopping others …")
    else:
        print("[supervisor] stop requested; shutting down …")

    for name, proc in reversed(procs):
        await terminate(proc, name)
    print("[supervisor] all stopped")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        print(f"❌ Critical Error in main runner: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n[supervisor] Abgebrochen durch Benutzer.")