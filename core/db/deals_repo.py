"""
deals_repo — High-level API für Deal-Operationen.

Ersetzt das Lesen/Schreiben von data/deals/{queue,sent,failed}/<id>.json.
"""
from __future__ import annotations
from datetime import datetime
from typing import Iterable, Optional
from sqlalchemy import select, func

from .engine import session_scope
from .models import (
    Deal, DealEvent,
    DEAL_STATUS_QUEUE, DEAL_STATUS_PROCESSING,
    DEAL_STATUS_SENT, DEAL_STATUS_FAILED,
)


def _extract_market(payload: dict) -> str:
    return str(payload.get("market") or "UNKNOWN").upper()


# ───────────────────────────────────────────────────────────────
# Schreiben
# ───────────────────────────────────────────────────────────────

def enqueue(product_id: str, payload: dict) -> int:
    """
    Fügt einen Deal in die Queue ein. Idempotent: existiert product_id bereits
    im Status queue/processing, wird nur das payload aktualisiert.
    Liefert die DB-id.
    """
    market = _extract_market(payload)
    with session_scope() as s:
        existing: Optional[Deal] = s.execute(
            select(Deal)
            .where(Deal.product_id == product_id)
            .where(Deal.status.in_([DEAL_STATUS_QUEUE, DEAL_STATUS_PROCESSING]))
            .order_by(Deal.id.desc())
            .limit(1)
        ).scalar_one_or_none()

        if existing:
            existing.payload = payload
            existing.title = payload.get("title")
            existing.affiliate_url = payload.get("affiliate_url")
            existing.market = market
            existing.updated_at = datetime.utcnow()
            s.add(DealEvent(deal=existing, event="updated"))
            return existing.id

        deal = Deal(
            product_id=product_id,
            market=market,
            status=DEAL_STATUS_QUEUE,
            title=payload.get("title"),
            affiliate_url=payload.get("affiliate_url"),
            payload=payload,
        )
        s.add(deal)
        s.flush()
        s.add(DealEvent(deal=deal, event="created"))
        return deal.id


def claim_next(worker: str, market: Optional[str] = None) -> Optional[dict]:
    """
    Liefert den nächsten Deal aus der Queue und lockt ihn auf 'processing'.
    Atomic-ish: in SQLite reicht ein einfacher Update-by-id im Scope.
    Returns: ein dict {id, product_id, payload, ...} oder None.
    """
    with session_scope() as s:
        q = (
            select(Deal)
            .where(Deal.status == DEAL_STATUS_QUEUE)
            .order_by(Deal.created_at.asc())
            .limit(1)
        )
        if market:
            q = q.where(Deal.market == market.upper())

        deal = s.execute(q).scalar_one_or_none()
        if not deal:
            return None

        deal.status = DEAL_STATUS_PROCESSING
        deal.locked_by = worker
        deal.locked_at = datetime.utcnow()
        s.add(DealEvent(deal=deal, event="claimed", detail=worker))

        return {
            "id": deal.id,
            "product_id": deal.product_id,
            "market": deal.market,
            "payload": deal.payload,
        }


def mark_sent(deal_id: int, detail: str | None = None) -> None:
    with session_scope() as s:
        deal = s.get(Deal, deal_id)
        if not deal:
            return
        deal.status = DEAL_STATUS_SENT
        deal.locked_by = None
        deal.locked_at = None
        deal.error_message = None
        s.add(DealEvent(deal=deal, event="sent", detail=detail))


def mark_failed(deal_id: int, error: str, retry: bool = False) -> None:
    with session_scope() as s:
        deal = s.get(Deal, deal_id)
        if not deal:
            return
        deal.error_message = error
        deal.retry_count = (deal.retry_count or 0) + 1
        if retry:
            deal.status = DEAL_STATUS_QUEUE
            deal.locked_by = None
            deal.locked_at = None
            s.add(DealEvent(deal=deal, event="retry", detail=error))
        else:
            deal.status = DEAL_STATUS_FAILED
            s.add(DealEvent(deal=deal, event="failed", detail=error))


def release_stale_locks(older_than_secs: int = 600) -> int:
    """
    Setzt processing-Deals, deren Lock älter als N Sekunden ist, zurück
    in die Queue. Liefert die Anzahl der zurückgesetzten Deals.
    """
    from datetime import timedelta
    cutoff = datetime.utcnow() - timedelta(seconds=older_than_secs)
    with session_scope() as s:
        rows = s.execute(
            select(Deal)
            .where(Deal.status == DEAL_STATUS_PROCESSING)
            .where(Deal.locked_at < cutoff)
        ).scalars().all()
        for d in rows:
            d.status = DEAL_STATUS_QUEUE
            d.locked_by = None
            d.locked_at = None
            s.add(DealEvent(deal=d, event="lock_released"))
        return len(rows)


# ───────────────────────────────────────────────────────────────
# Lesen
# ───────────────────────────────────────────────────────────────

def get(deal_id: int) -> Optional[dict]:
    with session_scope() as s:
        d = s.get(Deal, deal_id)
        return _to_dict(d) if d else None


def get_by_product_id(product_id: str) -> Optional[dict]:
    with session_scope() as s:
        d = s.execute(
            select(Deal)
            .where(Deal.product_id == product_id)
            .order_by(Deal.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        return _to_dict(d) if d else None


def list_by_status(status: str, limit: int = 100) -> list[dict]:
    with session_scope() as s:
        rows = s.execute(
            select(Deal).where(Deal.status == status).order_by(Deal.created_at.desc()).limit(limit)
        ).scalars().all()
        return [_to_dict(d) for d in rows]


def list_queue(limit: int = 500) -> list[dict]:
    """Liefert alle Deals im Status 'queue' inkl. payload, sortiert ältester zuerst."""
    with session_scope() as s:
        rows = s.execute(
            select(Deal)
            .where(Deal.status == DEAL_STATUS_QUEUE)
            .order_by(Deal.created_at.asc())
            .limit(limit)
        ).scalars().all()
        return [_to_dict(d) for d in rows]


def mark_sent_by_product_id(product_id: str, detail: str | None = None) -> bool:
    """Setzt den jüngsten queue/processing-Deal mit dieser product_id auf 'sent'."""
    with session_scope() as s:
        d = s.execute(
            select(Deal)
            .where(Deal.product_id == product_id)
            .where(Deal.status.in_([DEAL_STATUS_QUEUE, DEAL_STATUS_PROCESSING]))
            .order_by(Deal.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        if not d:
            return False
        d.status = DEAL_STATUS_SENT
        d.locked_by = None
        d.locked_at = None
        d.error_message = None
        s.add(DealEvent(deal=d, event="sent", detail=detail))
        return True


def mark_failed_by_product_id(product_id: str, error: str) -> bool:
    """Setzt den jüngsten queue/processing-Deal mit dieser product_id auf 'failed'."""
    with session_scope() as s:
        d = s.execute(
            select(Deal)
            .where(Deal.product_id == product_id)
            .where(Deal.status.in_([DEAL_STATUS_QUEUE, DEAL_STATUS_PROCESSING]))
            .order_by(Deal.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        if not d:
            return False
        d.status = DEAL_STATUS_FAILED
        d.error_message = error
        d.retry_count = (d.retry_count or 0) + 1
        s.add(DealEvent(deal=d, event="failed", detail=error))
        return True



def counts_by_status() -> dict[str, int]:
    """Liefert {"queue": 3, "processing": 1, "sent": 42, "failed": 0}."""
    out = {DEAL_STATUS_QUEUE: 0, DEAL_STATUS_PROCESSING: 0, DEAL_STATUS_SENT: 0, DEAL_STATUS_FAILED: 0}
    with session_scope() as s:
        rows = s.execute(
            select(Deal.status, func.count(Deal.id)).group_by(Deal.status)
        ).all()
        for status, n in rows:
            out[status] = n
    return out


def _to_dict(d: Deal) -> dict:
    return {
        "id": d.id,
        "product_id": d.product_id,
        "market": d.market,
        "status": d.status,
        "title": d.title,
        "affiliate_url": d.affiliate_url,
        "payload": d.payload,
        "locked_by": d.locked_by,
        "retry_count": d.retry_count,
        "created_at": d.created_at.isoformat() if d.created_at else None,
        "updated_at": d.updated_at.isoformat() if d.updated_at else None,
    }


# ───────────────────────────────────────────────────────────────
# Dashboard-Operationen
# ───────────────────────────────────────────────────────────────

def requeue(deal_id: int) -> bool:
    """Setzt einen Deal (jeden Status) zurück in die Queue."""
    with session_scope() as s:
        d = s.get(Deal, deal_id)
        if not d:
            return False
        d.status = DEAL_STATUS_QUEUE
        d.locked_by = None
        d.locked_at = None
        d.error_message = None
        s.add(DealEvent(deal=d, event="requeued", detail="dashboard"))
        return True


def delete(deal_id: int) -> bool:
    """Entfernt einen Deal vollständig (inkl. Events via Cascade falls definiert)."""
    with session_scope() as s:
        d = s.get(Deal, deal_id)
        if not d:
            return False
        s.delete(d)
        return True


def get_events(deal_id: int, limit: int = 50) -> list[dict]:
    with session_scope() as s:
        rows = s.execute(
            select(DealEvent)
            .where(DealEvent.deal_id == deal_id)
            .order_by(DealEvent.created_at.desc())
            .limit(limit)
        ).scalars().all()
        return [
            {
                "event": e.event,
                "detail": e.detail,
                "created_at": e.created_at.isoformat() if e.created_at else None,
            }
            for e in rows
        ]


# ───────────────────────────────────────────────────────────────
# Pipeline-Phasen (Dashboard-Visualisierung)
# ───────────────────────────────────────────────────────────────
# Kanonische Pipeline-Stufen, die ein Link von der Erfassung bis zum
# Versand durchläuft. Jede Phase wird aus den DealEvent-Einträgen
# abgeleitet (Status: pending / active / done / failed / skipped).
PHASE_INGESTED = "ingested"      # created — Link wurde erfasst
PHASE_ENRICHED = "enriched"      # updated — Payload durch Parser/AI angereichert
PHASE_CLAIMED  = "claimed"       # claimed — Worker hat Deal übernommen
PHASE_DELIVERED = "delivered"    # sent — Versand erfolgt (Telegram/FB/…)

PHASE_DEFINITIONS: list[dict] = [
    {"key": PHASE_INGESTED,  "label": "Erfasst",       "icon": "🆕"},
    {"key": PHASE_ENRICHED,  "label": "Angereichert",  "icon": "✨"},
    {"key": PHASE_CLAIMED,   "label": "In Bearbeitung", "icon": "🔒"},
    {"key": PHASE_DELIVERED, "label": "Versendet",     "icon": "✅"},
]


def _compute_phases_from(events: list, status: str) -> list[dict]:
    """
    Berechnet kanonische Pipeline-Phasen aus einer Event-Liste.

    events: Liste von DealEvent oder dicts mit 'event', 'detail', 'created_at'.
    status: aktueller Deal-Status (queue/processing/sent/failed).

    Returns: [{key, label, icon, state, at, detail}] in Pipeline-Reihenfolge.
        state ∈ {pending, active, done, failed}
    """
    def _ev(e):
        if isinstance(e, dict):
            return e.get("event"), e.get("detail"), e.get("created_at")
        ts = e.created_at.isoformat() if e.created_at else None
        return e.event, e.detail, ts

    # Erste Vorkommnisse je Event-Typ chronologisch (ältestes zuerst)
    norm = [_ev(e) for e in events]
    norm.sort(key=lambda x: (x[2] or ""))

    first: dict[str, tuple[str | None, str | None]] = {}  # event -> (detail, at)
    last_failure: tuple[str | None, str | None] | None = None
    for ev, det, at in norm:
        if ev and ev not in first:
            first[ev] = (det, at)
        if ev == "failed":
            last_failure = (det, at)

    mapping = {
        PHASE_INGESTED: "created",
        PHASE_ENRICHED: "updated",
        PHASE_CLAIMED:  "claimed",
        PHASE_DELIVERED: "sent",
    }

    out: list[dict] = []
    for spec in PHASE_DEFINITIONS:
        key = spec["key"]
        ev_name = mapping[key]
        info = first.get(ev_name)
        phase = {**spec, "state": "pending", "at": None, "detail": None}
        if info is not None:
            phase["detail"] = info[0]
            phase["at"] = info[1]
            phase["state"] = "done"
        out.append(phase)

    # Index der letzten "done"-Phase ermitteln. Pending-Phasen davor wurden
    # offensichtlich übersprungen (z.B. wenn der Parser kein 'updated'-Event
    # ausgelöst hat, der Deal aber bereits geclaimt wurde) → 'skipped'.
    last_done_idx = -1
    for i, p in enumerate(out):
        if p["state"] == "done":
            last_done_idx = i
    for i in range(last_done_idx):
        if out[i]["state"] == "pending":
            out[i]["state"] = "skipped"

    # Erste Pending-Phase NACH der letzten Done-Phase markieren je nach Status.
    next_idx = next(
        (i for i in range(last_done_idx + 1, len(out)) if out[i]["state"] == "pending"),
        None,
    )

    if status == DEAL_STATUS_FAILED and next_idx is not None:
        out[next_idx]["state"] = "failed"
        if last_failure:
            out[next_idx]["detail"] = last_failure[0]
            out[next_idx]["at"] = last_failure[1]
    elif status == DEAL_STATUS_FAILED:
        # alle Phasen done → letzte als failed kennzeichnen
        out[-1]["state"] = "failed"
    elif status in (DEAL_STATUS_QUEUE, DEAL_STATUS_PROCESSING) and next_idx is not None:
        out[next_idx]["state"] = "active"
    # status=sent: keine aktive Phase — alles relevant abgeschlossen.

    return out


def get_phases(deal_id: int) -> list[dict] | None:
    """Liefert die berechneten Pipeline-Phasen für einen Deal."""
    with session_scope() as s:
        d = s.get(Deal, deal_id)
        if not d:
            return None
        events = s.execute(
            select(DealEvent)
            .where(DealEvent.deal_id == deal_id)
            .order_by(DealEvent.created_at.asc())
        ).scalars().all()
        return _compute_phases_from(list(events), d.status)


def list_recent_events(limit: int = 200, hours: int = 72) -> list[dict]:
    """Chronologische Liste aller Events der letzten N Stunden, mit Deal-Infos verjoint.

    Returns: [{event, detail, created_at, deal_id, product_id, market, status, title,
               post_type}]  (post_type = 'reel' | 'offer' aus payload.type, falls vorhanden)
    """
    from datetime import timedelta
    cutoff = datetime.utcnow() - timedelta(hours=max(1, hours))
    with session_scope() as s:
        rows = s.execute(
            select(DealEvent, Deal)
            .join(Deal, Deal.id == DealEvent.deal_id)
            .where(DealEvent.created_at >= cutoff)
            .order_by(DealEvent.created_at.desc())
            .limit(limit)
        ).all()
        out: list[dict] = []
        for ev, d in rows:
            payload = d.payload or {}
            post_type = str(payload.get("type") or payload.get("kind") or "offer")
            out.append({
                "event":      ev.event,
                "detail":     ev.detail,
                "created_at": ev.created_at.isoformat() if ev.created_at else None,
                "deal_id":    d.id,
                "product_id": d.product_id,
                "market":     d.market,
                "status":     d.status,
                "title":      d.title,
                "post_type":  post_type,
            })
        return out

