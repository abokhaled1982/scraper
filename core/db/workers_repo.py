"""
workers_repo — Heartbeat + Status pro Worker.

Worker rufen periodisch heartbeat() / set_task() auf,
das Dashboard liest list_all() / counts().
"""
from __future__ import annotations
import os
from datetime import datetime, timedelta
from typing import Optional
from sqlalchemy import select

from .engine import session_scope
from .models import (
    Worker, WORKER_STATE_IDLE, WORKER_STATE_BUSY,
    WORKER_STATE_ERROR, WORKER_STATE_STOPPED,
)
from core.config import WORKER_STALE_AFTER_SECS


def register(name: str, pid: Optional[int] = None) -> None:
    pid = pid or os.getpid()
    with session_scope() as s:
        w = s.get(Worker, name)
        now = datetime.utcnow()
        if w is None:
            s.add(Worker(
                name=name, pid=pid, state=WORKER_STATE_IDLE,
                started_at=now, last_heartbeat=now, current_task=None, stats={},
            ))
        else:
            w.pid = pid
            w.state = WORKER_STATE_IDLE
            w.started_at = now
            w.last_heartbeat = now
            w.current_task = None


def heartbeat(name: str, state: str = WORKER_STATE_IDLE,
              current_task: Optional[str] = None,
              stats: Optional[dict] = None) -> None:
    with session_scope() as s:
        w = s.get(Worker, name)
        if w is None:
            w = Worker(name=name, pid=os.getpid())
            s.add(w)
        w.state = state
        w.last_heartbeat = datetime.utcnow()
        if current_task is not None:
            w.current_task = current_task
        if stats is not None:
            merged = dict(w.stats or {})
            merged.update(stats)
            w.stats = merged


def set_task(name: str, task: str) -> None:
    heartbeat(name, state=WORKER_STATE_BUSY, current_task=task)


def set_idle(name: str) -> None:
    heartbeat(name, state=WORKER_STATE_IDLE, current_task=None)


def set_next_run(name: str, secs: float, label: str = "next tick") -> None:
    """Markiert den Worker als idle und merkt den nächsten geplanten Tick.

    Speichert ``next_run_at`` (UTC-ISO) in den Worker-Stats, damit
    Dashboard und TUI einen Live-Countdown anzeigen können.
    Setzt zusätzlich ``current_task`` auf z.B. ``"sleeping (next in 10s)"``,
    sodass die Info auch ohne weitere UI-Änderungen sichtbar ist.
    """
    next_at = datetime.utcnow() + timedelta(seconds=max(0.0, float(secs)))
    task = f"sleeping → {label} in {int(secs)}s"
    heartbeat(
        name,
        state=WORKER_STATE_IDLE,
        current_task=task,
        stats={"next_run_at": next_at.isoformat(), "next_run_secs": int(secs)},
    )


def set_error(name: str, msg: str) -> None:
    heartbeat(name, state=WORKER_STATE_ERROR, current_task=msg)


def set_stopped(name: str) -> None:
    with session_scope() as s:
        w = s.get(Worker, name)
        if w:
            w.state = WORKER_STATE_STOPPED
            w.current_task = None


def list_all() -> list[dict]:
    cutoff = datetime.utcnow() - timedelta(seconds=WORKER_STALE_AFTER_SECS)
    with session_scope() as s:
        rows = s.execute(select(Worker).order_by(Worker.name)).scalars().all()
        out = []
        for w in rows:
            stale = (w.state != WORKER_STATE_STOPPED) and (w.last_heartbeat < cutoff)
            out.append({
                "name": w.name,
                "state": w.state if not stale else "stale",
                "current_task": w.current_task,
                "pid": w.pid,
                "last_heartbeat": w.last_heartbeat.isoformat() if w.last_heartbeat else None,
                "started_at": w.started_at.isoformat() if w.started_at else None,
                "stats": w.stats or {},
            })
        return out
