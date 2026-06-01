"""
SQLAlchemy-Models — zentrales Schema.

Tabellen:
    deals         — Produkte/Angebote, ersetzt data/deals/{queue,sent,failed}/*.json
    deal_events   — History (created/sent/failed/retry) pro Deal
    state_kv      — Generischer Key/Value-Store (ersetzt sent_ids.json, etc.)
    workers       — Worker-Heartbeats + aktueller Status (für Dashboard)
"""
from __future__ import annotations
from datetime import datetime
from sqlalchemy import (
    String, Integer, DateTime, Text, JSON, ForeignKey, Index, UniqueConstraint
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# ───────────────────────────────────────────────────────────────
# Deals
# ───────────────────────────────────────────────────────────────
# Status-Lebenszyklus:
#   queue → processing → sent
#                     ↘ failed → (retry) → queue
DEAL_STATUS_QUEUE = "queue"
DEAL_STATUS_PROCESSING = "processing"
DEAL_STATUS_SENT = "sent"
DEAL_STATUS_FAILED = "failed"


class Deal(Base):
    __tablename__ = "deals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # product_id ist ASIN (Amazon) oder Mydealz-ID etc. – fachlicher Identifier
    product_id: Mapped[str] = mapped_column(String(64), index=True)
    market: Mapped[str] = mapped_column(String(32), index=True, default="UNKNOWN")
    status: Mapped[str] = mapped_column(String(16), index=True, default=DEAL_STATUS_QUEUE)

    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    affiliate_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Komplettes Deal-JSON (so wie es heute in queue/*.json liegt).
    # JSON-Typ ist in SQLite TEXT – SQLAlchemy serialisiert für uns.
    payload: Mapped[dict] = mapped_column(JSON, default=dict)

    # Worker, der den Deal aktuell bearbeitet (für processing-Lock)
    locked_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    events: Mapped[list["DealEvent"]] = relationship(
        back_populates="deal", cascade="all, delete-orphan"
    )

    __table_args__ = (
        # Ein product_id darf nur einmal aktiv im System sein, aber historisch
        # mehrfach (z.B. „sent" und dann erneut „queue") – wir lassen das offen
        # und arbeiten mit upsert in deals_repo.
        Index("ix_deals_status_market", "status", "market"),
    )


class DealEvent(Base):
    __tablename__ = "deal_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    deal_id: Mapped[int] = mapped_column(
        ForeignKey("deals.id", ondelete="CASCADE"), index=True
    )
    event: Mapped[str] = mapped_column(String(32))   # created/sent/failed/retry/…
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    deal: Mapped[Deal] = relationship(back_populates="events")


# ───────────────────────────────────────────────────────────────
# Generischer Key/Value-Store
# ───────────────────────────────────────────────────────────────
# Ersetzt data/state/sent_ids.json, sent_asins.json, product_list.json etc.
# Key ist ein freier Namespace, z.B. "sent_asins:amazon", value ist JSON.
class StateKV(Base):
    __tablename__ = "state_kv"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[dict | list | str | int | float | bool | None] = mapped_column(
        JSON, default=None
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


# ───────────────────────────────────────────────────────────────
# Workers — Heartbeat + aktueller Status für Dashboard
# ───────────────────────────────────────────────────────────────
WORKER_STATE_IDLE = "idle"
WORKER_STATE_BUSY = "busy"
WORKER_STATE_ERROR = "error"
WORKER_STATE_STOPPED = "stopped"


class Worker(Base):
    __tablename__ = "workers"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    state: Mapped[str] = mapped_column(String(16), default=WORKER_STATE_IDLE)
    # Was tut der Worker gerade? z.B. "parsing B0DSLBN5FS" oder "polling queue"
    current_task: Mapped[str | None] = mapped_column(Text, nullable=True)
    pid: Mapped[int | None] = mapped_column(Integer, nullable=True)

    last_heartbeat: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    # Frei nutzbare Stats (z.B. {"processed": 42, "failed": 3})
    stats: Mapped[dict] = mapped_column(JSON, default=dict)
