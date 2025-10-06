#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Öffnet neue Produkte aus data/product_list.json in Chrome (Windows, ohne Selenium),
wartet kurz und schließt das Fenster wieder. Protokolliert veröffentlichte
Produkte in data/publische.json, damit beim nächsten Lauf nur neue dran sind.
"""

from __future__ import annotations
import json, time, tempfile, subprocess, shutil, os
from pathlib import Path
from datetime import datetime

from config import PRODUCT_LIST_PATH  # z.B. ./data/product_list.json  :contentReference[oaicite:2]{index=2}

DATA_DIR = PRODUCT_LIST_PATH.parent
PUBLISH_PATH = DATA_DIR / "publische.json"          # enthält veröffentlichte (geöffnete) Produkte
VISITED_TXT = DATA_DIR / "publische.visited.txt"    # einfache Liste der Keys zur schnellen Prüfung

# -------- Hilfen --------

import requests

PRODUCTS_DIR = PRODUCT_LIST_PATH.parent / "products"

def save_html_copy(key: str, url: str):
    try:
        resp = requests.get(url, timeout=20, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
        })
        resp.raise_for_status()
        PRODUCTS_DIR.mkdir(parents=True, exist_ok=True)
        out = PRODUCTS_DIR / f"{key}.html"
        out.write_text(resp.text, encoding="utf-8")
        print(f"[publisher] HTML gespeichert: {out}")
    except Exception as e:
        print(f"[publisher][ERROR] HTML {url}: {e}")


def _now_iso() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}

def _write_json_atomic(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)

def _load_visited() -> set[str]:
    if not VISITED_TXT.exists():
        return set()
    return set(x.strip() for x in VISITED_TXT.read_text(encoding="utf-8").splitlines() if x.strip())

def _save_visited(ids: set[str]) -> None:
    VISITED_TXT.write_text("\n".join(sorted(ids)), encoding="utf-8")

def _detect_chrome_exe() -> str | None:
    """
    Versucht Chrome auf Windows zu finden (ohne Selenium).
    """
    # 1) über PATH (falls verknüpft)
    exe = shutil.which("chrome") or shutil.which("chrome.exe")
    if exe:
        return exe

    # 2) Standardpfade
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ]
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return None

def _open_chrome_new_window(url: str, wait_seconds: float = 6.0) -> None:
    """
    Startet ein eigenes Chrome-Fenster mit separatem User-Dir, wartet, beendet wieder.
    -> Dadurch können wir das Fenster sicher schließen, ohne bestehende Sessions zu killen.
    """
    chrome = _detect_chrome_exe()
    if not chrome:
        # Fallback: Standardbrowser (öffnet Tab, kann nicht sauber geschlossen werden)
        import webbrowser
        webbrowser.open(url, new=1)
        time.sleep(wait_seconds)
        return

    # eigenes, temporäres Profil, damit wir den Prozess gefahrlos killen können
    tmp_profile = Path(tempfile.mkdtemp(prefix="chrome_profile_"))
    cmd = [
        chrome,
        "--new-window",
        f"--user-data-dir={tmp_profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        url,
    ]
    # Starten
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(wait_seconds)
        # Fenster/Prozess beenden
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except Exception:
            proc.kill()
    finally:
        # Profilordner aufräumen
        try:
            shutil.rmtree(tmp_profile, ignore_errors=True)
        except Exception:
            pass

# -------- Kernlogik --------

def _product_key(prod: dict) -> str:
    """
    Gleiche Key-Logik wie im parser_worker: zuerst ASIN, sonst URL, sonst Hash.
    (Hier reicht ASIN/URL, weil wir nur bereits gemergte Produkte verarbeiten.)
    """  # Schema siehe parser_worker.product_key  :contentReference[oaicite:3]{index=3}
    asin = (prod.get("asin") or "").strip().upper()
    if asin and len(asin) == 10:
        return asin
    url = (prod.get("product_url") or "").strip().lower()
    if url:
        return url
    # Fallback – selten nötig:
    name = (prod.get("product_name") or "").strip().lower()
    return f"__name__:{name}"[:120]

def _iter_new_products() -> list[tuple[str, dict]]:
    """
    Liefert (key, product_dict) nur für neue Produkte seit letztem Publisher-Lauf.
    """
    store = _load_json(PRODUCT_LIST_PATH)  # gesamter Store (dict)  :contentReference[oaicite:4]{index=4}
    visited = _load_visited()

    new_items: list[tuple[str, dict]] = []
    # store ist ein dict: key -> product-dict (vom parser_worker geschrieben)
    for key, prod in store.items():
        url = (prod.get("product_url") or "").strip()
        if not url:
            continue
        if key in visited:
            continue
        new_items.append((key, prod))

    # Neueste zuerst (nach _last_seen, wenn vorhanden)
    def _ts(p: dict) -> str:
        return p[1].get("_last_seen") or p[1].get("_first_seen") or ""
    new_items.sort(key=_ts, reverse=True)
    return new_items

def _append_to_publish_file(published: list[dict]) -> None:
    """
    Schreibt/ergänzt publische.json mit den neu veröffentlichten Produkten.
    Struktur: dict { key: product_with_meta }
    """
    pub = _load_json(PUBLISH_PATH)
    if not isinstance(pub, dict):
        pub = {}

    for item in published:
        key = item["__key__"]
        pub[key] = item

    _write_json_atomic(PUBLISH_PATH, pub)

def main():
    new_items = _iter_new_products()
    if not new_items:
        print("[publisher] Keine neuen Produkte zu öffnen.")
        return

    print(f"[publisher] neue Produkte: {len(new_items)}")
    visited = _load_visited()
    published_batch: list[dict] = []

    for key, prod in new_items:
        url = (prod.get("product_url") or "").strip()
        print(f"[publisher] OPEN {key} -> {url}")
        try:
            # 1) Browser öffnen -> (dein Browser/Extension/WebSocket speichert HTML in ./data/inbox)
            _open_chrome_new_window(url, wait_seconds=6.0)
            save_html_copy(key, url)

            # 2) Produkt als "veröffentlicht" festhalten
            published = dict(prod)
            published["__key__"] = key
            published["__published_at"] = _now_iso()
            published_batch.append(published)

            # 3) Merker updaten (damit wir nächstes Mal nicht nochmal öffnen)
            visited.add(key)
            _save_visited(visited)

            # kleine Pause zwischen Produkten
            time.sleep(30)
        except Exception as e:
            print(f"[publisher][ERROR] {key}: {e}")

    # publische.json ergänzen
    if published_batch:
        _append_to_publish_file(published_batch)
        print(f"[publisher] publische.json ergänzt: +{len(published_batch)} -> {PUBLISH_PATH}")

if __name__ == "__main__":
    main()
