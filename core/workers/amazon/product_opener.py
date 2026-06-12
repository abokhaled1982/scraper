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
from urllib.parse import urlparse,urlparse, urlunparse, parse_qs, urlencode

# config aus Parent-Ordner laden (direkter Skriptstart möglich)
sys.path.append(str(Path(__file__).resolve().parent.parent))

from core.logging import get_logger  # noqa: E402
log = get_logger("product_opener")  # noqa: E402
from core.db import state_repo
from core.workers.chrome_launcher import ChromeProfile  # noqa: E402

_PRODUCT_LIST_KEY = "product_list"
_OPENED_KEY = "opened"

# ---------------- Konfiguration ----------------
CHROME_BIN = os.environ.get(
    "CHROME_BIN",
    "/usr/bin/google-chrome"  # or "/usr/bin/chromium-browser" depending on what is installed
)
# Dediziertes Worker-Profil (eigenes user-data-dir) + Auto-Load der Extension
PROFILE_NAME   = os.environ.get("AMAZON_CHROME_PROFILE", "amazon")
ADDON_DIR      = os.environ.get("AMAZON_ADDON_DIR", "addons/proudct_parser")
_chrome = ChromeProfile(PROFILE_NAME, addons=[ADDON_DIR])
# Pause zwischen dem Öffnen zweier Angebote. 20s sorgt dafür, dass das Addon
# in der aktiven Tab genug Zeit hat, SiteStripe zu öffnen und den
# Affiliate-Link sauber in die Zwischenablage zu kopieren, bevor der nächste
# Tab den Fokus übernimmt.
PAUSE_SECONDS  = int(os.environ.get("PAUSE_SECONDS", "20"))
SKIP_TTL_SECONDS = int(os.environ.get("SKIP_TTL_SECONDS", str(24*3600)))
DRY_RUN = os.environ.get("DRY_RUN", "0") not in ("0", "", "false", "False", "no", "No")
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "30"))  # Wartezeit beim Leerlauf
# ------------------------------------------------


# ---------------- NEUE HELFERFUNKTION ----------------
def add_trigger_param(url: str) -> str:
    """
    Markiert die URL als "vom Opener geöffnet", indem ein URL-Fragment
    angehängt wird.

    Vorteile gegenüber einem Query-Parameter:
      - Fragmente werden NIE an den Server gesendet (Affiliate-Tracker wie
        Awin auf sportspar.de sehen sie gar nicht und bauen sie nicht in
        301/302-Redirects um).
      - Fragmente landen nicht in serverseitigen Logs.
      - Chrome kennt die volle URL inkl. Fragment beim Tab-Öffnen; unser
        Background-Worker erkennt den Marker via `chrome.tabs.onCreated` und
        `chrome.webNavigation.onBeforeNavigate` BEVOR irgendein Page-Skript
        die URL via `history.replaceState()` säubern kann.
    """
    try:
        parsed = urlparse(url)
        # Bestehende Query bleibt unangetastet; nur das Fragment wird gesetzt.
        return urlunparse(parsed._replace(fragment="__opener__"))
    except Exception as e:
        log.error(f"Fehler beim Setzen des Opener-Fragments für {url}: {e}")
        return url  # Im Fehlerfall die Original-URL zurückgeben

def load_json(p: Path, default):
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        log.error(f"[ERR] Failed to read JSON {p}: {e}")
        return default

def save_json(p: Path, data):
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        log.error(f"[ERR] Failed to write JSON {p}: {e}")

def ensure_product_list_exists() -> None:
    """No-op: product_list lebt nun in der DB (state_kv)."""
    return None

def wait_until_has_items(poll_seconds: int = POLL_SECONDS) -> dict:
    """
    Blockiert, bis state_kv['product_list'] mind. 1 Item enthält.
    Gibt den geladenen Dict zurück.

    Setzt während des Wartens periodisch `set_next_run`, damit das
    Dashboard einen echten Live-Countdown anzeigt und der Worker nicht
    fälschlich als 'busy' oder 'stale' erscheint.
    """
    from core.db import workers_repo as _wr
    _W = "amazon_opener"
    while True:
        products = state_repo.get_dict(_PRODUCT_LIST_KEY)
        if isinstance(products, dict) and len(products) > 0:
            return products
        # Echter Status: idle + Countdown bis zum nächsten DB-Check.
        try:
            _wr.set_next_run(_W, poll_seconds, label="product_list check")
        except Exception:
            pass
        log.info(f"[opener] waiting for items in DB key '{_PRODUCT_LIST_KEY}' (current: 0) … {poll_seconds}s")
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
        log.info(f"[DRY-RUN] Would open: {url}")
        return True
    return _chrome.open(url, new_tab=True)

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
    # Load configuration from environment/defaults
    from core.db import workers_repo
    _WORKER = "amazon_opener"
    workers_repo.register(_WORKER)
    POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "10")) 

    while True:
        # 1. Wait for items (blocking until items are present).
        # `wait_until_has_items` aktualisiert den Heartbeat während des Wartens
        # selbst via set_next_run, damit das Dashboard nicht 'stale' zeigt.
        workers_repo.set_next_run(_WORKER, POLL_SECONDS, label="product_list check")
        products = wait_until_has_items(poll_seconds=POLL_SECONDS)
        workers_repo.set_task(_WORKER, f"scanning {len(products)} candidates")

        # State laden/sicherstellen (DB)
        opened = state_repo.get_dict(_OPENED_KEY)

        # Reihenfolge: aktuell einfach nach Key; hier könntest du auch nach Rabatt etc. sortieren
        items = sorted(products.items(), key=lambda kv: kv[0])

        total = len(items)
        # Angenommen, SKIP_TTL_SECONDS und DRY_RUN sind global definiert
        log.warning(f"[INFO] Considering {total} items. TTL={SKIP_TTL_SECONDS}s, pause={PAUSE_SECONDS}s, dry_run={DRY_RUN}")

        opened_count = 0
        skipped = 0

        # 2. Processing logic
        for idx, (asin, meta) in enumerate(items, start=1):
            url = (meta or {}).get("product_url")
            if not url:
                log.warning(f"[{idx}/{total}] [SKIP] {asin}: no product_url")
                skipped += 1
                continue

            # URL mit Opener-Fragment markieren (Addon erkennt den Tab)
            triggered_url = add_trigger_param(url)

            ok_to_open, reason = should_open(asin, url, meta, opened)
            if not ok_to_open:
                skipped += 1
                continue

            workers_repo.set_task(
                _WORKER,
                f"opening [{idx}/{total}] {asin}"
            )
            log.info(f"[{idx}/{total}] OPEN {asin} -> {canonicalize_amazon_url(url)} (Triggered)")

            if open_in_chrome(triggered_url):
                update_opened(opened, asin, url, meta)
                # Atomare Merge-Operation: schreibt NUR den neuen Eintrag in die DB,
                # nicht den kompletten in-Memory-Snapshot zurück. Dadurch überlebt
                # ein Dashboard-DB-Reset und wird nicht von alten Daten überschrieben.
                state_repo.update_dict(_OPENED_KEY, {asin: opened[asin]})
                opened_count += 1
                # Während der Pause zwischen zwei Opens: ehrlich als 'idle +
                # Countdown' melden, damit das Dashboard keinen Fake-Busy zeigt.
                workers_repo.set_next_run(
                    _WORKER, PAUSE_SECONDS,
                    label=f"next open ({idx}/{total})"
                )
                time.sleep(PAUSE_SECONDS)
            else:
                log.info(f"[{idx}/{total}] [FAIL] Could not open {asin}")

        log.warning(f"[DONE] opened={opened_count}, skipped={skipped}, total={total}]")

        # 3. Pause before checking the product list again (mit echtem Countdown)
        workers_repo.set_next_run(_WORKER, POLL_SECONDS, label="cycle restart")
        log.info(f"[opener] Cycle complete. Waiting {POLL_SECONDS}s for next check...")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("\n[opener] stopped by user")
