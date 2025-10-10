#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Open product URLs from product_list.json while:
- de-duplicating by canonical URL and ASIN
- tracking opens in .opened.json with last_open + meta_hash + canonical_url
- only opening a tab if not already opened recently or content changed

NEU:
- Legt product_list.json an, wenn sie fehlt.
- Wartet, bis die Datei mind. 1 Item enthält (bricht nicht ab).
"""

import json
import time
import subprocess
import os
import hashlib
import sys
from pathlib import Path
from urllib.parse import urlparse

# config aus Parent-Ordner laden (direkter Skriptstart möglich)
sys.path.append(str(Path(__file__).resolve().parent.parent))
from config import OPENED_PATH, PRODUCT_LIST_PATH  # , ensure_directories (optional)

# ---------------- Konfiguration ----------------
CHROME_BIN = os.environ.get(
    "CHROME_BIN",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe"
)
PROFILE_NAME   = os.environ.get("CHROME_PROFILE", "Profile 1")
PAUSE_SECONDS  = int(os.environ.get("PAUSE_SECONDS", "30"))
SKIP_TTL_SECONDS = int(os.environ.get("SKIP_TTL_SECONDS", str(24*3600)))
DRY_RUN = os.environ.get("DRY_RUN", "0") not in ("0", "", "false", "False", "no", "No")
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "5"))  # Wartezeit beim Leerlauf
# ------------------------------------------------

def load_json(p: Path, default):
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[ERR] Failed to read JSON {p}: {e}")
        return default

def save_json(p: Path, data):
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[ERR] Failed to write JSON {p}: {e}")

def ensure_product_list_exists() -> None:
    """legt product_list.json an, wenn nicht vorhanden"""
    if not PRODUCT_LIST_PATH.exists():
        print(f"[opener] creating empty {PRODUCT_LIST_PATH}")
        save_json(PRODUCT_LIST_PATH, {})

def wait_until_has_items(poll_seconds: int = POLL_SECONDS) -> dict:
    """
    Blockiert, bis product_list.json ein dict mit >=1 Item enthält.
    Gibt den geladenen Dict zurück.
    """
    ensure_product_list_exists()

    while True:
        products = load_json(PRODUCT_LIST_PATH, default={})
        if isinstance(products, dict) and len(products) > 0:
            return products

        # Wenn keine Items da sind: kurz warten und weiter
        try:
            count = len(products) if isinstance(products, dict) else 0
        except Exception:
            count = 0
        print(f"[opener] waiting for items in {PRODUCT_LIST_PATH} (current: {count}) … {poll_seconds}s")
        time.sleep(poll_seconds)

def compute_meta_hash(meta: dict) -> str:
    try:
        blob = json.dumps(meta or {}, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return hashlib.md5(blob).hexdigest()
    except Exception:
        return "0"*32

def canonicalize_amazon_url(url: str) -> str:
    """Normalisiert Amazon-URLs; bevorzugt https://<host>/dp/<ASIN>"""
    if not url:
        return url
    try:
        u = urlparse(url)
        host = u.netloc.lower()
        path = u.path

        asin = None
        parts = [p for p in path.split("/") if p]
        for i, p in enumerate(parts):
            if p.lower() == "dp" and i + 1 < len(parts):
                asin = parts[i+1]; break
            if p.lower() == "product" and i > 0 and parts[i-1].lower() == "gp" and i + 1 < len(parts):
                asin = parts[i+1]; break

        if asin and len(asin) in (10, 12):
            return f"https://{host}/dp/{asin}"
        clean_path = "/" + "/".join(parts)
        return f"https://{host}{clean_path}"
    except Exception:
        return url

def open_in_chrome(url: str) -> bool:
    if DRY_RUN:
        print(f"[DRY-RUN] Would open: {url}")
        return True
    cmd = [CHROME_BIN, f"--profile-directory={PROFILE_NAME}", "--new-tab", url]
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except FileNotFoundError:
        print(f"[ERR] Chrome not found: {CHROME_BIN}")
        return False
    except Exception as e:
        print(f"[ERR] launching Chrome: {e}")
        return False

def compute_canonical(url: str, meta: dict) -> tuple[str, str]:
    return canonicalize_amazon_url(url), compute_meta_hash(meta)

def should_open(asin: str, url: str, meta: dict, opened: dict) -> tuple[bool, str]:
    """
    Entscheidet, ob ein Produkt geöffnet werden soll.
    Rückgabe: (True/False, Grund)
    """
    now = time.time()
    can_url, mhash = compute_canonical(url, meta)

    # gleiche kanonische URL kürzlich geöffnet?
    for prev_asin, rec in opened.items():
        if rec.get("canonical_url") == can_url and (now - rec.get("last_open", 0)) < SKIP_TTL_SECONDS:
            return (False, f"skip: URL already opened recently ({prev_asin})")

    # gleicher ASIN + unveränderte Meta innerhalb TTL?
    rec = opened.get(asin)
    if rec and rec.get("meta_hash") == mhash and (now - rec.get("last_open", 0)) < SKIP_TTL_SECONDS:
        return (False, "skip: same ASIN + unchanged meta within TTL")

    return (True, "open")

def update_opened(opened: dict, asin: str, url: str, meta: dict) -> None:
    now = time.time()
    opened[asin] = {
        "last_open": now,
        "meta_hash": compute_meta_hash(meta),
        "canonical_url": canonicalize_amazon_url(url),
    }

def main():
    # Optional: falls du sicher gehen willst, dass data/ existiert
    # from config import ensure_directories; ensure_directories()

    # wartet hier, bis Items vorhanden sind (legt Datei an, falls fehlt)
    products = wait_until_has_items()

    # State laden/sicherstellen
    opened = load_json(OPENED_PATH, default={})

    # Reihenfolge: aktuell einfach nach Key; hier könntest du auch nach Rabatt etc. sortieren
    items = sorted(products.items(), key=lambda kv: kv[0])

    total = len(items)
    print(f"[INFO] Considering {total} items. TTL={SKIP_TTL_SECONDS}s, pause={PAUSE_SECONDS}s, dry_run={DRY_RUN}")

    opened_count = 0
    skipped = 0
    for idx, (asin, meta) in enumerate(items, start=1):
        url = (meta or {}).get("product_url")
        if not url:
            print(f"[{idx}/{total}] [SKIP] {asin}: no product_url")
            skipped += 1
            continue

        ok_to_open, reason = should_open(asin, url, meta, opened)
        if not ok_to_open:
            print(f"[{idx}/{total}] [SKIP] {asin} -> {reason}")
            skipped += 1
            continue

        print(f"[{idx}/{total}] OPEN {asin} -> {canonicalize_amazon_url(url)}")
        if open_in_chrome(url):
            update_opened(opened, asin, url, meta)
            save_json(OPENED_PATH, opened)
            opened_count += 1
            time.sleep(PAUSE_SECONDS)
        else:
            print(f"[{idx}/{total}] [FAIL] Could not open {asin}")

    print(f"[DONE] opened={opened_count}, skipped={skipped}, total={total}]")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[opener] stopped by user")
