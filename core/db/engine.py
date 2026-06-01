"""
SQLAlchemy Engine + Session-Helper.
"""
from __future__ import annotations
from contextlib import contextmanager
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, Session

from core.config import DB_URL

# SQLite braucht check_same_thread=False, wenn wir aus mehreren Threads zugreifen
# (Logger-Server, Dashboard, ggf. Worker im selben Prozess).
_connect_args = {}
if DB_URL.startswith("sqlite"):
    _connect_args["check_same_thread"] = False

ENGINE = create_engine(
    DB_URL,
    echo=False,
    future=True,
    connect_args=_connect_args,
)

# WAL-Mode für SQLite: erlaubt parallele Reader während eines Writers.
if DB_URL.startswith("sqlite"):
    @event.listens_for(ENGINE, "connect")
    def _set_sqlite_pragma(dbapi_connection, _):
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

SessionLocal = sessionmaker(bind=ENGINE, expire_on_commit=False, future=True)


def init_db() -> None:
    """Erzeugt alle Tabellen, falls noch nicht vorhanden."""
    from .models import Base  # late import um zirkuläre Imports zu vermeiden
    Base.metadata.create_all(ENGINE)


@contextmanager
def session_scope() -> Session:
    """Standard-Transaktions-Scope."""
    session: Session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
