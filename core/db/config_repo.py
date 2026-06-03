"""
config_repo — Live-Konfiguration (Dashboard-editierbar).

Workers fragen Toggles wie `facebook.enabled` ab. Werte liegen in der Tabelle
`runtime_config`. Lese-Zugriffe sind mit einem kurzen TTL-Cache (default 5s)
gepuffert, damit Worker nicht jede Sekunde die DB hämmern.

Beispiel:
    from core.db import config_repo as cfg
    if not cfg.is_enabled("facebook"):
        log.info("⏸️  Facebook deaktiviert (Dashboard) – überspringe")
        return
    if cfg.is_worker_paused("fb_watcher"):
        return
"""
from __future__ import annotations
import threading
import time
from typing import Any

from sqlalchemy import select

from .engine import session_scope
from .models import RuntimeConfig


# ───────────────────────────────────────────────────────────────
# Defaults — werden geliefert, wenn ein Key in der DB fehlt.
# ───────────────────────────────────────────────────────────────
DEFAULTS: dict[str, Any] = {
    "facebook.enabled":     True,
    "facebook.post_reels":  True,
    "facebook.dry_run":     False,
    "facebook.skip_wait_count": 0,
    "telegram.enabled":     True,
    "telegram.dry_run":     False,
    "instagram.enabled":    True,
    "ai.enabled":           True,
    "filter.min_discount_pct": 0,
}

# Erlaubte Top-Level-Namespaces (für Dashboard-Filter)
NAMESPACES = ("facebook", "telegram", "instagram", "ai", "filter", "worker")


# ───────────────────────────────────────────────────────────────
# Cache
# ───────────────────────────────────────────────────────────────
_CACHE_TTL = 5.0
_cache: dict[str, tuple[float, Any]] = {}
_cache_lock = threading.Lock()


def _now() -> float:
    return time.time()


def _cache_get(key: str) -> tuple[bool, Any]:
    with _cache_lock:
        item = _cache.get(key)
    if not item:
        return (False, None)
    exp, val = item
    if exp < _now():
        return (False, None)
    return (True, val)


def _cache_set(key: str, val: Any) -> None:
    with _cache_lock:
        _cache[key] = (_now() + _CACHE_TTL, val)


def invalidate_cache(key: str | None = None) -> None:
    with _cache_lock:
        if key is None:
            _cache.clear()
        else:
            _cache.pop(key, None)


# ───────────────────────────────────────────────────────────────
# CRUD
# ───────────────────────────────────────────────────────────────
def get(key: str, default: Any = None) -> Any:
    hit, val = _cache_get(key)
    if hit:
        return val
    with session_scope() as s:
        row = s.get(RuntimeConfig, key)
        if row is not None:
            _cache_set(key, row.value)
            return row.value
    if default is not None:
        return default
    return DEFAULTS.get(key)


def set(key: str, value: Any, description: str | None = None) -> None:
    with session_scope() as s:
        row = s.get(RuntimeConfig, key)
        if row is None:
            s.add(RuntimeConfig(key=key, value=value, description=description))
        else:
            row.value = value
            if description is not None:
                row.description = description
    invalidate_cache(key)


def delete(key: str) -> None:
    with session_scope() as s:
        row = s.get(RuntimeConfig, key)
        if row is not None:
            s.delete(row)
    invalidate_cache(key)


def list_all() -> list[dict]:
    """Liefert alle DB-Einträge gemerged mit DEFAULTS für nicht-gesetzte Keys."""
    db_rows: dict[str, dict] = {}
    with session_scope() as s:
        rows = s.execute(select(RuntimeConfig).order_by(RuntimeConfig.key)).scalars().all()
        for r in rows:
            db_rows[r.key] = {
                "key": r.key,
                "value": r.value,
                "description": r.description,
                "source": "db",
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            }
    out = []
    for k, v in DEFAULTS.items():
        if k in db_rows:
            out.append(db_rows[k])
        else:
            out.append({
                "key": k, "value": v, "description": None,
                "source": "default", "updated_at": None,
            })
    # zusätzliche DB-Keys ohne Default
    for k in sorted(db_rows.keys()):
        if k not in DEFAULTS:
            out.append(db_rows[k])
    return out


# ───────────────────────────────────────────────────────────────
# Convenience
# ───────────────────────────────────────────────────────────────
def is_enabled(channel: str) -> bool:
    return bool(get(f"{channel}.enabled", True))


def is_dry_run(channel: str) -> bool:
    return bool(get(f"{channel}.dry_run", False))


def is_worker_paused(name: str) -> bool:
    return bool(get(f"worker.{name}.paused", False))


def consume_skip_wait_token(channel: str = "facebook") -> bool:
    """Atomar prüfen+dekrementieren: wenn skip_wait_count > 0, return True und ziehe 1 ab."""
    key = f"{channel}.skip_wait_count"
    with session_scope() as s:
        row = s.get(RuntimeConfig, key)
        cur = 0
        if row and isinstance(row.value, int):
            cur = row.value
        if cur <= 0:
            return False
        new_val = cur - 1
        if row is None:
            s.add(RuntimeConfig(key=key, value=new_val))
        else:
            row.value = new_val
    invalidate_cache(key)
    return True
