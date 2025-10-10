#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Reads list.json (mapping ASIN -> meta) and opens each product_url in Chrome,
one after another with a 30s pause.
"""

import json
import time
import subprocess
from pathlib import Path
import os
import sys

# --- CONFIG (edit if needed) ---
HERE = Path(__file__).parent.resolve()
ROOT = HERE.parent if HERE.name == "amazon" else HERE
PROJ = ROOT

PRODUCT_LIST_JSOS=  PROJ / "data" / "product_list.json"
CHROME_BIN = os.environ.get(
    "CHROME_BIN",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe"
)
PROFILE_NAME = os.environ.get("CHROME_PROFILE", "Profile 1")  # use your Chrome profile
PAUSE_SECONDS = 30
# -------------------------------

def load_products(p: Path):
    if not p.exists():
        print(f"[ERR] JSON not found: {p.resolve()}")
        sys.exit(1)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Top-level JSON must be an object {ASIN: {...}}")
        return data
    except Exception as e:
        print(f"[ERR] Failed to read JSON: {e}")
        sys.exit(1)

def open_in_chrome(url: str):
    # Open url as a new tab in (or starting) Chrome with the given profile
    cmd = [
        CHROME_BIN,
        f"--profile-directory={PROFILE_NAME}",
        "--new-tab",
        url
    ]
    # Use start without waiting; Chrome handles tab creation
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except FileNotFoundError:
        print(f"[ERR] Chrome not found: {CHROME_BIN}")
        return False
    except Exception as e:
        print(f"[ERR] launching Chrome: {e}")
        return False

def main():
    products = load_products(PRODUCT_LIST_JSOS)
    # deterministic order: by ASIN
    items = sorted(products.items(), key=lambda kv: kv[0])

    if not items:
        print("[INFO] No items in JSON.")
        return

    print(f"[INFO] Going to open {len(items)} tabs, pausing {PAUSE_SECONDS}s between each.")
    for idx, (asin, meta) in enumerate(items, start=1):
        url = (meta or {}).get("product_url")
        if not url:
            print(f"[SKIP] {asin}: no product_url")
            continue

        print(f"[{idx}/{len(items)}] OPEN {asin} -> {url}")
        ok = open_in_chrome(url)
        if not ok:
            print(f"[FAIL] Could not open {asin}")
            continue

        # pause before next tab
        time.sleep(PAUSE_SECONDS)

    print("[DONE] Finished opening all tabs.")

if __name__ == "__main__":
    main()
