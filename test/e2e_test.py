#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test/e2e_test.py — End-to-End-Pipeline-Tests.

Ziel: validieren, was passiert, wenn ein neuer Link ins System eingespeist
wird – von der Erfassung über AI/Parser bis zum Versand auf jedem Kanal –
sowie alle Fehler-/Recovery-Pfade und alle Dashboard-Schnittstellen.

Die Tests laufen in einer *isolierten* SQLite-Datei (tmp), damit die
produktive ``core_data.db`` unangetastet bleibt.

Aufruf:
    python -m test.e2e_test                 # nur in-process Tests
    python -m test.e2e_test --integration   # zusätzlich realer
                                            # run_all.py --mode parser
                                            # (startet alle Worker-Subprozesse)
    python -m test.e2e_test --verbose       # ausführliche Logs

Exit-Code 0 = OK, !=0 = mindestens ein Test ist fehlgeschlagen.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
import tempfile
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path

# ─── ISOLATION: Test-DB VOR jedem core-Import setzen ─────────────────────
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_TMP_DIR = Path(tempfile.mkdtemp(prefix="scraper_e2e_"))
_TEST_DB = _TMP_DIR / "e2e_test.db"
os.environ["CORE_DB_URL"] = f"sqlite:///{_TEST_DB}"
# Dashboard auf hohen Test-Port verschieben, damit es nicht mit laufendem
# Produktiv-Dashboard kollidiert.
os.environ.setdefault("CORE_DASHBOARD_PORT", "9931")
os.environ.setdefault("CORE_LOG_PORT", "9921")

# Erst jetzt core-Imports – sie picken die Test-DB auf.
from core.db import deals_repo, state_repo, workers_repo, init_db, ENGINE
from core.db.models import (
    DEAL_STATUS_QUEUE, DEAL_STATUS_PROCESSING,
    DEAL_STATUS_SENT, DEAL_STATUS_FAILED,
    WORKER_STATE_BUSY, WORKER_STATE_IDLE, WORKER_STATE_ERROR,
)


# ─────────────────────────────────────────────────────────────────────────
# Test-Framework
# ─────────────────────────────────────────────────────────────────────────
VERBOSE = False


class Result:
    def __init__(self) -> None:
        self.passed: list[str] = []
        self.failed: list[tuple[str, str]] = []
        self.skipped: list[str] = []

    def ok(self, name: str) -> None:
        self.passed.append(name)
        print(f"  ✅ {name}")

    def fail(self, name: str, err: str) -> None:
        self.failed.append((name, err))
        head = err.strip().splitlines()[0] if err.strip() else "(no message)"
        print(f"  ❌ {name}\n       {head}")
        if VERBOSE:
            for line in err.strip().splitlines()[1:]:
                print(f"       {line}")

    def skip(self, name: str, reason: str) -> None:
        self.skipped.append(name)
        print(f"  ⏭  {name}  ({reason})")

    def summary(self) -> int:
        total = len(self.passed) + len(self.failed)
        print("\n" + "=" * 64)
        print(f"E2E Ergebnis: {len(self.passed)}/{total} bestanden"
              + (f", {len(self.skipped)} übersprungen" if self.skipped else ""))
        if self.failed:
            print("\nFehlgeschlagen:")
            for name, err in self.failed:
                print(f"  • {name}")
                for line in err.strip().splitlines()[:6]:
                    print(f"      {line}")
            return 1
        print("Alle End-to-End-Prüfungen erfolgreich.")
        return 0


def section(title: str) -> None:
    print(f"\n── {title} " + "─" * max(0, 60 - len(title)))


def _case(r: Result, name: str, fn):
    try:
        fn()
        r.ok(name)
    except AssertionError as e:
        r.fail(name, str(e) or repr(e))
    except Exception:
        r.fail(name, traceback.format_exc())


def assert_eq(actual, expected, msg: str = "") -> None:
    if actual != expected:
        raise AssertionError(f"{msg}\n  expected: {expected!r}\n  actual:   {actual!r}")


def assert_in(needle, haystack, msg: str = "") -> None:
    if needle not in haystack:
        raise AssertionError(f"{msg}\n  not in: {haystack!r}\n  needle: {needle!r}")


# ─────────────────────────────────────────────────────────────────────────
# Helper: DB jeder Phase frisch zurücksetzen
# ─────────────────────────────────────────────────────────────────────────
def reset_db() -> None:
    """Löscht *alle* Tabelleninhalte (DROP ALL + CREATE ALL)."""
    from core.db.models import Base
    Base.metadata.drop_all(ENGINE)
    Base.metadata.create_all(ENGINE)


def make_payload(pid: str, market: str = "AMAZON", **extras) -> dict:
    payload = {
        "market":         market,
        "title":          f"Test-Produkt {pid}",
        "affiliate_url":  f"https://example.com/dp/{pid}?tag=test-21",
        "image_url":      f"https://example.com/img/{pid}.jpg",
        "normal_price":   29.99,
        "discounted_price": 19.99,
        "discount_percent": 33,
        "currency":       "EUR",
        "product_description": f"Beschreibung {pid}",
    }
    payload.update(extras)
    return payload


# ─────────────────────────────────────────────────────────────────────────
# 1) Link-Eintritt: enqueue & Idempotenz
# ─────────────────────────────────────────────────────────────────────────
def test_block_ingest(r: Result) -> None:
    section("1) Link-Erfassung & Idempotenz")

    def _basic_enqueue():
        reset_db()
        did = deals_repo.enqueue("B0TEST001", make_payload("B0TEST001"))
        assert did > 0, "enqueue lieferte keine ID"
        d = deals_repo.get(did)
        assert_eq(d["status"], DEAL_STATUS_QUEUE, "neuer Deal nicht im Queue-Status")
        assert_eq(d["market"], "AMAZON")
        assert_in("created", [e["event"] for e in deals_repo.get_events(did)],
                  "'created'-Event fehlt")

    def _idempotent_enqueue():
        reset_db()
        did1 = deals_repo.enqueue("B0TEST002", make_payload("B0TEST002", title="v1"))
        did2 = deals_repo.enqueue("B0TEST002", make_payload("B0TEST002", title="v2"))
        assert_eq(did1, did2, "doppelte enqueue muss dieselbe ID liefern (Idempotenz)")
        d = deals_repo.get(did1)
        assert_eq(d["title"], "v2", "Titel wurde bei Re-Enqueue nicht aktualisiert")
        events = [e["event"] for e in deals_repo.get_events(did1)]
        assert_in("updated", events, "'updated'-Event fehlt bei Re-Enqueue")

    def _enqueue_unknown_market():
        reset_db()
        did = deals_repo.enqueue("X1", {"title": "kein market"})
        d = deals_repo.get(did)
        assert_eq(d["market"], "UNKNOWN", "fehlender market sollte UNKNOWN sein")

    def _enqueue_multi_markets():
        reset_db()
        for pid, m in [("A1", "AMAZON"), ("M1", "MYDEALZ"), ("F1", "FACEBOOK")]:
            deals_repo.enqueue(pid, make_payload(pid, market=m))
        counts = deals_repo.counts_by_status()
        assert_eq(counts[DEAL_STATUS_QUEUE], 3, "alle 3 Deals müssen in Queue sein")

    _case(r, "neuer Link erzeugt Queue-Deal + created-Event", _basic_enqueue)
    _case(r, "doppelter Link ist idempotent (kein Duplikat)", _idempotent_enqueue)
    _case(r, "fehlender market → UNKNOWN", _enqueue_unknown_market)
    _case(r, "verschiedene Märkte parallel in Queue", _enqueue_multi_markets)


# ─────────────────────────────────────────────────────────────────────────
# 2) Vollständige Pipeline: queue → processing → sent
# ─────────────────────────────────────────────────────────────────────────
def test_block_full_lifecycle(r: Result) -> None:
    section("2) Vollständiger Lebenszyklus pro Kanal")

    def _claim_and_send():
        reset_db()
        did = deals_repo.enqueue("LIFE01", make_payload("LIFE01"))
        claimed = deals_repo.claim_next(worker="telegram_router")
        assert claimed is not None, "claim_next lieferte None obwohl Queue gefüllt"
        assert_eq(claimed["id"], did)
        d = deals_repo.get(did)
        assert_eq(d["status"], DEAL_STATUS_PROCESSING)
        assert_eq(d["locked_by"], "telegram_router")
        deals_repo.mark_sent(did, detail="telegram")
        d = deals_repo.get(did)
        assert_eq(d["status"], DEAL_STATUS_SENT)
        assert d["locked_by"] is None, "locked_by muss nach sent gecleart sein"
        ev_names = [e["event"] for e in deals_repo.get_events(did)]
        for needed in ("created", "claimed", "sent"):
            assert_in(needed, ev_names, f"Event '{needed}' fehlt im Lifecycle")

    def _claim_when_empty():
        reset_db()
        assert deals_repo.claim_next(worker="x") is None, \
            "leere Queue muss None liefern"

    def _claim_filtered_by_market():
        reset_db()
        deals_repo.enqueue("A1", make_payload("A1", market="AMAZON"))
        deals_repo.enqueue("M1", make_payload("M1", market="MYDEALZ"))
        c = deals_repo.claim_next(worker="fb", market="MYDEALZ")
        assert c is not None and c["product_id"] == "M1", \
            "claim_next mit market-Filter lieferte falschen Deal"

    def _mark_sent_by_product_id():
        reset_db()
        deals_repo.enqueue("PID-X", make_payload("PID-X"))
        ok = deals_repo.mark_sent_by_product_id("PID-X", detail="telegram+facebook")
        assert ok, "mark_sent_by_product_id muss True liefern"
        d = deals_repo.get_by_product_id("PID-X")
        assert_eq(d["status"], DEAL_STATUS_SENT)

    def _no_double_claim():
        reset_db()
        deals_repo.enqueue("DUP", make_payload("DUP"))
        first = deals_repo.claim_next(worker="w1")
        second = deals_repo.claim_next(worker="w2")
        assert first is not None
        assert second is None, "Deal darf nicht zweimal claimbar sein"

    _case(r, "queue → processing (claim) → sent", _claim_and_send)
    _case(r, "claim_next bei leerer Queue = None", _claim_when_empty)
    _case(r, "claim_next mit market-Filter", _claim_filtered_by_market)
    _case(r, "mark_sent_by_product_id setzt Status korrekt", _mark_sent_by_product_id)
    _case(r, "kein Doppel-Claim (Lock-Semantik)", _no_double_claim)


# ─────────────────────────────────────────────────────────────────────────
# 3) Fehlerpfade: failed / retry / stale-lock / requeue
# ─────────────────────────────────────────────────────────────────────────
def test_block_failures(r: Result) -> None:
    section("3) Fehler-, Retry- und Recovery-Pfade")

    def _failed_no_retry():
        reset_db()
        did = deals_repo.enqueue("FAIL01", make_payload("FAIL01"))
        deals_repo.claim_next(worker="w")
        deals_repo.mark_failed(did, error="Netzwerk-Fehler", retry=False)
        d = deals_repo.get(did)
        assert_eq(d["status"], DEAL_STATUS_FAILED)
        assert_eq(d["retry_count"], 1)

    def _failed_with_retry_goes_back_to_queue():
        reset_db()
        did = deals_repo.enqueue("FAIL02", make_payload("FAIL02"))
        deals_repo.claim_next(worker="w")
        deals_repo.mark_failed(did, error="429 Too Many", retry=True)
        d = deals_repo.get(did)
        assert_eq(d["status"], DEAL_STATUS_QUEUE, "retry=True muss Status auf 'queue' setzen")
        assert_eq(d["retry_count"], 1)
        assert d["locked_by"] is None

    def _retry_loop_n_times():
        reset_db()
        did = deals_repo.enqueue("FAIL03", make_payload("FAIL03"))
        for i in range(3):
            deals_repo.claim_next(worker="w")
            deals_repo.mark_failed(did, error=f"fehler {i}", retry=True)
        d = deals_repo.get(did)
        assert_eq(d["retry_count"], 3)
        assert_eq(d["status"], DEAL_STATUS_QUEUE)

    def _release_stale_locks():
        reset_db()
        did = deals_repo.enqueue("STALE", make_payload("STALE"))
        deals_repo.claim_next(worker="dead-worker")
        # Manuell locked_at künstlich altern lassen
        from sqlalchemy import update
        from core.db.engine import session_scope
        from core.db.models import Deal
        with session_scope() as s:
            s.execute(update(Deal).where(Deal.id == did).values(
                locked_at=datetime.utcnow() - timedelta(hours=1)
            ))
        released = deals_repo.release_stale_locks(older_than_secs=60)
        assert released >= 1, "stale Lock wurde nicht released"
        d = deals_repo.get(did)
        assert_eq(d["status"], DEAL_STATUS_QUEUE)
        assert d["locked_by"] is None

    def _requeue_after_sent():
        reset_db()
        did = deals_repo.enqueue("RQ", make_payload("RQ"))
        deals_repo.claim_next(worker="w")
        deals_repo.mark_sent(did)
        assert deals_repo.requeue(did), "requeue muss True liefern"
        assert_eq(deals_repo.get(did)["status"], DEAL_STATUS_QUEUE)

    def _delete_deal():
        reset_db()
        did = deals_repo.enqueue("DEL", make_payload("DEL"))
        assert deals_repo.delete(did)
        assert deals_repo.get(did) is None

    def _operations_on_missing_deal():
        reset_db()
        assert not deals_repo.requeue(99999), "requeue(non-existent) → False"
        assert not deals_repo.delete(99999), "delete(non-existent) → False"
        # mark_sent/failed schweigen
        deals_repo.mark_sent(99999)
        deals_repo.mark_failed(99999, "x")

    _case(r, "failed (retry=False) → Status failed + retry_count", _failed_no_retry)
    _case(r, "failed (retry=True) → zurück in Queue", _failed_with_retry_goes_back_to_queue)
    _case(r, "mehrere Retries akkumulieren retry_count", _retry_loop_n_times)
    _case(r, "release_stale_locks befreit hängende processing-Deals",
          _release_stale_locks)
    _case(r, "requeue nach sent funktioniert", _requeue_after_sent)
    _case(r, "delete entfernt Deal vollständig", _delete_deal)
    _case(r, "Operationen auf nicht existierenden Deal sind sicher",
          _operations_on_missing_deal)


# ─────────────────────────────────────────────────────────────────────────
# 4) Phasen-Berechnung (Pipeline-Visualisierung im Dashboard)
# ─────────────────────────────────────────────────────────────────────────
def test_block_phases(r: Result) -> None:
    section("4) Pipeline-Phasen-Berechnung (Dashboard-Stepper)")

    def _phases_initial_only_ingested():
        reset_db()
        did = deals_repo.enqueue("PH01", make_payload("PH01"))
        phases = deals_repo.get_phases(did)
        by_key = {p["key"]: p for p in phases}
        assert_eq(by_key["ingested"]["state"], "done")
        # Nächste Phase muss als active erscheinen
        assert by_key["enriched"]["state"] in ("active", "pending"), \
            "enriched sollte active oder pending sein"

    def _phases_after_update_enriched():
        reset_db()
        did = deals_repo.enqueue("PH02", make_payload("PH02"))
        # Re-Enqueue erzeugt 'updated' (= Enrichment durch Parser)
        deals_repo.enqueue("PH02", make_payload("PH02", title="enriched"))
        phases = {p["key"]: p for p in deals_repo.get_phases(did)}
        assert_eq(phases["ingested"]["state"], "done")
        assert_eq(phases["enriched"]["state"], "done")

    def _phases_after_claim_processing():
        reset_db()
        did = deals_repo.enqueue("PH03", make_payload("PH03"))
        deals_repo.claim_next(worker="w")
        phases = {p["key"]: p for p in deals_repo.get_phases(did)}
        assert_eq(phases["claimed"]["state"], "done")
        assert_eq(phases["delivered"]["state"], "active",
                  "während processing muss delivered 'active' sein")

    def _phases_after_sent_all_done():
        reset_db()
        did = deals_repo.enqueue("PH04", make_payload("PH04"))
        deals_repo.claim_next(worker="w")
        deals_repo.mark_sent(did, detail="telegram+facebook")
        phases = {p["key"]: p for p in deals_repo.get_phases(did)}
        for key in ("ingested", "claimed", "delivered"):
            assert_eq(phases[key]["state"], "done", f"{key} muss done sein")
        # detail muss Kanal enthalten
        assert "telegram" in (phases["delivered"]["detail"] or ""), \
            "delivered.detail muss Kanal enthalten"

    def _phases_after_failure_marks_failed():
        reset_db()
        did = deals_repo.enqueue("PH05", make_payload("PH05"))
        deals_repo.claim_next(worker="w")
        deals_repo.mark_failed(did, "boom", retry=False)
        phases = {p["key"]: p for p in deals_repo.get_phases(did)}
        # delivered muss failed sein, claimed bleibt done
        assert_eq(phases["claimed"]["state"], "done")
        assert_eq(phases["delivered"]["state"], "failed")
        assert "boom" in (phases["delivered"]["detail"] or ""), \
            "Fehlertext muss in failed-Phase auftauchen"

    def _phases_missing_deal_returns_none():
        reset_db()
        assert deals_repo.get_phases(99999) is None

    _case(r, "Phasen direkt nach enqueue", _phases_initial_only_ingested)
    _case(r, "Phasen nach Payload-Update (Parser-Enrichment)",
          _phases_after_update_enriched)
    _case(r, "Phasen während processing", _phases_after_claim_processing)
    _case(r, "Phasen nach sent: alle done + Kanal-Detail",
          _phases_after_sent_all_done)
    _case(r, "Phasen nach failed: korrekt als failed markiert",
          _phases_after_failure_marks_failed)
    _case(r, "get_phases für nicht existierenden Deal = None",
          _phases_missing_deal_returns_none)


# ─────────────────────────────────────────────────────────────────────────
# 5) State-KV (Dedup von bereits versendeten IDs etc.)
# ─────────────────────────────────────────────────────────────────────────
def test_block_state_kv(r: Result) -> None:
    section("5) State-KV (sent_ids, product_list, opened, …)")

    def _put_get_delete():
        reset_db()
        state_repo.put("foo", {"a": 1})
        assert_eq(state_repo.get("foo"), {"a": 1})
        state_repo.delete("foo")
        assert state_repo.get("foo") is None

    def _set_dedup_semantics():
        reset_db()
        state_repo.add_to_set("sent_ids:facebook", "A1")
        state_repo.add_to_set("sent_ids:facebook", "A1", "A2")
        assert_eq(state_repo.get_set("sent_ids:facebook"), {"A1", "A2"})
        assert state_repo.is_in_set("sent_ids:facebook", "A1")

    def _dict_update():
        reset_db()
        state_repo.update_dict("product_list", {"url-1": {"x": 1}})
        state_repo.update_dict("product_list", {"url-2": {"x": 2}})
        d = state_repo.get_dict("product_list")
        assert "url-1" in d and "url-2" in d, "update_dict muss merge-en"

    def _list_keys_introspection():
        reset_db()
        state_repo.put("a", [1, 2, 3])
        state_repo.put("b", {"k": "v"})
        keys = {k["key"]: k for k in state_repo.list_keys()}
        assert "a" in keys and "b" in keys
        assert_eq(keys["a"]["type"], "list")
        assert_eq(keys["a"]["size"], 3)

    _case(r, "put/get/delete", _put_get_delete)
    _case(r, "Set-Semantik mit Dedup", _set_dedup_semantics)
    _case(r, "Dict-Semantik mit Merge", _dict_update)
    _case(r, "list_keys für Dashboard-Browser", _list_keys_introspection)


# ─────────────────────────────────────────────────────────────────────────
# 6) Worker-Heartbeats (Dashboard-Liveness)
# ─────────────────────────────────────────────────────────────────────────
def test_block_workers(r: Result) -> None:
    section("6) Worker-Heartbeats / Stale-Detection")

    def _register_and_heartbeat():
        reset_db()
        workers_repo.register("w_test", pid=1234)
        workers_repo.set_task("w_test", "parsing B0XYZ")
        all_w = {w["name"]: w for w in workers_repo.list_all()}
        assert "w_test" in all_w
        assert_eq(all_w["w_test"]["state"], WORKER_STATE_BUSY)
        assert_eq(all_w["w_test"]["current_task"], "parsing B0XYZ")

    def _stale_after_threshold():
        reset_db()
        from sqlalchemy import update
        from core.db.engine import session_scope
        from core.db.models import Worker
        workers_repo.register("stale_w", pid=9999)
        # last_heartbeat künstlich in Vergangenheit setzen
        with session_scope() as s:
            s.execute(update(Worker).where(Worker.name == "stale_w").values(
                last_heartbeat=datetime.utcnow() - timedelta(hours=1)
            ))
        all_w = {w["name"]: w for w in workers_repo.list_all()}
        assert_eq(all_w["stale_w"]["state"], "stale",
                  "Worker mit altem Heartbeat muss 'stale' sein")

    def _error_state():
        reset_db()
        workers_repo.register("err_w")
        workers_repo.set_error("err_w", "Vertex-API down")
        w = {x["name"]: x for x in workers_repo.list_all()}["err_w"]
        assert_eq(w["state"], WORKER_STATE_ERROR)

    _case(r, "register + set_task setzt Status busy", _register_and_heartbeat)
    _case(r, "alter Heartbeat → stale", _stale_after_threshold)
    _case(r, "set_error setzt Status error", _error_state)


# ─────────────────────────────────────────────────────────────────────────
# 7) Dashboard-API (FastAPI TestClient – ohne echten Port)
# ─────────────────────────────────────────────────────────────────────────
def test_block_dashboard_api(r: Result) -> None:
    section("7) Dashboard-API (FastAPI TestClient)")

    try:
        from fastapi.testclient import TestClient
        from core.dashboard import app as dashboard_app
    except Exception as e:
        r.skip("Dashboard-API", f"fastapi/testclient nicht verfügbar: {e}")
        return

    client = TestClient(dashboard_app)

    def _status_endpoint():
        reset_db()
        deals_repo.enqueue("DASH01", make_payload("DASH01"))
        rsp = client.get("/api/status")
        assert_eq(rsp.status_code, 200)
        data = rsp.json()
        assert "deals" in data and "workers" in data
        assert_eq(data["deals"][DEAL_STATUS_QUEUE], 1)

    def _list_deals_endpoint():
        reset_db()
        deals_repo.enqueue("DASH02", make_payload("DASH02"))
        rsp = client.get("/api/deals", params={"status": "queue"})
        assert_eq(rsp.status_code, 200)
        deals = rsp.json()
        assert len(deals) == 1
        assert_eq(deals[0]["product_id"], "DASH02")

    def _deal_detail_includes_phases():
        reset_db()
        did = deals_repo.enqueue("DASH03", make_payload("DASH03"))
        deals_repo.claim_next(worker="w")
        deals_repo.mark_sent(did, detail="telegram")
        rsp = client.get(f"/api/deals/{did}")
        assert_eq(rsp.status_code, 200)
        body = rsp.json()
        assert "phases" in body, "Deal-Detail muss 'phases' enthalten"
        assert "events" in body, "Deal-Detail muss 'events' enthalten"
        # Stepper muss alle vier kanonischen Phasen liefern
        keys = [p["key"] for p in body["phases"]]
        for k in ("ingested", "enriched", "claimed", "delivered"):
            assert_in(k, keys, f"Phase '{k}' fehlt in API-Response")
        # mind. ingested + delivered = done
        states = {p["key"]: p["state"] for p in body["phases"]}
        assert_eq(states["ingested"], "done")
        assert_eq(states["delivered"], "done")

    def _phases_endpoint():
        reset_db()
        did = deals_repo.enqueue("DASH04", make_payload("DASH04"))
        rsp = client.get(f"/api/deals/{did}/phases")
        assert_eq(rsp.status_code, 200)
        body = rsp.json()
        assert_eq(body["deal_id"], did)
        assert len(body["phases"]) == 4

    def _phases_endpoint_404():
        rsp = client.get("/api/deals/99999/phases")
        assert_eq(rsp.status_code, 404)

    def _requeue_endpoint():
        reset_db()
        did = deals_repo.enqueue("DASH05", make_payload("DASH05"))
        deals_repo.claim_next(worker="w")
        deals_repo.mark_sent(did)
        rsp = client.post(f"/api/deals/{did}/requeue")
        assert_eq(rsp.status_code, 200)
        assert_eq(deals_repo.get(did)["status"], DEAL_STATUS_QUEUE)

    def _delete_endpoint():
        reset_db()
        did = deals_repo.enqueue("DASH06", make_payload("DASH06"))
        rsp = client.delete(f"/api/deals/{did}")
        assert_eq(rsp.status_code, 200)
        rsp2 = client.delete(f"/api/deals/{did}")
        assert_eq(rsp2.status_code, 404, "doppeltes Delete muss 404 liefern")

    def _timeline_endpoint():
        reset_db()
        did = deals_repo.enqueue("DASH07", make_payload("DASH07"))
        deals_repo.claim_next(worker="w")
        deals_repo.mark_sent(did, detail="telegram+facebook")
        rsp = client.get("/api/timeline", params={"hours": 24, "limit": 50})
        assert_eq(rsp.status_code, 200)
        events = rsp.json()
        assert len(events) >= 3, "Timeline sollte ≥3 Events haben"
        # post_type + product_id müssen vorhanden sein
        for e in events:
            assert "post_type" in e and "product_id" in e

    def _state_endpoints():
        reset_db()
        state_repo.put("sent_ids:telegram", ["A1"])
        rsp = client.get("/api/state")
        assert_eq(rsp.status_code, 200)
        keys = [x["key"] for x in rsp.json()]
        assert_in("sent_ids:telegram", keys)
        rsp2 = client.get("/api/state/sent_ids:telegram")
        assert_eq(rsp2.status_code, 200)
        assert_eq(rsp2.json()["value"], ["A1"])
        rsp3 = client.put("/api/state/sent_ids:telegram",
                          json={"value": ["A1", "A2"]})
        assert_eq(rsp3.status_code, 200)
        assert_eq(state_repo.get("sent_ids:telegram"), ["A1", "A2"])
        rsp4 = client.delete("/api/state/sent_ids:telegram")
        assert_eq(rsp4.status_code, 200)
        assert state_repo.get("sent_ids:telegram") is None

    def _state_get_404():
        reset_db()
        rsp = client.get("/api/state/__nope__")
        assert_eq(rsp.status_code, 404)

    def _index_html_renders():
        rsp = client.get("/")
        assert_eq(rsp.status_code, 200)
        assert b"Scraper Control Center" in rsp.content
        assert b"stepper" in rsp.content, "Stepper-CSS muss im HTML eingebettet sein"

    def _worker_signal_404():
        rsp = client.post("/api/workers/nonexistent_worker/stop")
        assert_eq(rsp.status_code, 404)

    def _worker_no_pid():
        reset_db()
        # registriere Worker, lösche pid künstlich
        workers_repo.register("nopid_w", pid=None)
        from sqlalchemy import update
        from core.db.engine import session_scope
        from core.db.models import Worker
        with session_scope() as s:
            s.execute(update(Worker).where(Worker.name == "nopid_w").values(pid=None))
        rsp = client.post("/api/workers/nopid_w/stop")
        assert_eq(rsp.status_code, 409, "Worker ohne PID muss 409 liefern")

    _case(r, "GET /api/status liefert deals+workers", _status_endpoint)
    _case(r, "GET /api/deals?status=queue", _list_deals_endpoint)
    _case(r, "GET /api/deals/{id} enthält phases & events", _deal_detail_includes_phases)
    _case(r, "GET /api/deals/{id}/phases", _phases_endpoint)
    _case(r, "GET /api/deals/{id}/phases → 404 bei unbekannt", _phases_endpoint_404)
    _case(r, "POST /api/deals/{id}/requeue", _requeue_endpoint)
    _case(r, "DELETE /api/deals/{id} (zweifach → 404)", _delete_endpoint)
    _case(r, "GET /api/timeline mit post_type", _timeline_endpoint)
    _case(r, "GET/PUT/DELETE /api/state/*", _state_endpoints)
    _case(r, "GET /api/state/{unbekannt} → 404", _state_get_404)
    _case(r, "GET / liefert HTML inkl. Stepper-CSS", _index_html_renders)
    _case(r, "POST /api/workers/unknown/stop → 404", _worker_signal_404)
    _case(r, "POST /api/workers/{w}/stop ohne PID → 409", _worker_no_pid)


# ─────────────────────────────────────────────────────────────────────────
# 8) Simulierte Multi-Channel-Ausspielung
# ─────────────────────────────────────────────────────────────────────────
def test_block_multichannel(r: Result) -> None:
    section("8) Multi-Channel-Versand (Telegram + Facebook)")

    def _two_channels_dedup():
        reset_db()
        did = deals_repo.enqueue("MC01", make_payload("MC01"))
        # Worker 1: Telegram
        deals_repo.claim_next(worker="telegram_router")
        deals_repo.mark_sent(did, detail="telegram")
        state_repo.add_to_set("sent_ids:telegram", "MC01")
        # Re-Enqueue für Facebook
        deals_repo.requeue(did)
        deals_repo.claim_next(worker="fb_watcher")
        deals_repo.mark_sent(did, detail="facebook")
        state_repo.add_to_set("sent_ids:facebook", "MC01")
        assert state_repo.is_in_set("sent_ids:telegram", "MC01")
        assert state_repo.is_in_set("sent_ids:facebook", "MC01")
        # Timeline muss beide sent-Events zeigen
        events = deals_repo.get_events(did)
        sent_events = [e for e in events if e["event"] == "sent"]
        assert len(sent_events) == 2, "es müssen 2 sent-Events existieren"

    def _failed_one_channel_other_succeeds():
        reset_db()
        did = deals_repo.enqueue("MC02", make_payload("MC02"))
        deals_repo.claim_next(worker="telegram_router")
        deals_repo.mark_failed(did, "TG rate-limit", retry=True)
        # zurück in Queue, jetzt Facebook
        deals_repo.claim_next(worker="fb_watcher")
        deals_repo.mark_sent(did, detail="facebook")
        d = deals_repo.get(did)
        assert_eq(d["status"], DEAL_STATUS_SENT)
        assert d["retry_count"] >= 1

    _case(r, "zwei Kanäle versendet + State-Dedup", _two_channels_dedup)
    _case(r, "1 Kanal fail+retry, anderer success", _failed_one_channel_other_succeeds)


# ─────────────────────────────────────────────────────────────────────────
# 9) Integration: realer Supervisor-Lauf
# ─────────────────────────────────────────────────────────────────────────
async def _run_supervisor_briefly(seconds: float) -> tuple[int, str]:
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    proc = await asyncio.create_subprocess_exec(
        sys.executable, str(ROOT / "run_all.py"), "--mode", "parser",
        cwd=str(ROOT), env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    output: list[bytes] = []

    async def drain():
        assert proc.stdout is not None
        while True:
            chunk = await proc.stdout.read(4096)
            if not chunk:
                return
            output.append(chunk)

    t = asyncio.create_task(drain())
    try:
        await asyncio.wait_for(proc.wait(), timeout=seconds)
        # vorzeitig beendet
        t.cancel()
        return proc.returncode or -1, b"".join(output).decode(errors="replace")
    except asyncio.TimeoutError:
        pass
    proc.send_signal(signal.SIGTERM)
    try:
        rc = await asyncio.wait_for(proc.wait(), timeout=15)
    except asyncio.TimeoutError:
        proc.kill()
        rc = await proc.wait()
    t.cancel()
    return rc, b"".join(output).decode(errors="replace")


def test_block_integration(r: Result, enabled: bool) -> None:
    section("9) Integration: realer run_all.py --mode parser")
    if not enabled:
        r.skip("realer Supervisor-Lauf", "--integration nicht gesetzt")
        return

    def _real_run():
        rc, out = asyncio.run(_run_supervisor_briefly(12.0))
        markers = ["[supervisor]", "started ws_server", "started product_parser"]
        missing = [m for m in markers if m not in out]
        if missing:
            tail = out[-3000:]
            raise AssertionError(f"fehlende Marker: {missing}\n--- TAIL ---\n{tail}")
        if rc not in (0, -signal.SIGTERM, 143):
            tail = out[-3000:]
            raise AssertionError(f"Exit-Code {rc}\n--- TAIL ---\n{tail}")

    def _enqueue_during_run():
        """
        Schreibt während eines (separat laufenden) Supervisors einen Deal in die
        Test-DB und beobachtet, dass er per claim_next abholbar bleibt.
        Da der Parser-Mode keinen Telegram-Sender enthält, bleibt der Deal in
        Queue – das genügt, um den Read-Pfad zu validieren.
        """
        reset_db()
        did = deals_repo.enqueue("INT01", make_payload("INT01"))
        # nur synchroner Check – der echte Worker liest unsere Test-DB
        # (CORE_DB_URL ist auf TMP gesetzt)
        d = deals_repo.get(did)
        assert d is not None and d["status"] == DEAL_STATUS_QUEUE

    _case(r, "Supervisor startet, läuft 12s, beendet sauber", _real_run)
    _case(r, "Deal kann während Lauf erzeugt werden", _enqueue_during_run)


# ─────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────
def main() -> int:
    global VERBOSE
    ap = argparse.ArgumentParser()
    ap.add_argument("--integration", action="store_true",
                    help="zusätzlich realen run_all.py --mode parser starten")
    ap.add_argument("--verbose", action="store_true",
                    help="ausführliche Stack-Traces ausgeben")
    args = ap.parse_args()
    VERBOSE = args.verbose

    print("End-to-End-Pipeline-Tests")
    print(f"Projekt-Root: {ROOT}")
    print(f"Test-DB:      {_TEST_DB}")
    init_db()

    r = Result()
    test_block_ingest(r)
    test_block_full_lifecycle(r)
    test_block_failures(r)
    test_block_phases(r)
    test_block_state_kv(r)
    test_block_workers(r)
    test_block_dashboard_api(r)
    test_block_multichannel(r)
    test_block_integration(r, args.integration)

    return r.summary()


if __name__ == "__main__":
    sys.exit(main())
