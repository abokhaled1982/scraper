# parser_worker.py
from __future__ import annotations
import re
import json, os, tempfile, time, hashlib, traceback
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Any, Tuple, List

from config import PRODUCT_LIST_PATH, LOCK_FILE
from amazon.amzon_dealsList_parser import parse_deals_from_html  # <-- use the deals-page parser now

# ---- file locking (POSIX preferred; no hard dep for Windows) ----
try:
    import fcntl  # type: ignore
    @contextmanager
    def _locked_file(lock_path: Path):
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "a+") as fp:
            fcntl.flock(fp.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fp.fileno(), fcntl.LOCK_UN)
except Exception:
    # Fallback: no-op lock; safe here since we have a single watcher process
    @contextmanager
    def _locked_file(lock_path: Path):
        yield

# ---- helpers ----
def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

def _is_nonempty(v: Any) -> bool:
    if v is None: return False
    if isinstance(v, (str, bytes)) and (str(v).strip() == ""): return False
    if isinstance(v, (list, dict)) and len(v) == 0: return False
    return True

def load_store(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        # Try restore from backup if exists
        bak = path.with_suffix(path.suffix + ".bak")
        if bak.exists():
            with open(bak, "r", encoding="utf-8") as f:
                return json.load(f)
        return {}

def _write_json_atomic(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name, dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp:
            json.dump(data, tmp, ensure_ascii=False, indent=2)
            tmp.flush()
            os.fsync(tmp.fileno())
        # backup
        if path.exists():
            bak = path.with_suffix(path.suffix + ".bak")
            try:
                if bak.exists():
                    bak.unlink()
                path.replace(bak)
            except Exception:
                pass
        os.replace(tmp_name, path)
    except Exception:
        try: os.unlink(tmp_name)
        except Exception: pass
        raise

def _safe_get(d: dict, *keys, default=None):
    for k in keys:
        v = d.get(k)
        if _is_nonempty(v):
            return v
    return default

# -------- store schema & logic (for DEALS PAGE rows) --------

def product_key(product: Dict[str, Any]) -> str:
    """
    Identity:
      1) product_url if present
      2) sha1 of (normalized product_name + price.value if any)
    """
    asin = (product.get("asin") or "").strip().upper()
    if re.fullmatch(r"[A-Z0-9]{10}", asin):
        return asin
    url = (product.get("product_url") or "").strip().lower()
    if url:
        return url

    name = (product.get("product_name") or "").strip().lower()
    price_val = None
    if isinstance(product.get("price"), dict):
        price_val = product["price"].get("value")
    basis = f"{name}\n{price_val if price_val is not None else ''}"
    h = hashlib.sha1()
    h.update(basis.encode("utf-8", errors="ignore"))
    key = h.hexdigest()[:16]
    print(f"[parser] fallback-key basis name='{name[:80]}' price='{price_val}' -> {key}")
    return key

def _compact_snapshot(prod: Dict[str, Any]) -> Dict[str, Any]:
    pv = None
    if isinstance(prod.get("price"), dict):
        pv = prod["price"].get("value")
    return {
        "ts": _now_iso(),
        "price": pv,
        "discount_percent": prod.get("discount_percent"),
    }

def _normalize_row(row: Dict[str, Any], source_file: str) -> Dict[str, Any]:
    """
    Convert angebot.py row -> store product object
    Row fields from angebot.parse_deals_from_html:
      - product_name, product_url, price(dict), discount_percent  :contentReference[oaicite:1]{index=1}
    """
    return {
        "asin": row.get("asin"),  # <-- keep ASIN from angebot.py
        "product_name": _safe_get(row, "product_name", default=None),
        "product_url": _safe_get(row, "product_url", default=None),
        "price": (row.get("price") if isinstance(row.get("price"), dict) else None),
        "discount_percent": row.get("discount_percent"),
        "_source_file": source_file,
    }

def _is_visible_row(row: Dict[str, Any]) -> bool:
    """
    Heuristic "visibility" check for deals cards:
      - must have a product_url OR a product_name,
      - and not be completely empty
    Price is optional (some cards may omit it).
    """
    name = (row.get("product_name") or "").strip()
    url = (row.get("product_url") or "").strip()
    has_any = bool(name or url)
    return has_any

def merge_product(store: Dict[str, Any], prod: Dict[str, Any]) -> Tuple[str, bool]:
    key = product_key(prod)
    ts = _now_iso()

    if key not in store:
        entry = dict(prod)
        entry["_first_seen"] = ts
        entry["_last_seen"]  = ts
        entry["_history"]    = []
        snap = _compact_snapshot(prod)
        if any(v is not None for k, v in snap.items() if k != "ts"):
            entry["_history"].append(snap)
        store[key] = entry
        return key, True

    # update existing
    entry = store[key]
    for k, v in prod.items():
        if _is_nonempty(v):
            entry[k] = v
    entry["_last_seen"] = ts

    snap = _compact_snapshot(prod)
    if any(v is not None for k, v in snap.items() if k != "ts"):
        entry.setdefault("_history", [])
        entry["_history"].append(snap)
        if len(entry["_history"]) > 5:
            entry["_history"] = entry["_history"][-5:]
    store[key] = entry
    return key, False

# ---- main entrypoint for watcher ----
def parse_and_merge(html_path: Path) -> Dict[str, Any]:
    """
    Parse ONE deals HTML → many products, merge them into product_list.json.
    Returns a summary dict: {'parsed': N, 'visible': V, 'new': X, 'updated': Y}
    """
    raw = html_path.read_text(encoding="utf-8", errors="ignore")

    # Parse all deal cards from this HTML
    rows: List[Dict[str, Any]] = parse_deals_from_html(raw)  # provided by angebot.py  :contentReference[oaicite:2]{index=2}
    print(f"[parser] file='{html_path.name}' parsed_rows={len(rows)}")

    # Pre-merge logging: show each row's compact summary
    for i, r in enumerate(rows, 1):
        pv = r.get("price", {}) if isinstance(r.get("price"), dict) else {}
        print(
            f"[parser] row#{i}: name='{(r.get('product_name') or '')[:80]}' "
            f"url='{(r.get('product_url') or '')[:120]}' "
            f"price={pv.get('value')} discount={r.get('discount_percent')}"
        )

    # Filter "visible" rows
    vis_rows = [r for r in rows if _is_visible_row(r)]
    if len(vis_rows) != len(rows):
        print(f"[parser] filtered non-visible rows: {len(rows) - len(vis_rows)} (kept {len(vis_rows)})")

    # Critical section: lock once for load -> merge all -> write
    new_count = 0
    upd_count = 0
    with _locked_file(LOCK_FILE):
        store = load_store(PRODUCT_LIST_PATH)
        before = len(store)
        print(f"[parser] store size before: {before}")

        for r in vis_rows:
            prod = _normalize_row(r, source_file=str(html_path))
            key_preview = product_key(prod)
            key, is_new = merge_product(store, prod)
            print(f"[parser] MERGE {('NEW' if is_new else 'UPDATED'):7} key={key} preview={key_preview}")
            if is_new: new_count += 1
            else:      upd_count += 1

        _write_json_atomic(PRODUCT_LIST_PATH, store)
        after = len(store)
        print(f"[parser] store size after:  {after} (+{after - before})")

    return {"parsed": len(rows), "visible": len(vis_rows), "new": new_count, "updated": upd_count}
