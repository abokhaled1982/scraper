"""
state_repo — generischer Key/Value-Store.

Ersetzt:
    data/state/sent_ids.json    → state_repo.get_set("sent_ids:facebook")
    data/state/sent_asins.json  → state_repo.get_set("sent_asins")
    data/state/product_list.json→ state_repo.get_dict("product_list")
    data/state/.opened.json     → state_repo.get_set("opened")
    data/state/.registry.json   → state_repo.get_dict("registry")
"""
from __future__ import annotations
from typing import Any, Iterable
from sqlalchemy import select

from .engine import session_scope
from .models import StateKV


# ───────────────────────────────────────────────────────────────
# Basis
# ───────────────────────────────────────────────────────────────

def get(key: str, default: Any = None) -> Any:
    with session_scope() as s:
        row = s.get(StateKV, key)
        return row.value if row is not None else default


def put(key: str, value: Any) -> None:
    with session_scope() as s:
        row = s.get(StateKV, key)
        if row is None:
            s.add(StateKV(key=key, value=value))
        else:
            row.value = value


def delete(key: str) -> None:
    with session_scope() as s:
        row = s.get(StateKV, key)
        if row:
            s.delete(row)


# ───────────────────────────────────────────────────────────────
# Convenience: Set-Semantik (dedupe-Listen wie sent_asins)
# ───────────────────────────────────────────────────────────────

def get_set(key: str) -> set[str]:
    val = get(key, [])
    if isinstance(val, list):
        return set(val)
    if isinstance(val, dict):
        # Migration aus Form {"asin": [...]}
        for v in val.values():
            if isinstance(v, list):
                return set(v)
    return set()


def add_to_set(key: str, *items: str) -> None:
    with session_scope() as s:
        row = s.get(StateKV, key)
        current = set()
        if row and isinstance(row.value, list):
            current = set(row.value)
        current.update(items)
        new_val = sorted(current)
        if row is None:
            s.add(StateKV(key=key, value=new_val))
        else:
            row.value = new_val


def is_in_set(key: str, item: str) -> bool:
    return item in get_set(key)


# ───────────────────────────────────────────────────────────────
# Convenience: Dict-Semantik
# ───────────────────────────────────────────────────────────────

def get_dict(key: str) -> dict:
    val = get(key, {})
    return val if isinstance(val, dict) else {}


def update_dict(key: str, updates: dict) -> None:
    with session_scope() as s:
        row = s.get(StateKV, key)
        current = row.value if (row and isinstance(row.value, dict)) else {}
        current.update(updates)
        if row is None:
            s.add(StateKV(key=key, value=current))
        else:
            row.value = current
