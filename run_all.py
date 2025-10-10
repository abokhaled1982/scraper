#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_all.py – Supervisor

Starts the browser loader (blocking) BEFORE all long-running services.
The loader opens Edge + Chrome with given profiles/URLs and contains its own sleeps.
Then the Amazon services are started and supervised.
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

# ------------------------------
# Config & Paths
# ------------------------------
HERE = Path(__file__).parent.resolve()
AMAZON = HERE / "amazon"

# URLs and profiles (from user's snippet)
URL = "https://www.amazon.de/deals?ref_=nav_cs_gb"
URL2 = "https://www.geldhub.de/de"
EDGE_PROFILE = "Default"       # e.g. "Default", "Profile 1", "Profile 2"
CHROME_PROFILE = "Profile 1"   # e.g. "Default", "Profile 1"

# ------------------------------
# Loader utilities (from user's snippet)
# ------------------------------
def candidates_for(app_name, subpath_64, subpath_86):
    # Typical installation paths + PATH (Windows)
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    cands = [
        app_name,  # if in PATH
        str(Path(pf) / subpath_64),
        str(Path(pf86) / subpath_86),
    ]
    return cands

def find_executable(candidates):
    for c in candidates:
        # first check PATH
        w = shutil.which(c)
        if w:
            return w
        # then absolute path
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
    """Open Edge and Chrome with the given profiles/URLs and wait (sleep already inside)."""
    edge_path = find_executable(edge_cands)
    chrome_path = find_executable(chrome_cands)

    if not edge_path:
        raise FileNotFoundError(
            "Microsoft Edge (msedge.exe) wurde nicht gefunden.\n"
            "Bitte prüfe die Installation oder passe den Pfad im Skript an.\n"
            f"Getestete Pfade:\n- " + "\n- ".join(edge_cands)
        )

    print("Starte Microsoft Edge...")
    subprocess.Popen([edge_path, f"--profile-directory={EDGE_PROFILE}", URL])

    # kurze Pause, damit Edge sicher hochkommt
    time.sleep(5)

    if not chrome_path:
        raise FileNotFoundError(
            "Google Chrome (chrome.exe) wurde nicht gefunden.\n"
            "Bitte prüfe die Installation oder passe den Pfad im Skript an.\n"
            f"Getestete Pfade:\n- " + "\n- ".join(chrome_cands)
        )

    print("Starte Google Chrome...")
    subprocess.Popen([chrome_path, f"--profile-directory={CHROME_PROFILE}", URL2])

    print("Warte 30 Sekunden...")
    time.sleep(30)
    print("Fertig ✅")

# ------------------------------
# Supervisor for long-running workers
# ------------------------------
def _ensure_dirs():
    # Create common directories if your workers expect them; safe no-ops otherwise.
    (HERE / "data" / "inbox").mkdir(parents=True, exist_ok=True)
    (HERE / "data" / "produckt").mkdir(parents=True, exist_ok=True)  # preserving original naming
    (HERE / "out" / "products").mkdir(parents=True, exist_ok=True)

async def spawn(name: str, *argv: str):
    print(f"[supervisor] spawn {name}: {' '.join(argv)}")
    creationflags = 0
    # Create a new process group on Windows so we can send signals cleanly
    if os.name == "nt":
        creationflags = getattr(asyncio.subprocess, "CREATE_NEW_PROCESS_GROUP", 0)  # type: ignore
    return await asyncio.create_subprocess_exec(*argv, creationflags=creationflags)

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
            try:
                await asyncio.wait_for(proc.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                print(f"[supervisor] WARN: {name} did not exit")
    except ProcessLookupError:
        pass

async def main():
    os.chdir(HERE)
    _ensure_dirs()

    # 1) Run loader BEFORE all scripts (blocking). Sleep is already in there.
    print("[supervisor] running loader (browsers) before services …")
    run_loader_blocking()
    print("[supervisor] loader finished, starting services …")

    # 2) Start long-running services
    ws_server       = await spawn("ws_server",       sys.executable, str(AMAZON / "ws_server.py"))
    deals_watcher   = await spawn("deals_watcher",   sys.executable, str(AMAZON / "watcher.py"))
    product_opener  = await spawn("product_opener",  sys.executable, str(AMAZON / "product_opener.py"))
    product_parser  = await spawn("product_parser",  sys.executable, str(AMAZON / "product_parser.py"))

    procs = [
        ("ws_server", ws_server),
        ("deals_watcher", deals_watcher),
        ("product_opener", product_opener),
        ("product_parser", product_parser),
    ]
    for n, p in procs:
        print(f"[supervisor] started {n} (pid={p.pid})")

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

    # 3) Shut down others in stable order
    await terminate(product_parser,  "product_parser")
    await terminate(product_opener,  "product_opener")
    await terminate(deals_watcher,   "deals_watcher")
    await terminate(ws_server,       "ws_server")

    print("[supervisor] all stopped")

if __name__ == "__main__":
    asyncio.run(main())
