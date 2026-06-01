"""
DB-Schicht — zentraler Zugriff auf SQLite (später Postgres).

Öffentliche API:
    from core.db import deals_repo, state_repo, workers_repo, init_db
"""
from .engine import init_db, session_scope, ENGINE
from . import deals_repo, state_repo, workers_repo

__all__ = [
    "init_db",
    "session_scope",
    "ENGINE",
    "deals_repo",
    "state_repo",
    "workers_repo",
]
