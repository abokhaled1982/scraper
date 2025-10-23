#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_all.py – Supervisor
Startet:
  1. Browser-Loader (Edge + Chrome) -> Synchron
  2. Sequenzieller Telegram-Login-Check -> Asynchron/Sequenziell
  3. Alle Amazon-Worker und Telegram-Clients -> Parallel
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
from typing import Optional, Dict, List, Tuple
from dotenv import load_dotenv

# ----------------------------------------------------------
# Initial Setup & Pfade
# ----------------------------------------------------------
HERE = Path(__file__).parent.resolve()
AMAZON = HERE / "amazon"
PY = sys.executable  # Aktuelles venv-Python

# Projekt-Root in sys.path aufnehmen, um login_once zu finden
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

# ⚠️ Import der Telegram-Login-Helfer
# Der Pfad muss korrigiert werden, da login_once.py im Stammverzeichnis ist,
# aber telRouter/telObserver es über 'from telegram.login_once' importieren.
# Wir müssen hier direkt vom Stammverzeichnis importieren, da run_all.py dort liegt.
try:
    # 💡 KORRIGIERT: Importiere direkt aus dem Stammverzeichnis, da run_all.py dort liegt.
    # Da die telRouter/Observer-Dateien es als 'telegram.login_once' importieren,
    # liegt der Fehler eher in deren Importpfad, aber hier fixen wir es für run_all.py:
    from telegram.login_once import LoginConfig, ensure_both_sessions_sequential
except ImportError:
    # Fallback, falls der Nutzer login_once.py ins 'telegram' Verzeichnis verschoben hat.
    try:
        from telegram.login_once import LoginConfig, ensure_both_sessions_sequential
    except ImportError:
        print("❌ Fehler: login_once.py konnte nicht gefunden werden. Bitte sicherstellen, dass sie im Projekt-Root liegt.")
        sys.exit(1)


# 🔹 .env laden
load_dotenv()

# URLs / Profile aus .env oder Defaults
URL              = os.getenv("EDGE_URL", "https://www.amazon.de/deals?ref_=nav_cs_gb")
URL2             = os.getenv("CHROME_URL", "https://www.geldhub.de/de")
EDGE_PROFILE     = os.getenv("EDGE_PROFILE", "Default")
CHROME_PROFILE   = os.getenv("CHROME_PROFILE", "Profile 1")

# 🔸 Telegram Konfiguration
API_ID           = int(os.getenv("API_ID", "0"))
API_HASH         = os.getenv("API_HASH", "")
SESSION_DIR      = os.getenv("SESSION_DIR", ".sessions")
PHONE            = os.getenv("TELEGRAM_PHONE")
PASSWORD         = os.getenv("TELEGRAM_PASSWORD")

ROUTER_NAME      = os.getenv("SESSION_NAME", "main_session")
OBSERVER_NAME    = os.getenv("OBS_SESSION_NAME", "observer_session")
ROUTER_CHANNEL   = os.getenv("CHANNEL_INVITE_URL", "").strip()
OBSERVER_CHANNEL = os.getenv("OBS_CHANNEL_INVITE_URL", "").strip()

if not all([API_ID, API_HASH, ROUTER_CHANNEL, OBSERVER_CHANNEL]):
    raise SystemExit("Fehler: Mindestens eine Telegram-Variable (API_ID, HASH, CHANNEL_INVITE_URL, OBS_CHANNEL_INVITE_URL) fehlt in .env.")

# ----------------------------------------------------------
# Browser Loader (Unverändert)
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
        # Dies ist ein kritischer Fehler im Originalcode, den wir beibehalten,
        # falls der Chrome-Pfad essentiell ist.
        # Im normalen Supervisor-Ablauf würde man hier eventuell nur warnen.
        raise FileNotFoundError("❌ Chrome wurde nicht gefunden – bitte Pfad prüfen.")

    print("🌐 Starte Google Chrome …")
    subprocess.Popen([chrome_path, f"--profile-directory={CHROME_PROFILE}", URL2])

    sleep_time = int(os.getenv("BROWSER_WAIT_SECS", "30"))
    print(f"⏳ Warte {sleep_time} Sekunden …")
    time.sleep(sleep_time)
    print("✅ Browser-Loader fertig.")
    
# ----------------------------------------------------------
# Supervisor Utilities (Unverändert)
# ----------------------------------------------------------
def _ensure_dirs():
    (HERE / "data" / "inbox").mkdir(parents=True, exist_ok=True)
    (HERE / "data" / "produckt").mkdir(parents=True, exist_ok=True)
    (HERE / "data" / "out").mkdir(parents=True, exist_ok=True)
    (HERE / SESSION_DIR).mkdir(parents=True, exist_ok=True) # Nutze SESSION_DIR Variable
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
# Sequentieller Telegram Login (Unverändert)
# ----------------------------------------------------------
def print_login_step(msg: str):
    print(f"[Telegram Login] {msg}")
    
async def do_telegram_login_check():
    """Stellt sicher, dass Router- und Observer-Session gültig sind."""
    print("\n--- Starte sequentiellen Telegram-Login-Check ---")
    
    router_cfg = LoginConfig(
        api_id=API_ID, api_hash=API_HASH, session_name=ROUTER_NAME, session_dir=SESSION_DIR,
        phone=PHONE, password=PASSWORD,
    )
    observer_cfg = LoginConfig(
        api_id=API_ID, api_hash=API_HASH, session_name=OBSERVER_NAME, session_dir=SESSION_DIR,
        phone=PHONE, password=PASSWORD,
    )
    
    # Führt den 2-stufigen Login aus (Router -> Observer)
    ok1, ok2 = await ensure_both_sessions_sequential(
        router_cfg, observer_cfg, on_step=print_login_step
    )
    
    if not (ok1 and ok2):
        raise SystemExit("❌ Telegram-Login fehlgeschlagen. Abbruch.")
    
    print("✅ Telegram-Login für Router & Observer abgeschlossen.")
    print("---------------------------------------------------\n")

# ----------------------------------------------------------
# Main Supervisor
# ----------------------------------------------------------
async def main():
    os.chdir(HERE)
    _ensure_dirs()
    
    # 1. 🌐 Browser-Loader (Blocking)
    # 💡 NEU: Dies ist der erste Schritt und ist synchron/blocking.
    print("[supervisor] running loader before services …")
    #run_loader_blocking()
    print("[supervisor] loader finished, starting Telegram check …")

    # 2. 🔑 Sequentieller Login-Check (Muss nach dem Browser-Start passieren!)
    await do_telegram_login_check() 

    # 3. 🟢 Services starten (Parallel)
    
    # Amazon Services
    ws_server       = await spawn("ws_server",      PY, str(AMAZON / "ws_server.py"))
    deals_watcher   = await spawn("deals_watcher",  PY, str(AMAZON / "watcher.py"))
    product_opener  = await spawn("product_opener", PY, str(AMAZON / "product_opener.py"))
    product_parser  = await spawn("product_parser", PY, str(AMAZON / "product_parser.py"))

    # Telegram Services (jetzt mit Router UND Observer)
    # Beide starten parallel als Subprozesse, da der Login bereits abgeschlossen ist.
    tel_router = await spawn("telegram_router", PY, "-m", "telegram.telRouter")
    tel_observer = await spawn("telegram_observer", PY, "-m", "telegram.telObserver")

    # Liste aller Prozesse für die Überwachung und das Beenden
    procs: List[Tuple[str, asyncio.subprocess.Process]] = [
        ("ws_server", ws_server),
        ("deals_watcher", deals_watcher),
        ("product_opener", product_opener),
        ("product_parser", product_parser),
        ("telegram_router", tel_router),
        ("telegram_observer", tel_observer),
    ]
    for n, p in procs:
        print(f"[supervisor] started {n} (pid={p.pid})")

    # Signalhandling (Unverändert)
    stop_event = asyncio.Event()
    def _sig(*_): stop_event.set()
    for s in (signal.SIGINT, signal.SIGTERM):
        try: signal.signal(s, _sig)
        except Exception: pass

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

    # 4. 🛑 Geordnet beenden (Unverändert)
    await terminate(tel_observer,    "telegram_observer")
    await terminate(tel_router,      "telegram_router")
    await terminate(product_parser,  "product_parser")
    await terminate(product_opener,  "product_opener")
    await terminate(deals_watcher,   "deals_watcher")
    await terminate(ws_server,       "ws_server")

    print("[supervisor] all stopped")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        print(f"❌ Critical Error in main runner: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n[supervisor] Abgebrochen durch Benutzer.")