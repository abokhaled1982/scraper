#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Open product URLs from product_list.json while:
- de-duplicating by canonical URL and ASIN
- tracking opens in .opened.json with last_open + meta_hash + canonical_url
- only opening a tab if not already opened recently or content changed

Replaces previous opener_2.py.
"""

import json
import time
import subprocess
from pathlib import Path
import os
import hashlib
from urllib.parse import urlparse
import sys
from pathlib import Path
sys.path.append(str(Path(__file__).resolve().parent.parent))
from config import OPENED_PATH, PRODUCT_LIST_PATH




# OPENED_PATH = PROJ / ".opened.json"    # state file we extend/maintain

CHROME_BIN = os.environ.get(
    "CHROME_BIN",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe"
)
PROFILE_NAME = os.environ.get("CHROME_PROFILE", "Profile 1")
PAUSE_SECONDS = int(os.environ.get("PAUSE_SECONDS", "30"))
# If the same canonical URL was opened within this TTL, skip reopening (seconds)
SKIP_TTL_SECONDS = int(os.environ.get("SKIP_TTL_SECONDS", str(24*3600)))
DRY_RUN = os.environ.get("DRY_RUN", "0") not in ("0", "", "false", "False", "no", "No")
# ----------------


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
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[ERR] Failed to write JSON {p}: {e}")

def compute_meta_hash(meta: dict) -> str:
    try:
        blob = json.dumps(meta or {}, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return hashlib.md5(blob).hexdigest()
    except Exception:
        return "0"*32

def canonicalize_amazon_url(url: str) -> str:
    """
    Normalize Amazon product URLs so query params/tracking don’t cause duplicates.
    Prefer https://<host>/dp/<ASIN>
    """
    if not url:
        return url
    try:
        u = urlparse(url)
        host = u.netloc.lower()
        path = u.path

        # Try to extract ASIN from common patterns
        asin = None
        parts = [p for p in path.split("/") if p]
        for i, p in enumerate(parts):
            # .../dp/ASIN
            if p.lower() == "dp" and i + 1 < len(parts):
                asin = parts[i+1]
                break
            # .../gp/product/ASIN
            if p.lower() == "product" and i > 0 and parts[i-1].lower() == "gp" and i + 1 < len(parts):
                asin = parts[i+1]
                break

        if asin and len(asin) in (10, 12):  # Amazon ASINs are typically 10 chars, but be lenient
            return f"https://{host}/dp/{asin}"
        # If we can’t find ASIN, at least drop query/fragment
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

def should_open(asin: str, url: str, meta: dict, opened: dict) -> tuple[bool, str]:
    """
    Decide whether to open this product.
    Returns (should_open_flag, reason_msg)
    """
    now = time.time()
    can_url = canonicalize_amazon_url(url)
    mhash = compute_meta_hash(meta)

    # Check if this canonical URL was opened recently by anyone
    # We look through all entries for a URL match within TTL.
    for prev_asin, rec in opened.items():
        rec_url = rec.get("canonical_url")
        last_open = rec.get("last_open", 0)
        if rec_url == can_url and (now - last_open) < SKIP_TTL_SECONDS:
            return (False, f"skip: URL already opened recently ({prev_asin})")

    # Check if this ASIN exists with same meta hash (no content change)
    rec = opened.get(asin)
    if rec:
        prev_hash = rec.get("meta_hash")
        last_open = rec.get("last_open", 0)
        if prev_hash == mhash and (now - last_open) < SKIP_TTL_SECONDS:
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
    # Load inputs
    #product_list_path = find_product_list_path()
    products = load_json(PRODUCT_LIST_PATH, default={})  # {ASIN: meta}
    opened = load_json(OPENED_PATH, default={})          # state
    # registry = load_json(REGISTRY_PATH, default={})    # not required for opening logic

    if not isinstance(products, dict) or not products:
        print("[INFO] No items in product_list.json.")
        return

    # Deterministic order: newest discount first? If not available, sort by ASIN
    # You can tweak this to sort by discount, time, etc.
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
            # pause between tabs
            time.sleep(PAUSE_SECONDS)
        else:
            print(f"[{idx}/{total}] [FAIL] Could not open {asin}")

    print(f"[DONE] opened={opened_count}, skipped={skipped}, total={total}")

if __name__ == "__main__":
    main()
