"""
Migrations-Skript — einmaliger Import.

Importiert:
    data/deals/queue/*.json   → deals (status=queue)
    data/deals/sent/*.json    → deals (status=sent)
    data/deals/failed/*.json  → deals (status=failed)
    data/state/sent_ids.json     → state_kv['sent_ids']
    data/state/sent_asins.json   → state_kv['sent_asins']
    data/state/product_list.json → state_kv['product_list']
    data/state/.opened.json      → state_kv['opened']
    data/state/.registry.json    → state_kv['registry']

Nach erfolgreichem Import werden die JSONs nach data/_archive_migration/<timestamp>/
verschoben (nicht gelöscht – Sicherheitskopie).

Aufruf:
    python -m core.migrate
"""
from __future__ import annotations
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

from core.db import init_db, deals_repo, state_repo
from core.db.models import (
    DEAL_STATUS_QUEUE, DEAL_STATUS_SENT, DEAL_STATUS_FAILED,
)
from core.db.engine import session_scope
from core.db.models import Deal, DealEvent

# Lokale Konfig (NICHT die alte config.py importieren um Print-Spam zu vermeiden)
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"

QUEUE_DIR  = DATA_DIR / "deals" / "queue"
SENT_DIR   = DATA_DIR / "deals" / "sent"
FAILED_DIR = DATA_DIR / "deals" / "failed"
STATE_DIR  = DATA_DIR / "state"


def _load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"  ⚠ skip {path.name}: {e}", file=sys.stderr)
        return None


def _import_deals_dir(dir_path: Path, status: str) -> int:
    if not dir_path.exists():
        return 0
    count = 0
    for fp in sorted(dir_path.glob("*.json")):
        payload = _load_json(fp)
        if not isinstance(payload, dict):
            continue
        product_id = (
            payload.get("product_id")
            or payload.get("asin")
            or fp.stem
        )
        # Direktes Insert mit gewünschtem Status (enqueue() würde alles in 'queue' legen)
        with session_scope() as s:
            market = str(payload.get("market") or "UNKNOWN").upper()
            deal = Deal(
                product_id=product_id,
                market=market,
                status=status,
                title=payload.get("title"),
                affiliate_url=payload.get("affiliate_url"),
                payload=payload,
            )
            s.add(deal)
            s.flush()
            s.add(DealEvent(deal=deal, event=f"imported_{status}"))
        count += 1
    print(f"  → {dir_path.name}: {count} deals")
    return count


def _import_state() -> None:
    if not STATE_DIR.exists():
        return
    mappings = {
        "sent_ids.json":     "sent_ids",
        "sent_asins.json":   "sent_asins",
        "product_list.json": "product_list",
        ".opened.json":      "opened",
        ".registry.json":    "registry",
    }
    for fname, key in mappings.items():
        fp = STATE_DIR / fname
        if not fp.exists():
            continue
        val = _load_json(fp)
        if val is None:
            continue
        state_repo.put(key, val)
        print(f"  → state['{key}'] ← {fname}")


def _archive_after_import() -> None:
    """Verschiebt importierte Quelldateien nach data/_archive_migration/<ts>/."""
    if not DATA_DIR.exists():
        return
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive_root = DATA_DIR / "_archive_migration" / ts
    archive_root.mkdir(parents=True, exist_ok=True)
    for src in [QUEUE_DIR, SENT_DIR, FAILED_DIR, STATE_DIR]:
        if src.exists():
            dst = archive_root / src.relative_to(DATA_DIR)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            print(f"  archived: {src} → {dst}")


def main() -> None:
    print("=== core.migrate: JSON → DB ===")
    init_db()

    total = 0
    print("[deals]")
    total += _import_deals_dir(QUEUE_DIR,  DEAL_STATUS_QUEUE)
    total += _import_deals_dir(SENT_DIR,   DEAL_STATUS_SENT)
    total += _import_deals_dir(FAILED_DIR, DEAL_STATUS_FAILED)

    print("[state]")
    _import_state()

    print(f"\n✔ Importiert: {total} deals")

    if "--archive" in sys.argv:
        print("\n[archive] verschiebe Quelldateien …")
        _archive_after_import()
    else:
        print("\nℹ Originaldateien bleiben liegen. Mit '--archive' verschieben.")


if __name__ == "__main__":
    main()
