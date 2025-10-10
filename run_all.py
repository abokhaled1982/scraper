#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_all.py – Supervisor (Windows/Linux kompatibel)

Startet die komplette Amazon-Pipeline:

  1. amazon/ws_server.py         (empfängt HTMLs von der Extension)
  2. amazon/watcher.py           (Deals-Watcher)
  3. amazon/product_opener.py    (öffnet Produkte aus product_list.json via Chrome)
  4. amazon/product_parser.py    (parst gespeicherte Produkt-HTMLs → JSONs)

Die Steuerung (Start/Stopp, Neustart) erfolgt zentral.
"""

from __future__ import annotations
import asyncio, signal, sys, os, platform
from pathlib import Path

HERE = Path(__file__).parent.resolve()
AMAZON = HERE / "amazon"
DATA_DIR = HERE / "data"
PRODUKT_DIR = DATA_DIR / "produckt"
OUT_DIR = HERE / "out" / "products"

def _ensure_dirs():
    (DATA_DIR / "inbox").mkdir(parents=True, exist_ok=True)
    PRODUKT_DIR.mkdir(parents=True, exist_ok=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

async def spawn(name: str, *argv: str):
    print(f"[supervisor] spawn {name}: {' '.join(argv)}")
    creationflags = 0
    if platform.system() == "Windows":
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

    # Prozesse starten
    ws_server       = await spawn("ws_server",       sys.executable, str(AMAZON / "ws_server.py"))
    deals_watcher   = await spawn("deals_watcher",   sys.executable, str(AMAZON / "watcher.py"))
    #product_opener  = await spawn("product_opener",  sys.executable, str(AMAZON / "product_opener.py"))
    product_parser  = await spawn("product_parser",  sys.executable, str(AMAZON / "product_parser.py"))

    procs = [
        ("ws_server", ws_server),
        ("deals_watcher", deals_watcher),
       # ("product_opener", product_opener),
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

    # Beenden in stabiler Reihenfolge
    await terminate(product_parser,  "product_parser")
    #await terminate(product_opener,  "product_opener")
    await terminate(deals_watcher,   "deals_watcher")
    await terminate(ws_server,       "ws_server")

    print("[supervisor] all stopped")

if __name__ == "__main__":
    asyncio.run(main())
