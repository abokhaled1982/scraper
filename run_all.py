#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_all.py – Supervisor
Startet:
  1. Browser-Loader (Edge + Chrome)
  2. Alle Amazon-Worker
  3. Telegram-Router (Watcher-Modus, alles über .env gesteuert)
"""

from __future__ import annotations
import os
import time
import shutil
import subprocess
import sys
import asyncio
import signal
from pathlib import Path
from typing import Optional, Dict
from dotenv import load_dotenv

# ----------------------------------------------------------
# Initial Setup & Pfade
# ----------------------------------------------------------
HERE = Path(__file__).parent.resolve()
AMAZON = HERE / "amazon"

# 🔹 .env laden
load_dotenv()

# URLs / Profile aus .env oder Defaults
URL            = os.getenv("EDGE_URL", "https://www.amazon.de/deals?ref_=nav_cs_gb")
URL2           = os.getenv("CHROME_URL", "https://www.geldhub.de/de")
EDGE_PROFILE   = os.getenv("EDGE_PROFILE", "Default")
CHROME_PROFILE = os.getenv("CHROME_PROFILE", "Profile 1")

# ----------------------------------------------------------
# Browser Loader
# ----------------------------------------------------------
def candidates_for(app_name, subpath_64, subpath_86):
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    return [
        app_name,
        str(Path(pf) / subpath_64),
        str(Path(pf86) / subpath_86),
    ]

def find_executable(candidates):
    for c in candidates:
        w = shutil.which(c)
        if w:
            return w
        p = Path(c)
        if p.is_file():
            return str(p)
    return None

edge_cands = candidates_for(
    "msedge.exe",
    r"Microsoft\Edge\Application\msedge.exe",
    r"Microsoft\Edge\Application\msedge.exe",
)
chrome_cands = candidates_for(
    "chrome.exe",
    r"Google\Chrome\Application\chrome.exe",
    r"Google\Chrome\Application\chrome.exe",
)

def run_loader_blocking():
    """Open Edge and Chrome with given profiles/URLs and wait (sleep already inside)."""
    edge_path = find_executable(edge_cands)
    chrome_path = find_executable(chrome_cands)

    if not edge_path:
        raise FileNotFoundError("❌ Edge wurde nicht gefunden – bitte Pfad prüfen.")

    print("🌐 Starte Microsoft Edge …")
    subprocess.Popen([edge_path, f"--profile-directory={EDGE_PROFILE}", URL])
    time.sleep(5)

    if not chrome_path:
        raise FileNotFoundError("❌ Chrome wurde nicht gefunden – bitte Pfad prüfen.")

    print("🌐 Starte Google Chrome …")
    subprocess.Popen([chrome_path, f"--profile-directory={CHROME_PROFILE}", URL2])

    sleep_time = int(os.getenv("BROWSER_WAIT_SECS", "30"))
    print(f"⏳ Warte {sleep_time} Sekunden …")
    time.sleep(sleep_time)
    print("✅ Browser-Loader fertig.")

# ----------------------------------------------------------
# Supervisor Utilities
# ----------------------------------------------------------
def _ensure_dirs():
    (HERE / "data" / "inbox").mkdir(parents=True, exist_ok=True)
    (HERE / "data" / "produckt").mkdir(parents=True, exist_ok=True)
    (HERE / "data" / "out").mkdir(parents=True, exist_ok=True)
    (HERE / ".sessions").mkdir(parents=True, exist_ok=True)
    (HERE / "assets").mkdir(parents=True, exist_ok=True)

async def spawn(name: str, *argv: str, env: Optional[Dict[str, str]] = None):
    print(f"[supervisor] spawn {name}: {' '.join(argv)}")
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(asyncio.subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    return await asyncio.create_subprocess_exec(
        *argv,
        creationflags=creationflags,
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
# Main Supervisor
# ----------------------------------------------------------
async def main():
    os.chdir(HERE)
    _ensure_dirs()

    print("[supervisor] running loader before services …")
    run_loader_blocking()
    print("[supervisor] loader finished, starting services …")

    # Amazon Services
    ws_server      = await spawn("ws_server",      sys.executable, str(AMAZON / "ws_server.py"))
    deals_watcher  = await spawn("deals_watcher",  sys.executable, str(AMAZON / "watcher.py"))
    product_opener = await spawn("product_opener", sys.executable, str(AMAZON / "product_opener.py"))
    product_parser = await spawn("product_parser", sys.executable, str(AMAZON / "product_parser.py"))

    # 🟢 Telegram Router – alles über .env gesteuert
    # WATCH_INTERVAL_SECS, ROUTER_MODE, CHANNEL_INVITE_URL, AFFILIATE_URL, DEAL_BADGE_THRESHOLD …
    tel_router = await spawn("telegram_router", sys.executable, "-m", "telegram.telRouter")

    procs = [
        ("ws_server", ws_server),
        ("deals_watcher", deals_watcher),
        ("product_opener", product_opener),
        ("product_parser", product_parser),
        ("telegram_router", tel_router),
    ]
    for n, p in procs:
        print(f"[supervisor] started {n} (pid={p.pid})")

    # Signalhandling
    stop_event = asyncio.Event()
    def _sig(*_): stop_event.set()
    for s in (signal.SIGINT, signal.SIGTERM):
        try: signal.signal(s, _sig)
        except Exception: pass

    async def wait_any():
        tasks = [asyncio.create_task(p.wait()) for _, p in procs]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        idx = tasks.index(next(iter(done)))
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

    # geordnet beenden
    await terminate(tel_router,      "telegram_router")
    await terminate(product_parser,  "product_parser")
    await terminate(product_opener,  "product_opener")
    await terminate(deals_watcher,   "deals_watcher")
    await terminate(ws_server,       "ws_server")

    print("[supervisor] all stopped")

if __name__ == "__main__":
    asyncio.run(main())
