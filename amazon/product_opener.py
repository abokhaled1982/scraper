#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
product_opener.py
Öffnet Produkt-URLs aus product_list.json in separaten Chrome-Instanzen,
damit die Extension das HTML an den WS-Server sendet. Danach wird die
Instanz geschlossen. Dedup/Cooldown verhindern Doppel-Öffnungen.

Ablauf:
  product_list.json -> (URLs) -> Chrome + Extension -> WS-Server -> data/produckt/*.html
  product_parser.py  -> out/products/<ASIN>.json

Features:
- Pool aus N parallelen Chrome-Instanzen (POOL_SIZE)
- Cooldown pro ASIN (OPEN_COOLDOWN_SECS)
- Erkennt automatisch übliche Pfade (product_list.json, extension/)
- Persistente Registry (.opened.json) um Re-Opens zu steuern
- Robustes Error-Handling & sauberes Cleanup
"""

from __future__ import annotations

import os
import sys
import json
import time
import shutil
import tempfile
import subprocess
import platform
import traceback
import threading
import queue
from pathlib import Path
from typing import Dict, Optional, Set, Tuple, List

# ====================== Grundpfade ermitteln ======================

HERE = Path(__file__).parent.resolve()
# Wenn dieses Skript im Ordner 'amazon' liegt → Projekt-Root ist die Eltern-Ebene
ROOT = HERE.parent if HERE.name == "amazon" else HERE
PROJ = ROOT

# Kandidaten für product_list.json
PRODUCT_LIST_JSON_CANDIDATES: List[Path] = [
    PROJ / "product_list.json",
    PROJ / "data" / "product_list.json",
]

# Kandidaten für Extension-Ordner (mit manifest.json)
EXT_CANDIDATES: List[Path] = [
    PROJ / "extension",
    HERE / "extension",
    PROJ / "amazon" / "extension",
]

# Ausgabeverzeichnis, in das der Produkt-Parser schreibt
OUT_PRODUCTS: Path = (PROJ / "out" / "products").resolve()

# Persistente Registry, um pro ASIN zu merken, wann zuletzt geöffnet wurde
OPENED_REGISTRY: Path = OUT_PRODUCTS / ".opened.json"

# ====================== Konfiguration =============================

# Wie lange bleibt ein Produktfenster offen (Sekunden)?
PRODUCT_WINDOW_LIFETIME_SECS: int = 60

# Wie oft der Dispatcher nachsieht (Sekunden)
DISPATCH_INTERVAL_SECS: int = 5

# Anzahl paralleler Chrome-Instanzen
POOL_SIZE: int = 3

# Cooldown je ASIN (währenddessen nicht erneut öffnen)
OPEN_COOLDOWN_SECS: int = 60 * 30  # 30 Minuten

# Chrome-Binary (per Env überschreibbar)
CHROME_BIN: str = os.environ.get(
    "CHROME_BIN",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    if platform.system() == "Windows" else "google-chrome"
)

# ====================== Hilfsfunktionen ===========================

def _resolve_extension_dir() -> Path:
    for p in EXT_CANDIDATES:
        if p.exists() and (p / "manifest.json").exists():
            return p.resolve()
    # Fallback: erster Kandidat, manifest.json wird später geprüft
    return EXT_CANDIDATES[0].resolve()

EXTENSION_DIR: Path = _resolve_extension_dir()

def _validate_env() -> None:
    # OUT_PRODUCTS ggf. anlegen (damit .opened.json gespeichert werden kann)
    OUT_PRODUCTS.mkdir(parents=True, exist_ok=True)

    if not EXTENSION_DIR.exists() or not (EXTENSION_DIR / "manifest.json").exists():
        print(f"[product-opener] WARN: EXTENSION_DIR ohne manifest.json: {EXTENSION_DIR}", file=sys.stderr)

    # Chrome-Verfügbarkeit prüfen
    if platform.system() == "Windows":
        if not Path(CHROME_BIN).exists():
            print(f"[product-opener] WARN: CHROME_BIN nicht gefunden: {CHROME_BIN}", file=sys.stderr)
    else:
        try:
            subprocess.run([CHROME_BIN, "--version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        except Exception:
            print(f"[product-opener] WARN: CHROME_BIN nicht aufrufbar: {CHROME_BIN}", file=sys.stderr)

def _find_product_list_json() -> Optional[Path]:
    for p in PRODUCT_LIST_JSON_CANDIDATES:
        if p.exists():
            return p
    return None

def _load_product_list() -> Dict[str, Dict]:
    pj = _find_product_list_json()
    if not pj:
        print("[product-opener] INFO: product_list.json nicht gefunden – warte …")
        return {}
    try:
        data = json.loads(pj.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            print("[product-opener] WARN: product_list.json erwartet ein Dict {ASIN: {...}}")
            return {}
        return data
    except Exception as e:
        print(f"[product-opener] ERROR product_list.json lesen: {e}")
        return {}

def _load_opened() -> Dict[str, float]:
    if OPENED_REGISTRY.exists():
        try:
            return json.loads(OPENED_REGISTRY.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}

def _save_opened(opened: Dict[str, float]) -> None:
    try:
        tmp = OPENED_REGISTRY.with_suffix(".tmp")
        tmp.write_text(json.dumps(opened, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(OPENED_REGISTRY)
    except Exception:
        pass

def _already_parsed(asin: str) -> bool:
    # Produkt gilt als erledigt, wenn Parser-Ausgabe vorhanden ist
    return (OUT_PRODUCTS / f"{asin}.json").exists()

def _due(opened: Dict[str, float], asin: str, now: float) -> bool:
    last = opened.get(asin)
    return last is None or (now - last) >= OPEN_COOLDOWN_SECS

# ====================== Chrome Start/Stop =========================

def _launch_chrome_with_extension(url: str) -> subprocess.Popen:
    # entweder temporär lassen ...
    # user_data_dir = Path(tempfile.mkdtemp(prefix="prodopener_profile_"))

    # ... oder festen User-Data-Pfad nutzen
    user_data_dir = Path.home() / "AppData/Local/Google/Chrome/User Data"  # Windows-Beispiel
    # macOS: Path.home() / "Library/Application Support/Google/Chrome"
    # Linux: Path.home() / ".config/google-chrome"

    profile_name = "Profile 2"  # z.B. "Default", "Profile 1", "Profile 2", ...

    args = [
        CHROME_BIN,
        "--new-window", url,
        f"--user-data-dir={user_data_dir}",
        f"--profile-directory={profile_name}",      # <— HIER: Profil-Attribut
        f"--load-extension={EXTENSION_DIR}",
        f"--disable-extensions-except={EXTENSION_DIR}",
        "--no-first-run",
        "--no-default-browser-check",
        "--allow-file-access-from-files",
        "--autoplay-policy=no-user-gesture-required",
    ]
    creationflags = 0
    if platform.system() == "Windows":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    proc = subprocess.Popen(args, creationflags=creationflags)
    # Profilpfad am Prozess merken, damit wir ihn beim Close löschen können
    proc._tmp_profile_dir = user_data_dir  # type: ignore[attr-defined]
    return proc

def _close_chrome(proc: subprocess.Popen):
    try:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    except Exception:
        pass
    # Profilverzeichnis aufräumen
    try:
        prof = getattr(proc, "_tmp_profile_dir", None)
        if prof and Path(prof).exists():
            shutil.rmtree(prof, ignore_errors=True)
    except Exception:
        pass

# ====================== Worker-Pool ===============================

class Task:
    __slots__ = ("asin", "url")
    def __init__(self, asin: str, url: str):
        self.asin = asin
        self.url = url

def worker_loop(work_q: "queue.Queue[Task]", worker_id: int, stop_evt: threading.Event, opened: Dict[str, float]):
    print(f"[product-opener-worker-{worker_id}] started")
    while not stop_evt.is_set():
        try:
            try:
                task: Task = work_q.get(timeout=1.0)
            except queue.Empty:
                continue

            asin, url = task.asin, task.url
            try:
                print(f"[product-opener-worker-{worker_id}] OPEN {asin} → {url}")
                proc = _launch_chrome_with_extension(url)
                time.sleep(PRODUCT_WINDOW_LIFETIME_SECS)
                _close_chrome(proc)
                opened[asin] = time.time()
                _save_opened(opened)
                print(f"[product-opener-worker-{worker_id}] DONE {asin}")
            except Exception as e:
                print(f"[product-opener-worker-{worker_id}] ERROR {asin}: {e}")
                traceback.print_exc()
            finally:
                try:
                    work_q.task_done()
                except Exception:
                    pass

        except Exception:
            print(f"[product-opener-worker-{worker_id}] unexpected worker error:")
            traceback.print_exc()
            time.sleep(1)
    print(f"[product-opener-worker-{worker_id}] stopped")

# ====================== Dispatcher / Main ==========================

def main():
    _validate_env()
    opened = _load_opened()
    work_q: "queue.Queue[Task]" = queue.Queue()
    inflight: Set[str] = set()
    stop_evt = threading.Event()

    # Worker starten
    workers = []
    for i in range(POOL_SIZE):
        t = threading.Thread(target=worker_loop, args=(work_q, i + 1, stop_evt, opened), daemon=True)
        t.start()
        workers.append(t)

    print(f"[product-opener] source=product_list.json, pool={POOL_SIZE}, window={PRODUCT_WINDOW_LIFETIME_SECS}s, cooldown={OPEN_COOLDOWN_SECS}s")
    try:
        while True:
            plist = _load_product_list()
            now = time.time()

            # Kandidaten aus JSON einsammeln (ASIN → URL)
            to_open: List[Tuple[str, str]] = []
            for asin, meta in plist.items():
                url = (meta or {}).get("product_url")
                if not asin or not url:
                    continue
                if _already_parsed(asin):
                    # schon geparst → überspringen
                    continue
                if asin in inflight:
                    continue
                if not _due(opened, asin, now):
                    continue
                to_open.append((asin, url))

            # Stabile Reihenfolge (z. B. ASIN-lexikografisch)
            to_open.sort(key=lambda x: x[0])

            # Enqueue bis die Worker gut gefüttert sind
            for asin, url in to_open:
                try:
                    work_q.put_nowait(Task(asin, url))
                    inflight.add(asin)
                except queue.Full:
                    break

            # inflight bereinigen:
            # - wenn parsed-JSON inzwischen existiert → raus
            # - oder wenn Cooldown gesetzt (Worker hat fertig) → raus
            for a in list(inflight):
                if _already_parsed(a) or (a in opened and (now - opened[a]) < OPEN_COOLDOWN_SECS):
                    inflight.discard(a)

            if not to_open and not inflight:
                print("[product-opener] keine fälligen Produkte – warte …")

            time.sleep(DISPATCH_INTERVAL_SECS)

    except KeyboardInterrupt:
        print("[product-opener] stopped by user")
    except Exception:
        print("[product-opener] dispatcher error; sleep 1s")
        traceback.print_exc()
        time.sleep(1)
    finally:
        stop_evt.set()
        try:
            work_q.join(timeout=5)
        except Exception:
            pass
        for t in workers:
            t.join(timeout=2)
        print("[product-opener] exited")

if __name__ == "__main__":
    main()
