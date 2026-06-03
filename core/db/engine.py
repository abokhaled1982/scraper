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
    """Erzeugt alle Tabellen, falls noch nicht vorhanden, und führt
    leichte Inplace-Migrations für SQLite (neue Spalten) aus."""
    from .models import Base  # late import um zirkuläre Imports zu vermeiden
    Base.metadata.create_all(ENGINE)
    _ensure_columns_sqlite()


def _ensure_columns_sqlite() -> None:
    """SQLite kann mit ALTER TABLE ADD COLUMN; create_all() ergänzt keine
    fehlenden Spalten an existierenden Tabellen. Hier idempotent nachziehen."""
    if not DB_URL.startswith("sqlite"):
        return
    # Liste der erwarteten Zusatz-Spalten (Tabelle → [(name, DDL)])
    expected: dict[str, list[tuple[str, str]]] = {
        "deals": [
            ("priority", "INTEGER NOT NULL DEFAULT 0"),
        ],
    }
    with ENGINE.begin() as conn:
        from sqlalchemy import text
        for table, cols in expected.items():
            try:
                rows = conn.execute(text(f"PRAGMA table_info({table})")).all()
            except Exception:
                continue
            have = {r[1] for r in rows}
            for name, ddl in cols:
                if name not in have:
                    try:
                        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
                    except Exception:
                        pass


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


# ───────────────────────────────────────────────────────────────
# Admin: Backup + Reset (vom Dashboard aufgerufen)
# ───────────────────────────────────────────────────────────────

def _sqlite_file_path() -> "Path | None":
    """Liefert den Dateipfad der SQLite-DB oder None (falls non-SQLite)."""
    from pathlib import Path
    if not DB_URL.startswith("sqlite:///"):
        return None
    return Path(DB_URL.replace("sqlite:///", "", 1)).resolve()


def backup_db() -> str:
    """Erzeugt einen konsistenten Snapshot der SQLite-DB in db/backups/.
    Nutzt die SQLite-Backup-API → safe auch wenn Worker schreiben.
    Gibt den Pfad zur Backup-Datei zurück.
    """
    import sqlite3
    from datetime import datetime
    src = _sqlite_file_path()
    if src is None:
        raise RuntimeError("Backup nur für SQLite unterstützt")
    backup_dir = src.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = backup_dir / f"core_data.backup-{ts}.db"
    src_conn = sqlite3.connect(str(src))
    dst_conn = sqlite3.connect(str(dst))
    try:
        src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()
    return str(dst)


def reset_db(make_backup: bool = True) -> dict:
    """Setzt die Datenbank zurück: optionales Backup → drop_all → create_all.
    ACHTUNG: Löscht alle Deals, Events, State, Config, Worker-Status.
    """
    from .models import Base
    info: dict = {"backup": None}
    if make_backup:
        try:
            info["backup"] = backup_db()
        except Exception as e:
            info["backup_error"] = str(e)
    # WAL/SHM zur Sicherheit leeren bevor wir das Schema neu aufbauen
    try:
        with ENGINE.begin() as conn:
            from sqlalchemy import text
            conn.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
    except Exception:
        pass
    Base.metadata.drop_all(ENGINE)
    Base.metadata.create_all(ENGINE)
    _ensure_columns_sqlite()
    info["ok"] = True
    return info
