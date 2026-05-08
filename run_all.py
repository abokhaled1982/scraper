#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_all.py – Supervisor
Startet:
  1. Sequenzieller Telegram-Login-Check -> Asynchron/Sequenziell
  2. Alle Amazon-Worker und Telegram-Clients -> Parallel
"""

from __future__ import annotations
import os
import sys
import asyncio
import signal
from pathlib import Path
from typing import Optional, Dict, List, Tuple
from dotenv import load_dotenv

# ----------------------------------------------------------
# Initial Setup & Pfade
# ----------------------------------------------------------
HERE = Path(__file__).parent.resolve()
AMAZON = HERE / "amazon"
PY = sys.executable  # Aktuelles venv-Python

# Projekt-Root in sys.path aufnehmen
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

try:
    from telegram.login_once import LoginConfig, ensure_both_sessions_sequential
except ImportError:
    print("❌ Fehler: login_once.py konnte nicht gefunden werden. Bitte sicherstellen, dass sie im Projekt-Root liegt.")
    sys.exit(1)

# ----------------------------------------------------------
# .env laden
# ----------------------------------------------------------
load_dotenv()

# 🔸 Telegram Konfiguration
API_ID           = int(os.getenv("API_ID", "0"))
API_HASH         = os.getenv("API_HASH", "")
SESSION_DIR      = os.getenv("SESSION_DIR", ".sessions")
PHONE            = os.getenv("TELEGRAM_PHONE")
PASSWORD         = os.getenv("TELEGRAM_PASSWORD")

ROUTER_NAME      = os.getenv("SESSION_NAME", "main_session")
OBSERVER_NAME    = os.getenv("OBS_SESSION_NAME", "observer_session")
SENDER_NAME      = os.getenv("OBS_SEND_OBSERVER_NAME", "observer_sender_session")
ROUTER_CHANNEL   = os.getenv("CHANNEL_INVITE_URL", "").strip()
OBSERVER_CHANNEL = os.getenv("OBS_CHANNEL_INVITE_URL", "").strip()

# Piraten Vars
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
# Sequentieller Telegram Login
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

    # 1. 🔑 Sequentieller Login-Check
    await do_telegram_login_check()

    # 2. 🟢 Services starten (Parallel)

    # Amazon Services
    ws_server      = await spawn("ws_server",      PY, str(AMAZON / "ws_server.py"))
    deals_watcher  = await spawn("deals_watcher",  PY, str(AMAZON / "watcher.py"))
    product_opener = await spawn("product_opener", PY, str(AMAZON / "product_opener.py"))
    product_parser = await spawn("product_parser", PY, str(AMAZON / "product_parser.py"))

    # Facebook Services (Post + Reels über einen Watcher)
    fb_watcher     = await spawn("fb_watcher",     PY, "-m", "facebook.fb_watcher")

    # Telegram Services
    tel_router   = await spawn("telegram_router",   PY, "-m", "telegram.telRouter")
    tel_observer = await spawn("telegram_observer", PY, "-m", "telegram.telObserver")
    tel_sender   = await spawn("telegram_sender",   PY, "-m", "telegram.telSender")
    tel_piraten  = await spawn("telegram_piraten",  PY, "-m", "telegram.telObserver_piraten")

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

    # Signal-Handling (Linux-kompatibel via asyncio loop)
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

    # 3. 🛑 Geordnet beenden
    await terminate(tel_piraten,    "telegram_piraten")
    await terminate(tel_observer,   "telegram_observer")
    await terminate(tel_router,     "telegram_router")
    await terminate(tel_sender,     "telegram_sender")
    await terminate(reels_watcher,  "reels_watcher")
    await terminate(fb_watcher,     "fb_watcher")
    await terminate(product_parser, "product_parser")
    await terminate(product_opener, "product_opener")
    await terminate(deals_watcher,  "deals_watcher")
    await terminate(ws_server,      "ws_server")
    print("[supervisor] all stopped")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        print(f"❌ Critical Error in main runner: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n[supervisor] Abgebrochen durch Benutzer.")