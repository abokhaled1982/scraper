"""
core.dashboard — FastAPI-Web-Dashboard (Control Center).

Tabs:
    - Overview : Counts + Worker-Liste mit Status/Heartbeat
    - Deals    : Queue/Processing/Sent/Failed inkl. Requeue + Delete
    - Workers  : Detailansicht + Stop/Kill via PID-Signal
    - Logs     : Letzte N Zeilen pro Worker aus .log/<DATE>/<worker>.log
    - State    : StateKV-Browser/Editor (sent_ids, product_list, ...)
    - Produkte : product_list-Übersicht (Observer-Links + Opener-Status)

Start als Modul:
    python -m core.dashboard
"""
from __future__ import annotations
import os
import signal
from datetime import date
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Body, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
import uvicorn

from core.config import DASHBOARD_HOST, DASHBOARD_PORT, LOG_DIR
from core.db import workers_repo, deals_repo, state_repo, config_repo, init_db
from core.db.engine import reset_db as _reset_db, backup_db as _backup_db

app = FastAPI(title="Scraper Dashboard")


@app.on_event("startup")
def _on_startup() -> None:
    init_db()


# ──────────────────────────────────────────────────────────────
# Status / Overview
# ──────────────────────────────────────────────────────────────

@app.get("/api/status")
def api_status() -> dict[str, Any]:
    """Overview-Datenquelle: Worker, Deal-Counts, Top-Queue und Opener-Pipeline.

    - workers      : alle registrierten Worker (Heartbeat, current_task, stats)
    - deals        : counts_by_status (queue/processing/sent/failed)
    - next_queue   : die nächsten zu versendenden Deals (Top-N nach
                     Priorität+FIFO), inkl. Titel/Markt/Preis – fürs
                     „Was kommt als nächstes?"-Panel im Overview-Tab.
    - opener       : Snapshot der Produkt-Pipeline:
                       * in_list  = Einträge in state_kv['product_list']
                       * opened   = Einträge in state_kv['opened']
                       * pending  = product_list-Schlüssel, die noch nicht
                                    in opened auftauchen (echte „zu öffnen"-
                                    Restmenge)
    """
    # Top-N der Queue (Priorität DESC, created_at ASC – identisch zur
    # list_queue-Sortierung, die der telRouter sowieso nutzt)
    try:
        top_queue = deals_repo.list_queue(limit=5)
    except Exception:
        top_queue = []
    next_queue = [
        {
            "id": d.get("id"),
            "market": d.get("market"),
            "title": d.get("title"),
            "priority": d.get("priority"),
            "created_at": d.get("created_at"),
            "price": ((d.get("payload") or {}).get("price")
                      or (d.get("payload") or {}).get("price_now")),
        }
        for d in top_queue
    ]

    # Opener-Pipeline (DB-state_kv)
    product_list = state_repo.get_dict("product_list") or {}
    opened = state_repo.get_dict("opened") or {}
    pending = [k for k in product_list.keys() if k not in opened]

    return {
        "workers": workers_repo.list_all(),
        "deals": deals_repo.counts_by_status(),
        "next_queue": next_queue,
        "opener": {
            "in_list": len(product_list),
            "opened": len(opened),
            "pending": len(pending),
        },
    }


# ──────────────────────────────────────────────────────────────
# Deals
# WICHTIG: Statische Pfade (search/markets/bulk/cleanup/submit_url) MÜSSEN
# vor den dynamischen ({deal_id}) Routen registriert werden – sonst frisst
# die int-Validierung von {deal_id} die Strings.
# ──────────────────────────────────────────────────────────────

@app.get("/api/deals")
def api_deals(status: str = Query("queue"), limit: int = 200) -> list[dict]:
    return deals_repo.list_by_status(status, limit=limit)


@app.get("/api/deals/search")
def api_deals_search(
    status: str | None = Query(None),
    market: str | None = Query(None),
    q: str | None = Query(None),
    only_no_image: bool = Query(False),
    only_no_price: bool = Query(False),
    min_price: float | None = Query(None),
    max_price: float | None = Query(None),
    limit: int = Query(200),
) -> list[dict]:
    return deals_repo.list_filtered(
        status=status, market=market, q=q,
        only_no_image=only_no_image, only_no_price=only_no_price,
        min_price=min_price, max_price=max_price, limit=limit,
    )


@app.get("/api/deals/markets")
def api_deals_markets() -> list[str]:
    return deals_repo.markets()


@app.post("/api/deals/bulk")
def api_deals_bulk(body: dict = Body(...)) -> dict:
    """Bulk-Aktionen.

    body = {"ids": [1,2,3], "action": "requeue"|"delete"|"mark_sent"|"set_priority", "priority": 100}
    """
    ids = body.get("ids") or []
    action = (body.get("action") or "").lower()
    if not ids or not isinstance(ids, list):
        raise HTTPException(400, "ids required")
    if action == "requeue":
        n = deals_repo.bulk_requeue(ids, priority=body.get("priority"))
    elif action == "delete":
        n = deals_repo.bulk_delete(ids)
    elif action == "mark_sent":
        n = deals_repo.bulk_set_status(ids, "sent")
    elif action == "mark_failed":
        n = deals_repo.bulk_set_status(ids, "failed")
    elif action == "set_priority":
        prio = int(body.get("priority", 0))
        n = sum(1 for i in ids if deals_repo.set_priority(int(i), prio))
    else:
        raise HTTPException(400, f"unknown action: {action}")
    return {"ok": True, "affected": n, "action": action}


@app.post("/api/deals/cleanup")
def api_deals_cleanup(body: dict = Body(...)) -> dict:
    """Löscht Deals eines Status, deren updated_at älter als N Tage ist."""
    status = body.get("status") or "failed"
    days = int(body.get("days", 30))
    dry = bool(body.get("dry_run", False))
    return deals_repo.cleanup(status=status, older_than_days=days, dry_run=dry)


@app.post("/api/deals/submit_url")
def api_deals_submit_url(body: dict = Body(...)) -> dict:
    """Manueller URL-Wurf in product_list (wie wenn der Observer einen Link
    sehen würde). product_opener pickt sie auf.

    product_list lebt als Dict[key, meta] in state_kv (siehe
    telObserver_piraten.add_link_to_product_list). Wir nutzen exakt
    dieselbe Key-Schema (A-<ASIN> / U-<hash>), damit der Opener-Dedup
    funktioniert.
    """
    url = (body.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "invalid url")
    try:
        import time as _time, re as _re, hashlib as _hashlib
        store = state_repo.get_dict("product_list")
        # Migration: falls historisch List → in Dict konvertieren
        if not isinstance(store, dict):
            old = store if isinstance(store, list) else []
            store = {}
            for u in old:
                if isinstance(u, str) and u.startswith(("http://", "https://")):
                    store[f"U-{_hashlib.sha1(u.encode()).hexdigest()[:10]}"] = {
                        "product_url": u, "source": "migrated",
                    }

        m = _re.search(r'/(?:dp|gp/product|d|o)/([A-Z0-9]{10})(?:[\/?]|$)', url, _re.IGNORECASE)
        key = f"A-{m.group(1).upper()}" if m else f"U-{_hashlib.sha1(url.encode()).hexdigest()[:10]}"

        if key in store:
            return {"ok": True, "added": False, "size": len(store), "key": key}

        store[key] = {
            "product_url": url,
            "source": "dashboard_manual",
            "timestamp": _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime()),
        }
        state_repo.put("product_list", store)
        return {"ok": True, "added": True, "size": len(store), "key": key}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/deals/{deal_id}")
def api_deal_detail(deal_id: int) -> dict:
    d = deals_repo.get(deal_id)
    if not d:
        raise HTTPException(404, "deal not found")
    d["events"] = deals_repo.get_events(deal_id, limit=100)
    d["phases"] = deals_repo.get_phases(deal_id) or []
    return d


@app.get("/api/deals/{deal_id}/phases")
def api_deal_phases(deal_id: int) -> dict:
    phases = deals_repo.get_phases(deal_id)
    if phases is None:
        raise HTTPException(404, "deal not found")
    return {"deal_id": deal_id, "phases": phases}


@app.get("/api/deals/{deal_id}/logs", response_class=PlainTextResponse)
def api_deal_logs(deal_id: int, lines: int = 300, days: int = 2) -> str:
    """Sammelt aus allen Worker-Logfiles der letzten N Tage die Zeilen,
    die die product_id (oder die deal-id) des Deals enthalten."""
    d = deals_repo.get(deal_id)
    if not d:
        raise HTTPException(404, "deal not found")
    needles: list[str] = []
    if d.get("product_id"):
        needles.append(str(d["product_id"]))
    payload = d.get("payload") or {}
    if isinstance(payload, dict) and payload.get("asin"):
        needles.append(str(payload["asin"]))
    needles.append(f"#{deal_id}")
    needles_lc = [n.lower() for n in needles if n]
    root = Path(LOG_DIR)
    if not root.exists():
        return f"# kein Log-Verzeichnis: {root}"
    day_dirs = sorted(
        (p for p in root.iterdir() if p.is_dir()),
        key=lambda p: p.name, reverse=True,
    )[: max(1, days)]
    hits: list[str] = []
    for day in day_dirs:
        for f in sorted(day.glob("*.log")):
            worker = f.stem
            try:
                with f.open("r", encoding="utf-8", errors="replace") as fh:
                    for ln in fh:
                        low = ln.lower()
                        if any(n in low for n in needles_lc):
                            hits.append(f"[{day.name} {worker}] {ln.rstrip()}")
            except OSError:
                continue
    if not hits:
        return f"# keine Logzeilen mit {needles!r} in {days} Tagen gefunden"
    return "\n".join(hits[-max(1, lines):])


@app.post("/api/deals/{deal_id}/requeue")
def api_deal_requeue(deal_id: int) -> dict:
    ok = deals_repo.requeue(deal_id)
    if not ok:
        raise HTTPException(404, "deal not found")
    return {"ok": True}


@app.patch("/api/deals/{deal_id}")
def api_deal_patch(deal_id: int, patch: dict = Body(...)) -> dict:
    ok = deals_repo.update_fields(deal_id, patch or {})
    if not ok:
        raise HTTPException(404, "deal not found or no editable fields in patch")
    return {"ok": True}


@app.post("/api/deals/{deal_id}/priority")
def api_deal_priority(deal_id: int, body: dict = Body(...)) -> dict:
    prio = int(body.get("priority", 0))
    if not deals_repo.set_priority(deal_id, prio):
        raise HTTPException(404, "deal not found")
    return {"ok": True, "priority": prio}


@app.post("/api/deals/{deal_id}/resend")
def api_deal_resend(deal_id: int, body: dict = Body(default={})) -> dict:
    """Manuelles erneutes Posten:
        - setzt Deal zurück in queue mit hoher Priorität (1000)
        - 'instant=True' konsumiert zusätzlich ein skip-wait-Token, damit
          fb_watcher die Pause überspringt
    """
    d = deals_repo.get(deal_id)
    if not d:
        raise HTTPException(404, "deal not found")
    pid = d.get("product_id")
    # 1) Entfernt aus sent_ids:facebook (fb_watcher In-Memory + DB)
    try:
        sent = state_repo.get_set("sent_ids:facebook")
        if pid in sent:
            sent.discard(pid)
            state_repo.put("sent_ids:facebook", sorted(sent))
    except Exception:
        pass
    # 2) Entfernt aus sent_asins-Register (telRouter Duplikat-Schutz).
    #    Ohne diesen Schritt würde telRouter den Deal sofort wieder
    #    als duplicate-skipped abräumen → "Resend macht nichts".
    try:
        reg = state_repo.get_dict("sent_asins") or {"asin": [], "filehash": []}
        changed = False
        if pid and pid in reg.get("asin", []):
            reg["asin"] = [a for a in reg["asin"] if a != pid]
            changed = True
        # Auch ASIN aus payload (Amazon-Schema)
        payload = d.get("payload") or {}
        asin = (payload.get("asin") if isinstance(payload, dict) else None)
        if asin and asin in reg.get("asin", []):
            reg["asin"] = [a for a in reg["asin"] if a != asin]
            changed = True
        if changed:
            state_repo.put("sent_asins", reg)
    except Exception:
        pass
    deals_repo.requeue(deal_id)
    deals_repo.set_priority(deal_id, 1000)
    instant = bool(body.get("instant", True))
    if instant:
        # FB-Timer beim nächsten Schritt überspringen
        cur = int(config_repo.get("facebook.skip_wait_count", 0) or 0)
        config_repo.set("facebook.skip_wait_count", cur + 1,
                        description="Anzahl ausstehender Pause-Überspringer")
    return {"ok": True, "instant": instant, "priority": 1000}


@app.delete("/api/deals/{deal_id}")
def api_deal_delete(deal_id: int) -> dict:
    ok = deals_repo.delete(deal_id)
    if not ok:
        raise HTTPException(404, "deal not found")
    return {"ok": True}


@app.get("/api/timeline")
def api_timeline(limit: int = 300, hours: int = 72) -> list[dict]:
    """Chronologischer Verlauf aller Pipeline-Events (link-erfasst → ai → sent / failed)."""
    return deals_repo.list_recent_events(limit=limit, hours=hours)


# ──────────────────────────────────────────────────────────────
# Runtime-Config (Settings-Tab)
# ──────────────────────────────────────────────────────────────

@app.get("/api/config")
def api_config_list() -> list[dict]:
    return config_repo.list_all()


@app.put("/api/config/{key:path}")
def api_config_set(key: str, body: dict = Body(...)) -> dict:
    if "value" not in body:
        raise HTTPException(400, "body must contain 'value'")
    config_repo.set(key, body["value"], description=body.get("description"))
    return {"ok": True, "key": key, "value": body["value"]}


@app.delete("/api/config/{key:path}")
def api_config_delete(key: str) -> dict:
    config_repo.delete(key)
    return {"ok": True, "key": key}


# ───────────────────────────────────────────────────────────────
# Admin: DB Backup + Reset
# ───────────────────────────────────────────────────────────────

@app.post("/api/admin/db/backup")
def api_admin_db_backup() -> dict:
    try:
        path = _backup_db()
        return {"ok": True, "path": path}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/admin/db/reset")
def api_admin_db_reset(body: dict = Body(default={})) -> dict:
    """Setzt die DB zurück. Erwartet body={"confirm": "RESET"}.
    Erzeugt vorher automatisch ein Backup in db/backups/."""
    if (body or {}).get("confirm") != "RESET":
        raise HTTPException(400, "confirm must be 'RESET'")
    make_backup = bool((body or {}).get("backup", True))
    try:
        return _reset_db(make_backup=make_backup)
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/admin/sent_asins")
def api_admin_sent_asins_get() -> dict:
    """Liefert das Duplikat-Register des telRouter (verhindert Doppel-Posts)."""
    reg = state_repo.get_dict("sent_asins") or {}
    asins = reg.get("asin") or []
    hashes = reg.get("filehash") or []
    return {
        "asin_count": len(asins),
        "filehash_count": len(hashes),
        "asins": asins[-50:],   # letzte 50
    }


@app.post("/api/admin/sent_asins/reset")
def api_admin_sent_asins_reset(body: dict = Body(default={})) -> dict:
    """Leert das Duplikat-Register. Optional body={"asins": ["B0.."]} entfernt
    nur einzelne Einträge statt alles."""
    reg = state_repo.get_dict("sent_asins") or {"asin": [], "filehash": []}
    only = (body or {}).get("asins")
    if isinstance(only, list) and only:
        removed = [a for a in reg.get("asin", []) if a in only]
        reg["asin"] = [a for a in reg.get("asin", []) if a not in only]
        state_repo.put("sent_asins", reg)
        return {"ok": True, "removed": removed, "remaining": len(reg["asin"])}
    state_repo.put("sent_asins", {"asin": [], "filehash": []})
    return {"ok": True, "cleared": True}


# ──────────────────────────────────────────────────────────────
# Workers (Signal-Control)
# ──────────────────────────────────────────────────────────────

def _signal_worker(name: str, sig: int) -> dict:
    workers = {w["name"]: w for w in workers_repo.list_all()}
    w = workers.get(name)
    if not w:
        raise HTTPException(404, f"worker {name} not registered")
    pid = w.get("pid")
    if not pid:
        raise HTTPException(409, "worker has no PID recorded")
    try:
        os.kill(int(pid), sig)
    except ProcessLookupError:
        workers_repo.set_stopped(name)
        raise HTTPException(410, "process already gone")
    except PermissionError:
        raise HTTPException(403, "no permission to signal process")
    return {"ok": True, "pid": pid, "signal": sig}


@app.post("/api/workers/{name}/stop")
def api_worker_stop(name: str) -> dict:
    return _signal_worker(name, signal.SIGTERM)


@app.post("/api/workers/{name}/kill")
def api_worker_kill(name: str) -> dict:
    return _signal_worker(name, signal.SIGKILL)


@app.post("/api/workers/{name}/pause")
def api_worker_pause(name: str) -> dict:
    config_repo.set(f"worker.{name}.paused", True,
                    description=f"Pause-Flag für {name} (dashboard)")
    return {"ok": True, "paused": True}


@app.post("/api/workers/{name}/resume")
def api_worker_resume(name: str) -> dict:
    config_repo.set(f"worker.{name}.paused", False,
                    description=f"Pause-Flag für {name} (dashboard)")
    return {"ok": True, "paused": False}


# ──────────────────────────────────────────────────────────────
# WebSocket Live-Logs (Phase 3)
# ──────────────────────────────────────────────────────────────

@app.websocket("/ws/logs/{worker}")
async def ws_logs(websocket: WebSocket, worker: str):
    """Streamt die Log-Datei eines Workers Live (tail -f Stil)."""
    import asyncio
    await websocket.accept()
    safe = "".join(c for c in worker if c.isalnum() or c in ("_", "-"))
    path = Path(LOG_DIR) / date.today().isoformat() / f"{safe}.log"
    try:
        # warte ggf. bis Datei existiert
        for _ in range(20):
            if path.exists():
                break
            await asyncio.sleep(0.5)
        if not path.exists():
            await websocket.send_text(f"# kein Log unter {path}")
            await websocket.close()
            return
        # initial: letzte 100 Zeilen
        try:
            with path.open("r", encoding="utf-8", errors="replace") as f:
                tail = f.readlines()[-100:]
            await websocket.send_text("".join(tail))
        except Exception:
            pass
        # tail
        with path.open("r", encoding="utf-8", errors="replace") as f:
            f.seek(0, 2)  # ans Ende
            while True:
                line = f.readline()
                if not line:
                    await asyncio.sleep(0.4)
                    continue
                await websocket.send_text(line)
    except WebSocketDisconnect:
        return
    except Exception as e:
        try:
            await websocket.send_text(f"# stream error: {e}")
            await websocket.close()
        except Exception:
            pass


# ──────────────────────────────────────────────────────────────
# Logs
# ──────────────────────────────────────────────────────────────

def _log_path(worker: str, day: str | None = None) -> Path:
    day = day or date.today().isoformat()
    safe = "".join(c for c in worker if c.isalnum() or c in ("_", "-"))
    return Path(LOG_DIR) / day / f"{safe}.log"


@app.get("/api/logs/{worker}", response_class=PlainTextResponse)
def api_logs(worker: str, lines: int = 200, day: str | None = None,
             grep: str | None = None) -> str:
    p = _log_path(worker, day)
    if not p.exists():
        return f"# kein Log unter {p}"
    try:
        with p.open("r", encoding="utf-8", errors="replace") as f:
            data = f.readlines()
    except OSError as e:
        raise HTTPException(500, f"log read error: {e}")
    if grep:
        data = [ln for ln in data if grep.lower() in ln.lower()]
    return "".join(data[-max(1, lines):])


@app.get("/api/logs")
def api_logs_index() -> dict[str, list[str]]:
    """Liefert {day: [worker, ...]} für die letzten 7 Tage."""
    out: dict[str, list[str]] = {}
    root = Path(LOG_DIR)
    if not root.exists():
        return out
    days = sorted(
        (p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name, reverse=True
    )[:7]
    for d in days:
        out[d.name] = sorted(p.stem for p in d.glob("*.log"))
    return out


# ──────────────────────────────────────────────────────────────
# State (KV)
# ──────────────────────────────────────────────────────────────

@app.get("/api/state")
def api_state_list() -> list[dict]:
    return state_repo.list_keys()


@app.get("/api/state/{key}")
def api_state_get(key: str) -> dict:
    val = state_repo.get(key, None)
    if val is None:
        raise HTTPException(404, "key not found")
    return {"key": key, "value": val}


@app.put("/api/state/{key}")
def api_state_put(key: str, value: Any = Body(..., embed=True)) -> dict:
    state_repo.put(key, value)
    return {"ok": True, "key": key}


@app.delete("/api/state/{key}")
def api_state_delete(key: str) -> dict:
    state_repo.delete(key)
    return {"ok": True}


# ──────────────────────────────────────────────────────────────
# Products (product_list + opened-Status)
# Eigener Tab im Dashboard. Zeigt alle vom Observer/Parser
# eingesammelten Amazon-URLs, ihren Status (noch offen / schon
# vom product_opener geöffnet) und erlaubt Einzel-Löschung
# bzw. Komplett-Clear.
# ──────────────────────────────────────────────────────────────

def _products_snapshot() -> list[dict]:
    """Flacht product_list (Dict[key, meta]) + opened (Dict[asin, …]) in
    eine Liste für die UI ab. Sortiert: neueste/zuletzt gesehene zuerst."""
    products = state_repo.get_dict("product_list")
    opened = state_repo.get_dict("opened")
    rows: list[dict] = []
    for key, meta in products.items():
        if not isinstance(meta, dict):
            meta = {"product_url": str(meta)}
        # ASIN aus Key (A-XXXX) oder aus meta.asin
        asin = meta.get("asin") or (key[2:] if key.startswith("A-") else None)
        op = opened.get(asin) if asin else None
        # opened-Repo nutzt manchmal direkt den key (z.B. ASIN) als opened-Key
        if op is None and key in opened:
            op = opened[key]
        rows.append({
            "key": key,
            "asin": asin,
            "product_url": meta.get("product_url"),
            "product_name": meta.get("product_name"),
            "price": (meta.get("price") or {}).get("value") if isinstance(meta.get("price"), dict) else meta.get("price"),
            "discount_percent": meta.get("discount_percent"),
            "source": meta.get("source") or meta.get("_source_file") or "—",
            "added_at": meta.get("timestamp") or meta.get("_first_seen"),
            "last_seen": meta.get("_last_seen"),
            "opened_at": (op or {}).get("last_open"),
            "canonical_url": (op or {}).get("canonical_url"),
        })
    # Sort: noch offene (kein opened_at) zuerst, dann nach added_at desc
    rows.sort(key=lambda r: (
        r["opened_at"] is not None,
        -(float(r["opened_at"]) if isinstance(r["opened_at"], (int, float)) else 0.0),
        r["added_at"] or "",
    ), reverse=False)
    return rows


@app.get("/api/products")
def api_products() -> dict:
    rows = _products_snapshot()
    pending = sum(1 for r in rows if not r["opened_at"])
    return {
        "rows": rows,
        "total": len(rows),
        "pending": pending,
        "opened": len(rows) - pending,
    }


@app.delete("/api/products/{key}")
def api_products_delete(key: str) -> dict:
    products = state_repo.get_dict("product_list")
    if key not in products:
        raise HTTPException(404, f"key not found: {key}")
    del products[key]
    state_repo.put("product_list", products)
    return {"ok": True, "removed": key, "size": len(products)}


@app.post("/api/products/clear")
def api_products_clear(body: dict = Body(default={})) -> dict:
    """Leert die komplette product_list. Optional auch opened-Cache
    zurücksetzen (body={'reset_opened': true})."""
    n = len(state_repo.get_dict("product_list"))
    state_repo.put("product_list", {})
    if bool(body.get("reset_opened")):
        state_repo.put("opened", {})
    return {"ok": True, "cleared": n}


# ──────────────────────────────────────────────────────────────
# UI
# ──────────────────────────────────────────────────────────────

_INDEX_HTML = r"""<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<title>Scraper Control Center</title>
<style>
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body { font-family: -apple-system, "Segoe UI", Roboto, sans-serif; margin: 0;
       background:#0f172a; color:#e2e8f0; }
header { padding: 14px 20px; background:#0b1220; border-bottom:1px solid #1e293b;
         display:flex; gap:18px; align-items:center; }
header h1 { margin:0; font-size:18px; color:#7dd3fc; }
nav { display:flex; gap:6px; margin-left:auto; }
nav button { background:#1e293b; color:#cbd5e1; border:1px solid #334155;
             padding:6px 14px; border-radius:6px; cursor:pointer; font-size:13px; }
nav button.active { background:#0369a1; border-color:#0284c7; color:#fff; }
main { padding: 18px 20px; }
.grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 22px; }
.card { background:#1e293b; border-radius: 10px; padding: 14px; }
.card h2 { margin:0; font-size:12px; color:#94a3b8; text-transform: uppercase; letter-spacing:.5px;}
.card .v { font-size: 30px; margin-top: 6px; font-weight: 600; }
.queue   { color:#facc15; }
.processing { color:#7dd3fc; }
.sent    { color:#4ade80; }
.failed  { color:#f87171; }
table { width:100%; border-collapse: collapse; background:#1e293b; border-radius:10px;
        overflow:hidden; margin-bottom:18px; }
th, td { padding: 9px 12px; text-align: left; font-size: 12.5px; border-bottom: 1px solid #334155;
         vertical-align: top; }
th { background:#0b1220; color:#94a3b8; text-transform: uppercase; font-size: 10.5px;
     letter-spacing:.5px; }
.pill { display:inline-block; padding: 2px 8px; border-radius: 999px; font-size: 10.5px;
        font-weight: 600; }
.pill.idle  { background:#0b1220; color:#94a3b8; }
.pill.busy  { background:#082f49; color:#7dd3fc; }
.pill.error { background:#450a0a; color:#fca5a5; }
.pill.stale { background:#451a03; color:#fdba74; }
.pill.stopped { background:#1f2937; color:#9ca3af; }
small { color:#64748b; }
button.act { background:#334155; color:#e2e8f0; border:none; padding:4px 10px;
             border-radius:5px; cursor:pointer; font-size:11.5px; margin-right:4px; }
button.act:hover { background:#475569; }
button.danger { background:#7f1d1d; }
button.danger:hover { background:#991b1b; }
.toolbar { display:flex; gap:8px; align-items:center; margin-bottom:12px; }
.toolbar input, .toolbar select {
    background:#0b1220; color:#e2e8f0; border:1px solid #334155;
    padding:6px 10px; border-radius:6px; font-size:13px; }
pre.log { background:#020617; padding: 14px; border-radius:8px; overflow:auto;
          max-height: 70vh; font-size:12px; line-height:1.45; white-space: pre-wrap;
          color:#cbd5e1; }
pre.json { background:#020617; padding:12px; border-radius:8px; overflow:auto;
           font-size:12px; color:#cbd5e1; max-height:60vh; }
.row-actions { white-space: nowrap; }
.tag { font-family: monospace; font-size:11px; color:#94a3b8; }

/* ── Modal ── */
.modal-backdrop { position:fixed; inset:0; background:rgba(0,0,0,.65); z-index:50;
    display:flex; align-items:flex-start; justify-content:center; padding:40px 20px;
    overflow-y:auto; }
.modal { background:#0b1220; border:1px solid #334155; border-radius:12px;
    width:min(960px, 100%); padding:22px 26px; box-shadow:0 20px 60px rgba(0,0,0,.5); }
.modal h2 { margin:0 0 4px; font-size:18px; color:#7dd3fc; }
.modal h3 { margin:18px 0 6px; font-size:12px; color:#94a3b8;
    text-transform:uppercase; letter-spacing:.5px; border-bottom:1px solid #1e293b; padding-bottom:4px; }
.modal .close { float:right; background:transparent; border:none; color:#94a3b8;
    font-size:22px; cursor:pointer; line-height:1; }
.modal .close:hover { color:#f87171; }
.kv { display:grid; grid-template-columns:180px 1fr; gap:6px 14px; font-size:12.5px;
    background:#020617; border-radius:8px; padding:12px 14px; }
.kv .k { color:#94a3b8; font-weight:600; }
.kv .v { color:#e2e8f0; word-break:break-word; }
.kv .v a { color:#7dd3fc; }
.kv .v img { max-width:140px; max-height:140px; border-radius:6px;
    border:1px solid #334155; display:block; margin-top:4px; }
.kv .v .imgs { display:flex; gap:6px; flex-wrap:wrap; }
.badge { display:inline-block; background:#082f49; color:#7dd3fc;
    padding:2px 8px; border-radius:6px; font-size:11px; margin-right:4px; }

/* ── Pipeline-Stepper ── */
.stepper { display:flex; gap:0; margin:14px 0 6px; background:#020617;
    padding:14px 12px; border-radius:8px; overflow-x:auto; }
.step { flex:1; min-width:140px; position:relative; padding:0 8px; text-align:center; }
.step + .step::before { content:""; position:absolute; left:-50%; top:18px;
    width:100%; height:2px; background:#334155; z-index:0; }
.step.done + .step::before, .step.active + .step::before { background:#0ea5e9; }
.step.failed + .step::before { background:#7f1d1d; }
.step .dot { width:36px; height:36px; border-radius:50%; margin:0 auto;
    background:#1e293b; border:2px solid #334155; display:flex;
    align-items:center; justify-content:center; font-size:18px;
    position:relative; z-index:1; }
.step.done    .dot { background:#052e16; border-color:#22c55e; color:#86efac; }
.step.active  .dot { background:#082f49; border-color:#0ea5e9; color:#7dd3fc;
    animation: pulse 1.6s ease-in-out infinite; }
.step.failed  .dot { background:#450a0a; border-color:#dc2626; color:#fca5a5; }
.step.pending .dot { color:#475569; }
.step.skipped .dot { background:#1e293b; border-color:#475569; color:#64748b;
    opacity:.6; }
.step .lbl { font-size:11.5px; margin-top:6px; color:#cbd5e1; font-weight:600; }
.step .ts  { font-size:10px; color:#64748b; margin-top:2px; line-height:1.2;
    word-break:break-word; }
.step.pending .lbl { color:#64748b; font-weight:400; }
@keyframes pulse { 0%,100% { box-shadow:0 0 0 0 rgba(14,165,233,.45); }
                   50%      { box-shadow:0 0 0 8px rgba(14,165,233,0); } }

/* Phase-Pill für Deals-Tab */
.phase-pill { display:inline-block; padding:1px 7px; border-radius:999px;
    font-size:10.5px; font-weight:600; background:#082f49; color:#7dd3fc; }
.phase-pill.failed { background:#450a0a; color:#fca5a5; }
.phase-pill.active { background:#451a03; color:#fdba74; }
.phase-pill.done   { background:#052e16; color:#86efac; }

/* Live-Status-Pill (inkl. Pulse) für Queue/Processing */
.live-pill { display:inline-flex; align-items:center; gap:6px; padding:2px 10px;
    border-radius:999px; font-size:11px; font-weight:600; }
.live-pill .led { width:8px; height:8px; border-radius:50%; display:inline-block; }
.live-pill.queue       { background:#082f49; color:#7dd3fc; }
.live-pill.queue .led  { background:#0ea5e9; animation: ledpulse 1.4s ease-in-out infinite; }
.live-pill.processing  { background:#451a03; color:#fdba74; }
.live-pill.processing .led { background:#f97316; animation: ledpulse 0.9s ease-in-out infinite; }
.live-pill.sent        { background:#052e16; color:#86efac; }
.live-pill.sent .led   { background:#22c55e; }
.live-pill.failed      { background:#450a0a; color:#fca5a5; }
.live-pill.failed .led { background:#dc2626; }
@keyframes ledpulse { 0%,100% { transform:scale(1); opacity:1; }
                      50%      { transform:scale(1.6); opacity:.5; } }

/* Queue-Live-Banner */
.queue-banner { display:flex; align-items:center; gap:14px; padding:10px 14px;
    background:linear-gradient(90deg,#082f49,#0b1220); border:1px solid #0369a1;
    border-radius:8px; margin-bottom:10px; font-size:12.5px; }
.queue-banner .tick { font-variant-numeric:tabular-nums; color:#fdba74;
    font-weight:700; min-width:32px; text-align:right; }
.queue-banner .bar  { flex:1; height:6px; background:#1e293b; border-radius:3px;
    overflow:hidden; }
.queue-banner .bar > div { height:100%; background:linear-gradient(90deg,#0ea5e9,#22c55e);
    transition: width .9s linear; }

/* ── Lifecycle-Cards im Deal-Detail ── */
.lc-grid { display:grid; grid-template-columns:repeat(auto-fit, minmax(220px, 1fr));
    gap:10px; margin:10px 0 14px; }
.lc-card { display:flex; gap:10px; padding:10px 12px; background:#020617;
    border:1px solid #1e293b; border-radius:10px; }
.lc-card .lc-icon { font-size:22px; line-height:1; }
.lc-card .lc-title { font-size:10.5px; color:#94a3b8; text-transform:uppercase;
    letter-spacing:.5px; font-weight:600; }
.lc-card .lc-value { font-size:13px; color:#e2e8f0; margin-top:2px; word-break:break-word; }
.lc-card .lc-value code { font-size:11px; background:#0b1220; padding:1px 6px;
    border-radius:4px; color:#7dd3fc; }
.lc-card .lc-sub { font-size:11px; color:#64748b; margin-top:3px;
    word-break:break-word; }
.lc-card .lc-sub a { color:#7dd3fc; }
.ch-pill { display:inline-block; padding:2px 9px; border-radius:999px;
    font-size:11px; font-weight:600; margin:2px 4px 2px 0; }
.ch-pill.ch-done    { background:#052e16; color:#86efac; border:1px solid #166534; }
.ch-pill.ch-pending { background:#1e293b; color:#64748b; border:1px dashed #334155; }
</style>
</head>
<body>
<header>
  <h1>📊 Scraper Control Center</h1>
  <span class="tag" id="refreshTag">– offline –</span>
  <nav>
    <button data-tab="overview" class="active">Overview</button>
    <button data-tab="deals">Deals</button>
    <button data-tab="timeline">Verlauf</button>
    <button data-tab="products">🛒 Produkte</button>
    <button data-tab="logs">Logs</button>
    <button data-tab="state">State</button>
    <button data-tab="settings">⚙ Settings</button>
  </nav>
</header>
<main>
  <section id="tab-overview"></section>
  <section id="tab-deals" hidden></section>
  <section id="tab-timeline" hidden></section>
  <section id="tab-products" hidden></section>
  <section id="tab-logs" hidden></section>
  <section id="tab-state" hidden></section>
  <section id="tab-settings" hidden></section>
</main>
<div id="modalRoot"></div>
<script>
const $ = sel => document.querySelector(sel);
const esc = s => (s ?? "").toString()
    .replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;");

// Live-Countdown bis zum nächsten Tick eines Workers (aus stats.next_run_at)
function fmtNext(stats) {
    const nra = stats && stats.next_run_at;
    if (!nra) return '<small style="color:#64748b;">—</small>';
    const t = Date.parse(nra.endsWith('Z') ? nra : nra + 'Z');
    if (isNaN(t)) return '<small style="color:#64748b;">—</small>';
    const rem = Math.max(0, Math.round((t - Date.now()) / 1000));
    if (rem === 0) return '<span class="badge" style="background:#052e16;color:#4ade80;">🟢 jetzt</span>';
    const col = rem < 15 ? '#4ade80' : (rem < 60 ? '#fde047' : '#67e8f9');
    const lbl = rem >= 60 ? `${Math.floor(rem/60)}:${String(rem%60).padStart(2,'0')}` : `${rem}s`;
    return `<span class="badge" style="background:#0f172a;color:${col};">⏳ ${lbl}</span>`;
}

// "vor Xs / Xm" für last_heartbeat → schnelle Tot/Lebend-Diagnose
function fmtHbAge(iso) {
    if (!iso) return '<small style="color:#64748b;">—</small>';
    const t = Date.parse(iso.endsWith('Z') ? iso : iso + 'Z');
    if (isNaN(t)) return '<small style="color:#64748b;">—</small>';
    const ago = Math.max(0, Math.round((Date.now() - t) / 1000));
    let lbl, col;
    if (ago < 30)        { lbl = `${ago}s`;             col = '#4ade80'; }
    else if (ago < 120)  { lbl = `${ago}s`;             col = '#fde047'; }
    else if (ago < 3600) { lbl = `${Math.floor(ago/60)}m`; col = '#fb923c'; }
    else                 { lbl = `${Math.floor(ago/3600)}h`; col = '#f87171'; }
    return `<small style="color:${col};">vor ${lbl}</small>`;
}

let activeTab = "overview";

document.querySelectorAll("nav button").forEach(b => {
    b.onclick = () => {
        document.querySelectorAll("nav button").forEach(x => x.classList.remove("active"));
        b.classList.add("active");
        activeTab = b.dataset.tab;
        document.querySelectorAll("main section").forEach(s => s.hidden = true);
        $("#tab-" + activeTab).hidden = false;
        render();
    };
});

async function api(path, opts) {
    const r = await fetch(path, opts);
    if (!r.ok) throw new Error(`${path} → ${r.status}`);
    const ct = r.headers.get("content-type") || "";
    return ct.includes("json") ? r.json() : r.text();
}

// ── Overview ──────────────────────────────────────────────
async function renderOverview() {
    const data = await api("/api/status");
    const c = data.deals || {};
    const op = data.opener || { in_list: 0, opened: 0, pending: 0 };

    // Pause-Flags parallel laden (für Pause/Resume-Badges in der Worker-Tabelle)
    let cfg = [];
    try { cfg = await api("/api/config"); } catch (e) {}
    const pausedMap = {};
    cfg.filter(c => c.key.startsWith("worker.") && c.key.endsWith(".paused"))
       .forEach(c => { pausedMap[c.key.split(".")[1]] = !!c.value; });

    // ── KPI-Zeile inkl. Opener-Pipeline ─────────────────────
    const cards = [
        ["queue", "Queue"], ["processing", "Processing"],
        ["sent", "Sent"], ["failed", "Failed"]
    ].map(([k,l]) =>
        `<div class="card"><h2>${l}</h2><div class="v ${k}">${c[k] ?? 0}</div></div>`
    ).join("");

    // Opener-Pipeline-Karte: product_list / opened / pending
    //   - in_list  : Anzahl Einträge in state_kv['product_list']
    //   - opened   : Anzahl Einträge in state_kv['opened']
    //   - pending  : product_list-Keys, die noch nicht in opened sind
    //                = echte „zu öffnen"-Restmenge des amazon_opener
    const openerCard = `
      <div class="card" style="grid-column: 1 / -1; background:#0b1220; border:1px solid #1e293b;">
        <h2>🛒 Opener-Pipeline (Amazon)</h2>
        <div style="display:flex; gap:24px; flex-wrap:wrap; margin-top:6px; align-items:flex-end;">
          <div>
            <div style="font-size:11px; color:#94a3b8; text-transform:uppercase;">In Liste</div>
            <div style="font-size:26px; font-weight:700; color:#e2e8f0;">${op.in_list}</div>
          </div>
          <div>
            <div style="font-size:11px; color:#94a3b8; text-transform:uppercase;">Bereits geöffnet</div>
            <div style="font-size:26px; font-weight:700; color:#86efac;">${op.opened}</div>
          </div>
          <div>
            <div style="font-size:11px; color:#94a3b8; text-transform:uppercase;">Noch zu öffnen</div>
            <div style="font-size:26px; font-weight:700; color:${op.pending > 0 ? '#fde047' : '#64748b'};">${op.pending}</div>
          </div>
        </div>
      </div>`;

    // ── „Nächster Versand"-Banner (kombiniert Queue-Count + Worker-ETA) ──
    const senders = (data.workers || []).filter(w => (w.stats || {}).next_run_at);
    let nextBanner = '';
    const queueN = c.queue ?? 0;
    if (queueN === 0) {
        nextBanner = `
        <div class="card" style="grid-column: 1 / -1; background:#0b1220; border:1px solid #1e293b;">
          <h2>Nächster Versand</h2>
          <div style="font-size:18px; color:#94a3b8;">Queue ist leer — kein Deal eingeplant</div>
        </div>`;
    } else if (senders.length) {
        const etas = senders.map(w => {
            const nra = w.stats.next_run_at;
            const t = Date.parse(nra.endsWith('Z') ? nra : nra + 'Z');
            return { name: w.name, t, paused: (w.current_task || '').includes('paused') };
        }).filter(x => !isNaN(x.t));
        if (etas.length) {
            etas.sort((a,b) => a.t - b.t);
            const earliest = etas[0];
            const rem = Math.max(0, Math.round((earliest.t - Date.now()) / 1000));
            const lbl = rem >= 60 ? `${Math.floor(rem/60)}:${String(rem%60).padStart(2,'0')} min` : `${rem} s`;
            const col = rem < 15 ? '#4ade80' : (rem < 60 ? '#fde047' : '#67e8f9');
            const all = etas.map(e => {
                const r = Math.max(0, Math.round((e.t - Date.now()) / 1000));
                const l = r >= 60 ? `${Math.floor(r/60)}:${String(r%60).padStart(2,'0')}` : `${r}s`;
                return `<span class="badge" style="background:#0f172a;color:#cbd5e1;margin-right:6px;">
                  ${esc(e.name)} <strong style="color:${col};">⏳ ${l}</strong></span>`;
            }).join('');
            nextBanner = `
            <div class="card" data-eta="${earliest.t}" style="grid-column: 1 / -1; background:#0b1220; border:1px solid #1e293b;">
              <h2>Nächster Versand <span style="color:#64748b;font-weight:normal;">(${queueN} in Queue)</span></h2>
              <div style="font-size:28px; font-weight:700; color:${col};">⏳ ${lbl}</div>
              <div style="margin-top:8px; font-size:12px; color:#94a3b8;">
                Frühester Worker: <strong>${esc(earliest.name)}</strong></div>
              <div style="margin-top:8px;">${all}</div>
            </div>`;
        }
    } else {
        nextBanner = `
        <div class="card" style="grid-column: 1 / -1; background:#0b1220; border:1px solid #1e293b;">
          <h2>Nächster Versand</h2>
          <div style="font-size:16px; color:#fde047;">
            ${queueN} Deal(s) in Queue, aber kein aktiver Sender-Worker meldet eine ETA.
          </div>
        </div>`;
    }

    // ── Top-5 Queue: was geht als nächstes raus? ────────────
    const nq = data.next_queue || [];
    const queueRows = nq.length ? nq.map((d, idx) => {
        const title = d.title ? esc(d.title).slice(0, 80) : `<small style="color:#64748b;">– kein Titel –</small>`;
        const market = d.market ? `<span class="tag">${esc(d.market)}</span>` : '';
        const prio = (d.priority || 0) !== 0
            ? `<span class="badge" style="background:#0c4a6e;color:#7dd3fc;">P ${d.priority}</span>`
            : '';
        const price = d.price != null ? `<small style="color:#86efac;">${esc(d.price)} €</small>` : '';
        return `
          <tr>
            <td style="color:#64748b; width:30px;">#${idx + 1}</td>
            <td>${title} ${prio}</td>
            <td>${market}</td>
            <td>${price}</td>
            <td><small style="color:#94a3b8;">${esc(d.created_at || "")}</small></td>
            <td>
              <button class="act" onclick="showDealDetail(${d.id})" title="Details">🔍</button>
            </td>
          </tr>`;
    }).join('') : `<tr><td colspan="6"><small style="color:#64748b;">Queue ist leer.</small></td></tr>`;

    const queueCard = `
      <div class="card" style="grid-column: 1 / -1; background:#0b1220; border:1px solid #1e293b;">
        <h2>📋 Nächste Deals in der Queue
          <span style="float:right; font-size:11px; color:#64748b; font-weight:normal;">
            Top ${nq.length} · sortiert nach Priorität, dann FIFO
          </span>
        </h2>
        <table style="margin-top:6px;"><tbody>${queueRows}</tbody></table>
      </div>`;

    // ── Worker-Tabelle mit Live-Countdown + Pause/Stop/Kill ─
    // Eine zentrale Tabelle für alle Worker (Facebook-Timer, Telegram-Router,
    // Amazon-Opener/Parser/Watcher/WS, Observer …). Spalte „Next" zeigt den
    // Live-Countdown aus stats.next_run_at — das ist genau das, was im
    // Worker-Log als "sleeping → next tick in Xs" steht.
    const rows = data.workers.length ? data.workers.map(w => {
        const paused = pausedMap[w.name];
        return `
        <tr>
          <td>${esc(w.name)}</td>
          <td><span class="pill ${esc(w.state)}">${esc(w.state)}</span>
              ${paused?'<span class="badge" style="background:#451a03;color:#fdba74;">⏸ pause</span>':''}</td>
          <td>${esc(w.current_task || "")}</td>
          <td>${fmtNext(w.stats)}</td>
          <td>${esc(w.pid || "")}</td>
          <td><small>${esc(w.started_at || "")}</small></td>
          <td>${fmtHbAge(w.last_heartbeat)}</td>
          <td class="row-actions">
            ${paused
              ? `<button class="act" onclick="workerCfg('${esc(w.name)}','resume')">▶ Resume</button>`
              : `<button class="act" onclick="workerCfg('${esc(w.name)}','pause')">⏸ Pause</button>`}
            <button class="act" onclick="signalWorker('${esc(w.name)}','stop')">⏻ Stop</button>
            <button class="act danger" onclick="signalWorker('${esc(w.name)}','kill')">⚡ Kill</button>
          </td>
        </tr>`;
    }).join("")
        : `<tr><td colspan="8"><small>noch keine Worker registriert</small></td></tr>`;

    $("#tab-overview").innerHTML = `
        <div class="grid">${cards}</div>
        ${openerCard}
        ${nextBanner}
        ${queueCard}
        <h2 style="font-size:13px; color:#94a3b8; text-transform:uppercase;
                   letter-spacing:.5px; margin:14px 0 8px;">⚙ Worker &amp; Timer</h2>
        <p style="margin:0 0 6px;"><small style="color:#64748b;">
          Spalte „Next" zeigt den Live-Countdown bis zum nächsten Tick (Facebook-Timer,
          Queue-Check, Inbox-Scan …). „Pause" setzt nur das DB-Flag, „Stop" sendet SIGTERM,
          „Kill" SIGKILL. Restart braucht <code>run_all.py</code>.
        </small></p>
        <table><thead><tr>
            <th>Name</th><th>Status</th><th>Aufgabe</th><th>Next</th>
            <th>PID</th><th>Started</th><th>Heartbeat</th><th></th>
        </tr></thead><tbody>${rows}</tbody></table>
        <p style="margin-top:14px;"><small style="color:#64748b;">
          Observer-/Worker-Schalter (z. B. Piraten-Observer pausieren) findest du im
          <a href="#" onclick="document.querySelector('nav button[data-tab=settings]').click(); return false;"
             style="color:#7dd3fc;">⚙ Settings</a>-Tab.
          URL manuell einreichen geht im
          <a href="#" onclick="document.querySelector('nav button[data-tab=products]').click(); return false;"
             style="color:#7dd3fc;">🛒 Produkte</a>-Tab.
        </small></p>`;
}

// Quick-Toggle für beliebige *.enabled-Flags in runtime_config.
// Wird von Settings-Tab aufgerufen. Re-rendert Settings nach dem Schreiben.
async function toggleConfigFlag(key, on) {
    try {
        await api(`/api/config/${encodeURIComponent(key)}`, {
            method: "PUT",
            headers: {"Content-Type":"application/json"},
            body: JSON.stringify({ value: !!on })
        });
        renderSettings();
    } catch (e) { alert("Fehlgeschlagen: " + e.message); }
}

// ── Verlauf / Timeline ────────────────────────────────────
let timelineHours = 24;
let timelineFilter = "";
const EVENT_BADGE = {
  created:       {label: "🆕 erfasst",       css: "background:#1e293b;color:#7dd3fc;"},
  updated:       {label: "✏️ aktualisiert",   css: "background:#1e293b;color:#fde68a;"},
  claimed:       {label: "🔒 claimed",        css: "background:#082f49;color:#7dd3fc;"},
  requeued:      {label: "↻ requeued",       css: "background:#451a03;color:#fdba74;"},
  retry:         {label: "⟳ retry",          css: "background:#451a03;color:#fdba74;"},
  failed:        {label: "❌ failed",         css: "background:#450a0a;color:#fca5a5;"},
  sent:          {label: "✅ sent",           css: "background:#052e16;color:#86efac;"},
  lock_released: {label: "🔓 lock_released",  css: "background:#1f2937;color:#9ca3af;"},
};
function badgeFor(evName) {
  const b = EVENT_BADGE[evName] || {label: evName, css: "background:#334155;color:#cbd5e1;"};
  return `<span class="badge" style="${b.css}">${esc(b.label)}</span>`;
}
function channelChips(detail) {
  if (!detail) return "";
  return detail.split(/[+,\s]+/).filter(Boolean).map(c => {
    const k = c.toLowerCase();
    let icon;
    if      (k.includes("face"))  icon = "📘 FB";
    else if (k.includes("insta")) icon = "📷 IG";
    else if (k.includes("tele"))  icon = "✈️ TG";
    else if (k.includes("whats")) icon = "💬 WA";
    else                          icon = esc(c);
    return `<span class="badge" style="background:#0b1220;color:#7dd3fc;border:1px solid #1e293b;">${icon}</span>`;
  }).join(" ");
}
async function renderTimeline() {
  const sec = $("#tab-timeline");
  const ev = await api(`/api/timeline?hours=${timelineHours}&limit=400`);
  const filt = timelineFilter.trim().toLowerCase();
  const visible = filt
    ? ev.filter(e =>
        (e.product_id||"").toLowerCase().includes(filt) ||
        (e.title||"").toLowerCase().includes(filt) ||
        (e.event||"").toLowerCase().includes(filt) ||
        (e.detail||"").toLowerCase().includes(filt) ||
        (e.market||"").toLowerCase().includes(filt))
    : ev;
  const rows = visible.length ? visible.map(e => {
    const dt = (e.created_at||"").replace("T"," ").slice(0, 19);
    const channels = e.event === "sent" ? channelChips(e.detail) : "";
    const detail = e.event === "sent" ? "" : esc(e.detail || "");
    const post = e.post_type === "reel" ? "🎬 reel" : "📄 offer";
    return `
      <tr>
        <td><small>${esc(dt)}</small></td>
        <td>${badgeFor(e.event)}</td>
        <td><span class="tag">${esc(e.product_id)}</span><br>
            <small>${esc(e.market)} · ${post} · status=${esc(e.status)}</small></td>
        <td>${esc((e.title||"").slice(0,90))}</td>
        <td>${channels}${detail ? `<small>${detail}</small>` : ""}</td>
        <td class="row-actions">
          <button class="act" onclick="showDealDetail(${e.deal_id})">🔍</button>
          <button class="act" onclick="requeueDeal(${e.deal_id})">↻</button>
        </td>
      </tr>`;
  }).join("") : `<tr><td colspan="6"><small>keine Events in den letzten ${timelineHours} h</small></td></tr>`;

  sec.innerHTML = `
    <p><small>Kompletter Pipeline-Verlauf: Link erfasst → AI-Extraktion → Post/Reel → Versand pro Kanal.
              Spalte <b>Kanal/Detail</b> zeigt bei <code>sent</code>-Events die Plattformen.</small></p>
    <div class="toolbar">
      <label>Zeitraum:</label>
      <select id="tlHours">
        ${[1,6,24,72,168].map(h => `<option value="${h}" ${h===timelineHours?"selected":""}>${h<24?h+" h":(h/24)+" d"}</option>`).join("")}
      </select>
      <input id="tlFilter" placeholder="Filter (ProduktID / Titel / Event / Markt)…" value="${esc(timelineFilter)}" style="min-width:340px;">
      <button class="act" onclick="renderTimeline()">↻ Refresh</button>
    </div>
    <table><thead><tr>
      <th>Zeit (UTC)</th><th>Event</th><th>Produkt</th><th>Titel</th>
      <th>Kanal / Detail</th><th></th>
    </tr></thead><tbody>${rows}</tbody></table>`;
  $("#tlHours").onchange = e => { timelineHours  = parseInt(e.target.value,10); renderTimeline(); };
  $("#tlFilter").oninput = e => { timelineFilter = e.target.value; renderTimeline(); };
}

// ── Deals (Cockpit: Filter + Bulk + Resend + Edit) ────────
let dealsStatus = "queue";
let dealsFilter = { q: "", market: "", only_no_image: false, only_no_price: false,
                    min_price: "", max_price: "" };
let dealsSelected = new Set();
async function renderDeals() {
    const sec = $("#tab-deals");
    const counts = (await api("/api/status")).deals;
    const tabs = ["queue","processing","sent","failed"].map(s =>
        `<button class="act"
                 style="${s===dealsStatus?'background:#0369a1':''}"
                 onclick="dealsStatus='${s}';dealsSelected.clear();renderDeals()">${s} (${counts[s]??0})</button>`
    ).join("");

    let markets = [];
    try { markets = await api("/api/deals/markets"); } catch(e) {}

    const params = new URLSearchParams({ status: dealsStatus, limit: 300 });
    if (dealsFilter.q) params.set("q", dealsFilter.q);
    if (dealsFilter.market) params.set("market", dealsFilter.market);
    if (dealsFilter.only_no_image) params.set("only_no_image", "true");
    if (dealsFilter.only_no_price) params.set("only_no_price", "true");
    if (dealsFilter.min_price) params.set("min_price", dealsFilter.min_price);
    if (dealsFilter.max_price) params.set("max_price", dealsFilter.max_price);
    const deals = await api(`/api/deals/search?${params}`);

    const rows = deals.length ? deals.map(d => {
      const checked = dealsSelected.has(d.id) ? "checked" : "";
      const prio = d.priority || 0;
      const prioColor = prio > 100 ? "color:#facc15;font-weight:600" : "color:#64748b";
      return `
        <tr>
          <td><input type="checkbox" ${checked}
                     onchange="toggleSelect(${d.id}, this.checked)"></td>
          <td>${d.id}</td>
          <td><span class="tag">${esc(d.product_id)}</span></td>
          <td>${esc(d.market)}</td>
          <td>${esc((d.title||"").slice(0,80))}</td>
          <td style="${prioColor}">${prio}</td>
          <td>${esc(d.retry_count||0)}</td>
          <td><small>${esc(d.created_at||"")}</small></td>
          <td class="row-actions">
            <button class="act" onclick="showDealDetail(${d.id})">🔍</button>
            <button class="act" onclick="resendDeal(${d.id})" title="Sofort erneut posten">🚀</button>
            <button class="act" onclick="editDeal(${d.id})" title="Bearbeiten">✏️</button>
            <button class="act" onclick="setPrio(${d.id})" title="Priorität">⭐</button>
            <button class="act" onclick="requeueDeal(${d.id})">↻</button>
            <button class="act danger" onclick="deleteDeal(${d.id})">🗑</button>
          </td>
        </tr>`;
    }).join("")
    : `<tr><td colspan="9"><small>keine Einträge</small></td></tr>`;

    const selCount = dealsSelected.size;
    const marketOpts = `<option value="">(alle Märkte)</option>` +
      markets.map(m => `<option ${m===dealsFilter.market?"selected":""}>${esc(m)}</option>`).join("");

    sec.innerHTML = `
        <div class="toolbar">${tabs}</div>
        ${dealsStatus==="queue" ? renderQueueBanner(deals.length) : ""}
        <div class="toolbar" style="flex-wrap:wrap;">
          <input id="dF_q" placeholder="Suche (ID/Titel/URL)…" value="${esc(dealsFilter.q)}" style="min-width:240px;">
          <select id="dF_market">${marketOpts}</select>
          <label><input type="checkbox" id="dF_noimg" ${dealsFilter.only_no_image?"checked":""}> nur ohne Bild</label>
          <label><input type="checkbox" id="dF_nopx" ${dealsFilter.only_no_price?"checked":""}> nur ohne Preis</label>
          <input id="dF_min" type="number" step="0.01" placeholder="min €" value="${esc(dealsFilter.min_price)}" style="width:90px;">
          <input id="dF_max" type="number" step="0.01" placeholder="max €" value="${esc(dealsFilter.max_price)}" style="width:90px;">
          <button class="act" onclick="applyDealFilter()">🔎 Filter</button>
          <button class="act" onclick="resetDealFilter()">✖ Reset</button>
        </div>
        <div class="toolbar" style="background:#0b1220;padding:8px 12px;border-radius:8px;">
          <strong>${selCount}</strong> ausgewählt
          <button class="act" onclick="selectAllDeals()">☑ Alle</button>
          <button class="act" onclick="dealsSelected.clear();renderDeals()">☐ Keine</button>
          <span style="border-left:1px solid #334155;height:18px;margin:0 6px;"></span>
          <button class="act" onclick="bulkAct('requeue')" ${selCount?'':'disabled'}>↻ Requeue</button>
          <button class="act" onclick="bulkAct('mark_sent')" ${selCount?'':'disabled'}>✅ Mark sent</button>
          <button class="act" onclick="bulkSetPrio()" ${selCount?'':'disabled'}>⭐ Priorität</button>
          <button class="act danger" onclick="bulkAct('delete')" ${selCount?'':'disabled'}>🗑 Löschen</button>
          <span style="border-left:1px solid #334155;height:18px;margin:0 6px;"></span>
          <button class="act" onclick="cleanupOld()" title="Cleanup">🧹 Alte ${esc(dealsStatus)} löschen…</button>
        </div>
        <table><thead><tr>
            <th></th><th>ID</th><th>Product</th><th>Market</th><th>Title</th>
            <th>Live-Status</th><th>Prio</th><th>Retry</th><th>Created</th><th></th>
        </tr></thead><tbody>${rows}</tbody></table>`;

    $("#dF_q").onchange = e => dealsFilter.q = e.target.value;
    $("#dF_market").onchange = e => { dealsFilter.market = e.target.value; applyDealFilter(); };
    $("#dF_noimg").onchange = e => { dealsFilter.only_no_image = e.target.checked; applyDealFilter(); };
    $("#dF_nopx").onchange = e => { dealsFilter.only_no_price = e.target.checked; applyDealFilter(); };
    $("#dF_min").onchange = e => dealsFilter.min_price = e.target.value;
    $("#dF_max").onchange = e => dealsFilter.max_price = e.target.value;
}
function applyDealFilter() { renderDeals(); }
function resetDealFilter() {
    dealsFilter = { q:"", market:"", only_no_image:false, only_no_price:false, min_price:"", max_price:"" };
    renderDeals();
}

// ─── Queue-Live-Banner (Tick-Anzeige) ───
// Standard-Tick aus core.config (WATCH_INTERVAL_SECS env, Fallback 10).
let __queueTickSecs = 10;
let __queueTickCounter = 0;
function renderQueueBanner(count) {
    const next = Math.max(0, __queueTickSecs - __queueTickCounter);
    const pct  = Math.min(100, (__queueTickCounter / __queueTickSecs) * 100);
    const msg  = count
      ? `<strong>${count}</strong> Deal(s) warten → nächster telRouter-Tick in <span class="tick">${next}s</span>`
      : `Queue leer — nächster Polling-Tick in <span class="tick">${next}s</span>`;
    return `<div class="queue-banner">
        <span>🔄</span><span style="flex:0 0 auto;">${msg}</span>
        <div class="bar"><div style="width:${pct}%;"></div></div>
    </div>`;
}
setInterval(() => {
    if (activeTab !== "deals" || dealsStatus !== "queue") { __queueTickCounter = 0; return; }
    __queueTickCounter += 1;
    if (__queueTickCounter >= __queueTickSecs) {
        __queueTickCounter = 0;
        renderDeals();  // alle Xs frisch laden, parallel zum Worker-Tick
    } else {
        // nur Banner aktualisieren (kein DB-Hit)
        const sec = document.querySelector("#tab-deals .queue-banner");
        if (sec) {
            const next = __queueTickSecs - __queueTickCounter;
            const pct  = Math.min(100, (__queueTickCounter / __queueTickSecs) * 100);
            const tEl = sec.querySelector(".tick");
            const bEl = sec.querySelector(".bar > div");
            if (tEl) tEl.textContent = `${next}s`;
            if (bEl) bEl.style.width = `${pct}%`;
        }
    }
}, 1000);
function toggleSelect(id, checked) {
    if (checked) dealsSelected.add(id); else dealsSelected.delete(id);
    // Nur Header-Zeile updaten — voll re-render macht Liste flackern
    renderDeals();
}
function selectAllDeals() {
    document.querySelectorAll("#tab-deals tbody tr").forEach(r => {
        const cb = r.querySelector("input[type=checkbox]");
        if (!cb) return;
        const m = r.querySelector("td:nth-child(2)");
        if (m) dealsSelected.add(parseInt(m.textContent, 10));
    });
    renderDeals();
}
async function bulkAct(action) {
    if (!dealsSelected.size) return;
    if (action === "delete" && !confirm(`${dealsSelected.size} Deals löschen?`)) return;
    const r = await api("/api/deals/bulk", {
        method: "POST", headers: {"Content-Type":"application/json"},
        body: JSON.stringify({ ids: [...dealsSelected], action })
    });
    alert(`${action}: ${r.affected} deals`);
    dealsSelected.clear();
    renderDeals();
}
async function bulkSetPrio() {
    const v = prompt("Priorität setzen (höher = zuerst):", "100");
    if (v === null) return;
    const r = await api("/api/deals/bulk", {
        method: "POST", headers: {"Content-Type":"application/json"},
        body: JSON.stringify({ ids:[...dealsSelected], action:"set_priority", priority: parseInt(v,10) })
    });
    alert(`set_priority: ${r.affected} deals`);
    renderDeals();
}
async function setPrio(id) {
    const v = prompt("Priorität (höher = zuerst):", "100");
    if (v === null) return;
    await api(`/api/deals/${id}/priority`, {
        method:"POST", headers:{"Content-Type":"application/json"},
        body: JSON.stringify({ priority: parseInt(v,10) })
    });
    renderDeals();
}
async function cleanupOld() {
    const d = prompt(`Lösche alle '${dealsStatus}' älter als wie viele Tage?`, "30");
    if (d === null) return;
    const days = parseInt(d, 10);
    // dry-run first
    const dry = await api("/api/deals/cleanup", {
        method:"POST", headers:{"Content-Type":"application/json"},
        body: JSON.stringify({ status: dealsStatus, days, dry_run:true })
    });
    if (!confirm(`${dry.candidates} Deals würden gelöscht. Fortfahren?`)) return;
    const res = await api("/api/deals/cleanup", {
        method:"POST", headers:{"Content-Type":"application/json"},
        body: JSON.stringify({ status: dealsStatus, days, dry_run:false })
    });
    alert(`Gelöscht: ${res.deleted}`);
    renderDeals();
}
async function resendDeal(id) {
    if (!confirm("Diesen Deal jetzt sofort erneut posten (Timer wird übersprungen)?")) return;
    const r = await api(`/api/deals/${id}/resend`, {
        method:"POST", headers:{"Content-Type":"application/json"},
        body: JSON.stringify({ instant:true })
    });
    alert(`Resend gequeued (Prio ${r.priority}). Skip-Wait aktiv: ${r.instant}`);
    renderDeals();
}
async function editDeal(id) {
    const d = await api(`/api/deals/${id}`);
    const p = d.payload || {};
    const fields = ["title","discounted_price","normal_price","discount_percent",
                    "image_url","video_url","affiliate_url","caption","cta"];
    const form = fields.map(f => {
        const v = (f === "title" || f === "affiliate_url") ? (d[f] ?? "") : (p[f] ?? "");
        return `<div class="kv"><div class="k">${esc(f)}</div>
                <div class="v"><input id="edit_${f}" value="${esc(v)}" style="width:100%;background:#0b1220;color:#e2e8f0;border:1px solid #334155;padding:6px;border-radius:5px;"></div></div>`;
    }).join("");
    $("#modalRoot").innerHTML = `
      <div class="modal-backdrop" onclick="if(event.target===this)closeModal()">
        <div class="modal">
          <button class="close" onclick="closeModal()">✕</button>
          <h2>Deal #${id} bearbeiten</h2>
          <h3>Felder</h3>
          ${form}
          <div style="margin-top:14px;">
            <button class="act" onclick="saveEdit(${id})">💾 Speichern</button>
            <button class="act" onclick="closeModal()">Abbrechen</button>
          </div>
        </div>
      </div>`;
}
async function saveEdit(id) {
    const fields = ["title","discounted_price","normal_price","discount_percent",
                    "image_url","video_url","affiliate_url","caption","cta"];
    const top = {}; const payload = {};
    for (const f of fields) {
        const el = document.getElementById(`edit_${f}`);
        if (!el) continue;
        const v = el.value;
        if (f === "title" || f === "affiliate_url") top[f] = v;
        else payload[f] = v;
    }
    top.payload = payload;
    await api(`/api/deals/${id}`, {
        method:"PATCH", headers:{"Content-Type":"application/json"},
        body: JSON.stringify(top)
    });
    closeModal();
    renderDeals();
}
async function requeueDeal(id) {
    await api(`/api/deals/${id}/requeue`, { method: "POST" });
    renderDeals();
}
async function deleteDeal(id) {
    if (!confirm("Deal #" + id + " löschen?")) return;
    await api(`/api/deals/${id}`, { method: "DELETE" });
    renderDeals();
}

// ── Deal-Detail Modal (zeigt die von der KI gemappten Produktdaten) ──
const PRODUCT_FIELD_GROUPS = [
    ["Kerndaten",      ["product_id","market","title","affiliate_url"]],
    ["KI-Klassifikation", ["produkt_kategorie","template_type","template_id"]],
    ["Preis",          ["normal_price","discounted_price","discount_percent","savings","currency"]],
    ["Beschreibung",   ["product_name","product_description","caption","cta","voiceover_text","website_text","website"]],
    ["Medien",         ["image_url","images","video_url"]],
];

function renderKV(payload, keys) {
    const rows = keys
        .filter(k => payload[k] !== undefined && payload[k] !== null && payload[k] !== "")
        .map(k => {
            const v = payload[k];
            let cell;
            if (k === "images" && Array.isArray(v)) {
                cell = `<div class="imgs">` +
                    v.slice(0,8).map(u => `<img src="${esc(u)}" loading="lazy">`).join("") +
                    `</div>`;
            } else if (k === "image_url" && typeof v === "string") {
                cell = `<img src="${esc(v)}" loading="lazy"><br><a href="${esc(v)}" target="_blank">${esc(v)}</a>`;
            } else if (typeof v === "string" && /^https?:\/\//i.test(v)) {
                cell = `<a href="${esc(v)}" target="_blank">${esc(v)}</a>`;
            } else if (typeof v === "object") {
                cell = `<pre class="json" style="margin:0;max-height:200px;">${esc(JSON.stringify(v,null,2))}</pre>`;
            } else {
                cell = esc(v);
            }
            return `<div class="k">${esc(k)}</div><div class="v">${cell}</div>`;
        }).join("");
    return rows ? `<div class="kv">${rows}</div>` : "";
}

async function showDealDetail(id) {
    let d;
    try { d = await api(`/api/deals/${id}`); }
    catch (e) { alert(e.message); return; }
    const payload = d.payload || {};
    const merged = Object.assign({
        product_id: d.product_id, market: d.market, title: d.title,
        affiliate_url: d.affiliate_url,
    }, payload);

    let groups = "";
    const usedKeys = new Set();
    for (const [label, keys] of PRODUCT_FIELD_GROUPS) {
        const html = renderKV(merged, keys);
        if (html) {
            groups += `<h3>${esc(label)}</h3>${html}`;
            keys.forEach(k => usedKeys.add(k));
        }
    }
    const restKeys = Object.keys(merged).filter(k => !usedKeys.has(k));
    const restHtml = renderKV(merged, restKeys);
    if (restHtml) groups += `<h3>Weitere KI-Felder</h3>` + restHtml;

    // ── Lifecycle-Übersicht aus Events ableiten ──────────────────
    const lc = summarizeLifecycle(d.events || [], d);
    const lifecycleHtml = renderLifecycle(lc, d);

    const events = (d.events || []).map(e =>
        `<tr><td><span class="badge">${esc(e.event)}</span></td>` +
        `<td><small>${esc(e.created_at||"")}</small></td>` +
        `<td>${esc(e.detail||"")}</td></tr>`
    ).join("") || `<tr><td colspan="3"><small>keine Events</small></td></tr>`;

    const rawJson = esc(JSON.stringify(payload, null, 2));

    $("#modalRoot").innerHTML = `
      <div class="modal-backdrop" onclick="if(event.target===this)closeModal()">
        <div class="modal">
          <button class="close" onclick="closeModal()">✕</button>
          <h2>Deal #${d.id} — <span class="tag">${esc(d.product_id)}</span></h2>
          <div><span class="badge">${esc(d.status)}</span>
               <span class="badge">${esc(d.market)}</span>
               <small>created ${esc(d.created_at||"")}</small></div>
          ${renderStepper(d.phases || [])}
          <h3>Lebenszyklus</h3>
          ${lifecycleHtml}
          ${groups || "<p><small>Kein Payload vorhanden</small></p>"}
          <h3>Roh-Payload (JSON)</h3>
          <pre class="json">${rawJson}</pre>
          <h3>Events <button class="act" style="float:right;font-size:11px;" onclick="loadDealLogs(${d.id})">📜 Logs laden</button></h3>
          <table><thead><tr><th>Event</th><th>Zeit</th><th>Detail</th></tr></thead>
            <tbody>${events}</tbody></table>
          <div id="dealLogs_${d.id}"></div>
        </div>
      </div>`;
}

// ── Lifecycle-Helper ──────────────────────────────────────────────
function summarizeLifecycle(events, d) {
    // sort ascending (oldest first) for first-seen lookup
    const evs = [...events].sort((a,b) => (a.created_at||"").localeCompare(b.created_at||""));
    const first = (name) => evs.find(e => e.event === name) || null;
    // posted ist ein Sammel-Event mit Detail = "facebook" | "instagram" | "telegram"
    const channels = {};
    for (const e of evs) {
        if (e.event === "posted") {
            const ch = (e.detail || "").toLowerCase();
            const key = ch.includes("telegram") ? "telegram"
                      : ch.includes("instagram") ? "instagram"
                      : ch.includes("facebook")  ? "facebook" : (ch || "other");
            if (!channels[key]) channels[key] = e.created_at || "";
        }
    }
    return {
        created:  first("created"),
        updated:  first("updated"),
        templateSelected: [...evs].reverse().find(e => e.event === "template_selected") || null,
        cacheHit:  first("render_cache_hit"),
        renderDone: first("render_done"),
        renderFailed: first("render_failed"),
        claimed: first("claimed"),
        channels,
        failed: first("failed"),
        sent:   first("sent"),
        sentDetail: ([...evs].reverse().find(e => e.event === "sent") || {}).detail || null,
    };
}
function renderLifecycle(lc, d) {
    const fmtTs = ts => ts ? esc(ts.replace("T"," ").slice(0,19)) : "—";
    const card = (icon, title, value, sub) => `
        <div class="lc-card">
            <div class="lc-icon">${icon}</div>
            <div class="lc-body">
                <div class="lc-title">${esc(title)}</div>
                <div class="lc-value">${value}</div>
                ${sub ? `<div class="lc-sub">${sub}</div>` : ""}
            </div>
        </div>`;

    // Quelle
    const sourceVal = `<span class="tag">${esc(d.market || "?")}</span>`;
    const sourceSub = lc.created ? `erfasst ${fmtTs(lc.created.created_at)}` : "";

    // Template
    let tmplVal = "<small>(noch nicht gewählt)</small>", tmplSub = "";
    if (lc.templateSelected) {
        const parts = String(lc.templateSelected.detail || "").split("|").map(s=>s.trim());
        const ttype = parts[0] || "?";
        const tid   = parts[1] || "";
        tmplVal = `<span class="badge">${esc(ttype)}</span>`;
        tmplSub = tid ? `<code>${esc(tid)}</code>` : "";
    }

    // Render
    let renderVal = "<small>—</small>", renderSub = "";
    if (lc.cacheHit) {
        renderVal = `<span class="badge" style="background:#052e16;color:#86efac;">♻️ Cache-Hit</span>`;
        renderSub = `${esc(lc.cacheHit.detail || "")} · ${fmtTs(lc.cacheHit.created_at)}`;
    } else if (lc.renderDone) {
        renderVal = `<span class="badge" style="background:#082f49;color:#7dd3fc;">✅ Creatomate</span>`;
        const url = (lc.renderDone.detail || "").startsWith("http")
            ? `<a href="${esc(lc.renderDone.detail)}" target="_blank">▶ Video</a>` : "";
        renderSub = `${url} · ${fmtTs(lc.renderDone.created_at)}`;
    } else if (lc.renderFailed) {
        renderVal = `<span class="badge" style="background:#450a0a;color:#fca5a5;">❌ Render-Fehler</span>`;
        renderSub = fmtTs(lc.renderFailed.created_at);
    }

    // Channels
    const allCh = ["facebook","instagram","telegram"];
    const chHtml = allCh.map(c => {
        const ts = lc.channels[c];
        const done = !!ts;
        const cls = done ? "ch-done" : "ch-pending";
        const icon = c === "facebook" ? "📘" : c === "instagram" ? "📷" : "✈";
        return `<span class="ch-pill ${cls}" title="${done?'versendet '+esc(ts):'nicht versendet'}">${icon} ${c}${done?' ✓':''}</span>`;
    }).join("");
    const channelsVal = chHtml;
    const channelsSub = lc.sentDetail ? `<small>final: ${esc(lc.sentDetail)}</small>` : "";

    return `
        <div class="lc-grid">
            ${card("🔗", "Quelle", sourceVal, sourceSub)}
            ${card("🎬", "Creatomate-Template", tmplVal, tmplSub)}
            ${card("🎞️", "Rendering", renderVal, renderSub)}
            ${card("📡", "Kanäle", channelsVal, channelsSub)}
        </div>`;
}
async function loadDealLogs(id) {
    const target = document.getElementById(`dealLogs_${id}`);
    if (!target) return;
    target.innerHTML = `<h3>Logs (alle Worker, letzte 2 Tage)</h3><pre class="json">… lädt …</pre>`;
    try {
        const txt = await fetch(`/api/deals/${id}/logs`).then(r => r.text());
        target.innerHTML = `<h3>Logs (alle Worker, letzte 2 Tage)</h3>
            <pre class="json" style="max-height:360px;overflow:auto;">${esc(txt)}</pre>`;
    } catch (e) {
        target.innerHTML = `<h3>Logs</h3><pre class="json">Fehler: ${esc(e.message)}</pre>`;
    }
}

function renderStepper(phases) {
    if (!phases || !phases.length) return "";
    const steps = phases.map(p => {
        const ts = p.at ? (p.at.replace("T", " ").slice(0, 19)) : "—";
        const det = p.detail ? `<div class="ts">${esc(p.detail.toString().slice(0,60))}</div>` : "";
        return `<div class="step ${esc(p.state)}">
            <div class="dot">${esc(p.icon || "•")}</div>
            <div class="lbl">${esc(p.label)}</div>
            <div class="ts">${esc(ts)}</div>${det}
          </div>`;
    }).join("");
    return `<h3>Pipeline-Phasen</h3><div class="stepper">${steps}</div>`;
}
function closeModal() { $("#modalRoot").innerHTML = ""; }
document.addEventListener("keydown", e => { if (e.key === "Escape") closeModal(); });

// ── Workers ───────────────────────────────────────────────
async function renderWorkers() {
    // Workers-Tab wurde in Overview eingebaut. Diese Funktion bleibt nur als
    // No-Op-Stub, damit ältere Inline-Aufrufe (z. B. aus signalWorker oder
    // workerCfg) keinen ReferenceError werfen. Re-Rendern erfolgt über
    // renderOverview().
    if (typeof renderOverview === "function") return renderOverview();
}
async function workerCfg(name, action) {
    await api(`/api/workers/${name}/${action}`, { method:"POST" });
    renderOverview();
}
async function signalWorker(name, action) {
    if (!confirm(`${action.toUpperCase()} → ${name}?`)) return;
    try {
        await api(`/api/workers/${name}/${action}`, { method: "POST" });
    } catch (e) { alert(e.message); }
    renderOverview();
}
async function submitUrl() {
    const u = $("#subUrl").value.trim();
    if (!u) return;
    try {
        const r = await api("/api/deals/submit_url", {
            method:"POST", headers:{"Content-Type":"application/json"},
            body: JSON.stringify({ url: u })
        });
        alert(r.added ? `✅ hinzugefügt (Größe ${r.size})` : `(bereits in product_list)`);
        $("#subUrl").value = "";
    } catch(e) { alert(e.message); }
}

// ── Logs (mit optionalem Live-Stream über WebSocket) ──────
let logWorker = null;
let logGrep = "";
let logWS = null;
let logLive = false;
async function renderLogs() {
    const idx = await api("/api/logs");
    const days = Object.keys(idx);
    let workers = [];
    if (days.length) workers = idx[days[0]] || [];
    if (!logWorker && workers.length) logWorker = workers[0];
    const opts = workers.map(w =>
        `<option ${w===logWorker?'selected':''}>${esc(w)}</option>`
    ).join("");
    let body = "";
    // Beim Tab-Wechsel ggf. WS schließen
    closeLogWS();
    if (logWorker && !logLive) {
        const q = new URLSearchParams({ lines: 300 });
        if (logGrep) q.set("grep", logGrep);
        body = await api(`/api/logs/${logWorker}?${q}`);
    }
    $("#tab-logs").innerHTML = `
        <div class="toolbar">
          <label>Worker:</label>
          <select id="logSel">${opts}</select>
          <input id="logGrep" placeholder="filter…" value="${esc(logGrep)}">
          <button class="act" onclick="renderLogs()">↻ Refresh</button>
          <label style="margin-left:10px;">
            <input type="checkbox" id="logLive" ${logLive?"checked":""}> 🔴 Live-Stream
          </label>
        </div>
        <pre class="log" id="logBody">${esc(body || (logLive ? "Verbinde…" : "# kein Worker ausgewählt"))}</pre>`;
    $("#logSel").onchange = e => { logWorker = e.target.value; renderLogs(); };
    $("#logGrep").onchange = e => { logGrep = e.target.value; renderLogs(); };
    $("#logLive").onchange = e => { logLive = e.target.checked; renderLogs(); };
    if (logLive && logWorker) openLogWS(logWorker);
}
function closeLogWS() {
    if (logWS) { try { logWS.close(); } catch(e){} logWS = null; }
}
function openLogWS(worker) {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    logWS = new WebSocket(`${proto}//${location.host}/ws/logs/${worker}`);
    const body = () => document.getElementById("logBody");
    logWS.onmessage = ev => {
        const el = body(); if (!el) return;
        if (logGrep && !ev.data.toLowerCase().includes(logGrep.toLowerCase())) return;
        el.textContent += ev.data;
        // max ca. 4000 Zeichen autoscroll
        if (el.textContent.length > 200000) el.textContent = el.textContent.slice(-150000);
        el.scrollTop = el.scrollHeight;
    };
    logWS.onclose = () => { /* still */ };
    logWS.onerror = () => { /* still */ };
}

// ── State ─────────────────────────────────────────────────
let stateKey = null;
async function renderState() {
    const list = await api("/api/state");
    const rows = list.map(k => `
        <tr>
          <td><a href="#" onclick="stateKey='${esc(k.key)}';renderState();return false">
              ${esc(k.key)}</a></td>
          <td><span class="tag">${esc(k.type)}</span></td>
          <td>${esc(k.size)}</td>
          <td class="row-actions">
            <button class="act danger" onclick="deleteState('${esc(k.key)}')">🗑</button>
          </td>
        </tr>`).join("");
    let detail = "<small>kein Key ausgewählt</small>";
    if (stateKey) {
        try {
            const v = await api(`/api/state/${encodeURIComponent(stateKey)}`);
            detail = `
              <h3 style="margin:14px 0 6px;">${esc(stateKey)}</h3>
              <textarea id="stateVal" style="width:100%;height:280px;background:#020617;
                  color:#cbd5e1;border:1px solid #334155;border-radius:8px;padding:10px;
                  font-family:monospace;font-size:12px;">${esc(JSON.stringify(v.value, null, 2))}</textarea>
              <div style="margin-top:8px;">
                <button class="act" onclick="saveState()">💾 Speichern</button>
                <small> ⚠ JSON-Validität wird geprüft.</small>
              </div>`;
        } catch (e) { detail = `<small>Fehler: ${esc(e.message)}</small>`; }
    }
    $("#tab-state").innerHTML = `
        <div style="display:grid;grid-template-columns:380px 1fr;gap:18px;">
          <div>
            <table><thead><tr><th>Key</th><th>Type</th><th>Size</th><th></th></tr></thead>
            <tbody>${rows || `<tr><td colspan="4"><small>leer</small></td></tr>`}</tbody></table>
          </div>
          <div>${detail}</div>
        </div>`;
}
async function saveState() {
    const txt = $("#stateVal").value;
    let val;
    try { val = JSON.parse(txt); }
    catch (e) { alert("Ungültiges JSON: " + e.message); return; }
    await api(`/api/state/${encodeURIComponent(stateKey)}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ value: val }),
    });
    renderState();
}
async function deleteState(k) {
    if (!confirm("Key '" + k + "' löschen?")) return;
    await api(`/api/state/${encodeURIComponent(k)}`, { method: "DELETE" });
    if (stateKey === k) stateKey = null;
    renderState();
}

// ── Produkte (product_list + opened-Status) ───────────────
// Eigener Tab. Zeigt alle vom Observer / Parser eingesammelten Amazon-URLs,
// ihren Opener-Status und erlaubt Einzel-Löschung + Komplett-Clear.
let productsFilter = "";
let productsOnlyPending = false;
function _fmtAge(iso) {
    if (!iso) return '<small style="color:#64748b;">—</small>';
    try {
        const t = Date.parse(iso.endsWith("Z") ? iso : iso + "Z");
        if (isNaN(t)) return esc(iso);
        const s = Math.max(0, Math.round((Date.now() - t) / 1000));
        if (s < 60) return `<small>${s}s</small>`;
        if (s < 3600) return `<small>${Math.floor(s/60)}m</small>`;
        if (s < 86400) return `<small>${Math.floor(s/3600)}h</small>`;
        return `<small>${Math.floor(s/86400)}d</small>`;
    } catch { return esc(iso); }
}
function _fmtOpened(epochOrIso) {
    if (!epochOrIso) return '<span class="pill stopped">⏳ offen</span>';
    let t;
    if (typeof epochOrIso === "number") {
        t = epochOrIso * 1000;
    } else {
        t = Date.parse(String(epochOrIso).endsWith("Z") ? epochOrIso : epochOrIso + "Z");
    }
    if (isNaN(t)) return '<span class="pill busy">✓ geöffnet</span>';
    const ago = Math.max(0, Math.round((Date.now() - t) / 1000));
    let lbl;
    if (ago < 60) lbl = `${ago}s`;
    else if (ago < 3600) lbl = `${Math.floor(ago/60)}m`;
    else if (ago < 86400) lbl = `${Math.floor(ago/3600)}h`;
    else lbl = `${Math.floor(ago/86400)}d`;
    return `<span class="pill busy">✓ vor ${lbl}</span>`;
}
async function renderProducts() {
    const data = await api("/api/products");
    let rows = data.rows || [];
    if (productsOnlyPending) rows = rows.filter(r => !r.opened_at);
    if (productsFilter) {
        const q = productsFilter.toLowerCase();
        rows = rows.filter(r =>
            (r.product_url || "").toLowerCase().includes(q) ||
            (r.asin || "").toLowerCase().includes(q) ||
            (r.product_name || "").toLowerCase().includes(q) ||
            (r.source || "").toLowerCase().includes(q) ||
            (r.key || "").toLowerCase().includes(q));
    }
    const trs = rows.map(r => {
        const urlShort = (r.product_url || "").replace(/^https?:\/\/(www\.)?/, "");
        const name = r.product_name
            ? `<div style="color:#e2e8f0; margin-bottom:2px;">${esc(r.product_name).slice(0,90)}</div>`
            : "";
        const priceTag = (r.price != null)
            ? `<span class="tag" style="color:#facc15;">${esc(r.price)}€</span>`
            : "";
        const discTag = (r.discount_percent != null)
            ? `<span class="tag" style="color:#4ade80;">-${esc(r.discount_percent)}%</span>`
            : "";
        return `
        <tr>
          <td><span class="tag">${esc(r.key)}</span>
              ${r.asin ? `<br><small style="color:#64748b;">${esc(r.asin)}</small>` : ""}</td>
          <td>${name}
              <a href="${esc(r.product_url || '#')}" target="_blank" rel="noopener"
                 style="color:#7dd3fc; font-size:12px;">${esc(urlShort).slice(0,120)}</a>
              ${(priceTag || discTag) ? `<div style="margin-top:4px;">${priceTag} ${discTag}</div>` : ""}
          </td>
          <td><span class="tag">${esc(r.source)}</span></td>
          <td>${_fmtAge(r.added_at)}</td>
          <td>${_fmtOpened(r.opened_at)}</td>
          <td class="row-actions">
            <button class="act" onclick="window.open('${esc(r.product_url || '')}','_blank')">↗ Öffnen</button>
            <button class="act danger" onclick="deleteProduct('${esc(r.key)}')">🗑</button>
          </td>
        </tr>`;
    }).join("");

    const header = `
      <div class="grid" style="grid-template-columns:repeat(3,1fr); margin-bottom:14px;">
        <div class="card"><h2>Total</h2><div class="v">${data.total ?? 0}</div></div>
        <div class="card"><h2>⏳ Offen</h2><div class="v queue">${data.pending ?? 0}</div></div>
        <div class="card"><h2>✓ Geöffnet</h2><div class="v sent">${data.opened ?? 0}</div></div>
      </div>
      <div class="toolbar">
        <input id="prodFilter" placeholder="Filter: ASIN, URL, Name, Quelle …"
               value="${esc(productsFilter)}" style="flex:1; min-width:240px;">
        <label style="display:flex; gap:6px; align-items:center; color:#94a3b8; font-size:12.5px;">
          <input type="checkbox" id="prodOnlyPending" ${productsOnlyPending ? "checked" : ""}>
          nur offene
        </label>
        <button class="act" onclick="submitNewProductUrl()">＋ URL hinzufügen</button>
        <button class="act" onclick="renderProducts()">↻ Refresh</button>
        <button class="act danger" onclick="clearAllProducts()">🗑 Liste leeren</button>
      </div>`;

    const body = rows.length === 0
        ? `<p style="color:#64748b; padding:24px;">Keine Produkte
             ${productsOnlyPending ? "(nur offene gefiltert)" : "in der Liste"}.</p>`
        : `<table>
             <thead><tr>
               <th>Key</th><th>Produkt / URL</th><th>Quelle</th>
               <th>Hinzugefügt</th><th>Status</th><th></th>
             </tr></thead>
             <tbody>${trs}</tbody>
           </table>`;

    $("#tab-products").innerHTML = header + body;
    const fi = $("#prodFilter"); if (fi) fi.oninput = (e) => {
        productsFilter = e.target.value;
        renderProducts();
    };
    const cb = $("#prodOnlyPending"); if (cb) cb.onchange = (e) => {
        productsOnlyPending = e.target.checked;
        renderProducts();
    };
}
async function deleteProduct(key) {
    if (!confirm(`Produkt ${key} aus der Liste löschen?`)) return;
    try {
        await api(`/api/products/${encodeURIComponent(key)}`, { method: "DELETE" });
        renderProducts();
    } catch (e) { alert("Fehler: " + e.message); }
}
async function clearAllProducts() {
    if (!confirm("Komplette product_list leeren?\\n(opened-Cache bleibt erhalten)")) return;
    const resetOpened = confirm("Auch opened-Cache zurücksetzen?\\n(OK = ja, Cancel = nein)");
    try {
        await api(`/api/products/clear`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ reset_opened: resetOpened }),
        });
        renderProducts();
    } catch (e) { alert("Fehler: " + e.message); }
}
async function submitNewProductUrl() {
    const url = prompt("Amazon-URL hinzufügen:");
    if (!url) return;
    try {
        const r = await api(`/api/deals/submit_url`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ url }),
        });
        if (r.added) alert(`✅ hinzugefügt als ${r.key} (Größe ${r.size})`);
        else alert(`ℹ️ bereits in der Liste: ${r.key}`);
        renderProducts();
    } catch (e) { alert("Fehler: " + e.message); }
}

// ── Settings (Runtime-Config: Channel-Toggles, Limits, …) ─
// Quick-Toggle-Block: vordefinierte Observer/Worker, die sich per
// `<name>.enabled`-Flag in runtime_config steuern lassen.
//   - Werte > 0 als ON, false als OFF; Default = ON (wenn Key fehlt)
//   - Wir setzen das Flag identisch zum bisherigen Piraten-Toggle, damit
//     der bestehende TTL-Bypass (.enabled-Suffix) greift.
// ACHTUNG: Damit ein Worker pausiert, muss er das Flag in seiner
// Hauptschleife selbst lesen. Aktuell respektiert es z.B. piraten.enabled
// im telObserver_piraten — andere Worker werden über die Anzeige hier
// nach und nach nachgezogen.
const WORKER_TOGGLES = [
    { key: "piraten.enabled",          icon: "🏴‍☠️", label: "Piraten-Observer",
      hint: "Telegram-Watcher für Piraten-Channels (übernimmt eingehende Links in die Queue)." },
    { key: "telegram_router.enabled",  icon: "✈️",   label: "Telegram-Router",
      hint: "Versendet Deals aus der Queue an die Telegram-Kanäle." },
    { key: "fb_watcher.enabled",       icon: "📘",   label: "Facebook-Watcher",
      hint: "Postet Reels/Beiträge nach dem Facebook-Timer." },
    { key: "amazon_opener.enabled",    icon: "🛒",   label: "Amazon-Opener",
      hint: "Öffnet Produkt-URLs in Chrome (product_list → opened)." },
    { key: "amazon_parser.enabled",    icon: "🤖",   label: "Amazon-Parser",
      hint: "Parst die vom Opener gelieferten HTML-Snapshots." },
];

async function renderSettings() {
    const items = await api("/api/config");
    // Map für schnellen Lookup der Toggle-States
    const cfgMap = {};
    items.forEach(it => { cfgMap[it.key] = it.value; });

    // ── Worker-Toggle-Block (über der RAW-KV-Tabelle) ──
    const toggleRows = WORKER_TOGGLES.map(t => {
        const on = cfgMap[t.key] !== false; // default ON
        const badge = on
            ? '<span class="pill running" style="font-size:12px;">🟢 AKTIV</span>'
            : '<span class="pill stopped" style="font-size:12px;">⏸ AUS</span>';
        const btn = on
            ? `<button class="act danger" onclick="toggleConfigFlag('${esc(t.key)}', false)">⏸ pausieren</button>`
            : `<button class="act" onclick="toggleConfigFlag('${esc(t.key)}', true)">▶ aktivieren</button>`;
        return `
          <tr>
            <td style="width:38%;">
              <strong style="font-size:14px;">${t.icon} ${esc(t.label)}</strong><br>
              <small style="color:#94a3b8;">${esc(t.hint)}</small><br>
              <small><span class="tag">${esc(t.key)}</span></small>
            </td>
            <td style="width:18%;">${badge}</td>
            <td>${btn}</td>
          </tr>`;
    }).join("");

    const togglesBlock = `
      <h3 style="margin:6px 0 6px;font-size:13px;color:#7dd3fc;
                 text-transform:uppercase;letter-spacing:.5px;">⚡ Observer & Worker</h3>
      <p><small style="color:#94a3b8;">
        Schaltet den jeweiligen Worker live an/aus (Cache &lt; 5 s).
        Worker müssen das Flag in ihrer Hauptschleife respektieren.
      </small></p>
      <table style="margin-bottom:18px;"><tbody>${toggleRows}</tbody></table>`;

    // ── Bestehender RAW-KV-Editor (gruppiert nach Namespace) ──
    const groups = {};
    items.forEach(it => {
        const ns = (it.key.split(".")[0] || "misc");
        (groups[ns] = groups[ns] || []).push(it);
    });
    const order = ["facebook","telegram","instagram","ai","filter","worker"];
    const known = new Set(order);
    const ns_list = [...order.filter(n => groups[n]), ...Object.keys(groups).filter(n => !known.has(n))];

    const html = ns_list.map(ns => {
        const rows = groups[ns].map(it => {
            const v = it.value;
            const isBool = typeof v === "boolean" || (it.key.endsWith(".enabled") || it.key.endsWith(".paused") || it.key.endsWith(".dry_run"));
            const isNum  = typeof v === "number";
            let ctrl;
            if (isBool) {
                const checked = v ? "checked" : "";
                ctrl = `<label class="switch"><input type="checkbox" ${checked}
                          onchange="saveCfg('${esc(it.key)}', this.checked)"> ${v?"ON":"OFF"}</label>`;
            } else if (isNum) {
                ctrl = `<input type="number" value="${esc(v)}" style="width:120px;"
                          onchange="saveCfg('${esc(it.key)}', parseFloat(this.value))">`;
            } else {
                ctrl = `<input value="${esc(JSON.stringify(v))}" style="min-width:280px;"
                          onchange="saveCfgRaw('${esc(it.key)}', this.value)">`;
            }
            return `
              <tr>
                <td><span class="tag">${esc(it.key)}</span><br>
                    <small>${esc(it.description || "")}</small></td>
                <td>${ctrl}</td>
                <td><small>${it.source === "default" ? "default" : "db · " + esc(it.updated_at||"")}</small></td>
                <td><button class="act danger" onclick="cfgReset('${esc(it.key)}')">↺ reset</button></td>
              </tr>`;
        }).join("");
        const nsTitle = ({
            facebook: "📘 Facebook", telegram: "✈️ Telegram", instagram: "📷 Instagram",
            ai: "🤖 KI / Extractor", filter: "🔧 Filter", worker: "👷 Worker-Steuerung",
        })[ns] || `📦 ${ns}`;
        return `
          <h3 style="margin:20px 0 6px;font-size:13px;color:#7dd3fc;
                     text-transform:uppercase;letter-spacing:.5px;">${nsTitle}</h3>
          <table><thead><tr>
            <th>Key</th><th>Wert</th><th>Quelle</th><th></th>
          </tr></thead><tbody>${rows}</tbody></table>`;
    }).join("");

    $("#tab-settings").innerHTML = `
        <p><small>Änderungen wirken <b>sofort</b> auf laufende Worker (Cache &lt; 5 s).
        ↺ reset löscht den DB-Wert → Default greift wieder.</small></p>
        <div class="toolbar">
          <button class="act" onclick="newCfgKey()">+ neuer Key</button>
          <button class="act" onclick="renderSettings()">↻ Refresh</button>
          <button class="act" onclick="dbBackup()">💾 DB-Backup</button>
          <button class="act" onclick="resetSentAsins()" title="Leert das Duplikat-Register → zuvor gesendete Deals können erneut versendet werden">🔄 sent_asins leeren</button>
          <button class="act danger" onclick="dbReset()">⚠ DB Reset</button>
        </div>
        ${togglesBlock}
        <h3 style="margin:18px 0 6px;font-size:13px;color:#7dd3fc;
                   text-transform:uppercase;letter-spacing:.5px;">🛠 Raw-KV-Editor</h3>
        ${html}`;
}
async function dbBackup() {
    try {
        const r = await api("/api/admin/db/backup", { method:"POST",
            headers:{"Content-Type":"application/json"}, body:"{}" });
        alert(`Backup erstellt:\n${r.path}`);
    } catch (e) { alert("Backup fehlgeschlagen: " + e); }
}
async function dbReset() {
    const t = prompt("⚠️ ACHTUNG: alle Deals, State, Config & Worker-Status werden gelöscht.\nVorher wird automatisch ein Backup in db/backups/ erzeugt.\n\nZum Bestätigen tippe: RESET");
    if (t !== "RESET") return;
    try {
        const r = await api("/api/admin/db/reset", { method:"POST",
            headers:{"Content-Type":"application/json"},
            body: JSON.stringify({ confirm:"RESET", backup:true }) });
        alert(`DB zurückgesetzt.\nBackup: ${r.backup || "(keins)"}\n\nBitte run_all neu starten, damit Worker frische Sessions ziehen.`);
        renderSettings();
    } catch (e) { alert("Reset fehlgeschlagen: " + e); }
}
async function resetSentAsins() {
    let info;
    try { info = await api("/api/admin/sent_asins"); } catch(e) { alert(e); return; }
    if (!confirm(`Duplikat-Register leeren?\n\nAktuell: ${info.asin_count} ASIN/Produkt-IDs, ${info.filehash_count} Filehashes.\n\nDanach können alle zuvor versendeten Deals erneut in die Queue → telRouter übernimmt sie wieder.`)) return;
    try {
        await api("/api/admin/sent_asins/reset", { method:"POST",
            headers:{"Content-Type":"application/json"}, body:"{}" });
        alert("✅ Duplikat-Register geleert.");
    } catch (e) { alert("Fehlgeschlagen: " + e); }
}
async function saveCfg(key, value) {
    await api(`/api/config/${encodeURIComponent(key)}`, {
        method: "PUT", headers: {"Content-Type":"application/json"},
        body: JSON.stringify({ value })
    });
    renderSettings();
}
async function saveCfgRaw(key, raw) {
    let v;
    try { v = JSON.parse(raw); }
    catch { v = raw; }
    await saveCfg(key, v);
}
async function cfgReset(key) {
    if (!confirm(`Reset ${key} auf Default?`)) return;
    await api(`/api/config/${encodeURIComponent(key)}`, { method:"DELETE" });
    renderSettings();
}
async function newCfgKey() {
    const k = prompt("Key (z.B. facebook.min_wait_secs):");
    if (!k) return;
    const v = prompt(`Wert für ${k} (JSON, z.B. true / 60 / "abc"):`, "true");
    if (v === null) return;
    let val; try { val = JSON.parse(v); } catch { val = v; }
    await saveCfg(k, val);
}
async function render() {
    try {
        if (activeTab === "overview") await renderOverview();
        else if (activeTab === "timeline") await renderTimeline();
        else if (activeTab === "deals") await renderDeals();
        else if (activeTab === "logs") await renderLogs();
        else if (activeTab === "state") await renderState();
        else if (activeTab === "products") await renderProducts();
        else if (activeTab === "settings") await renderSettings();
        $("#refreshTag").textContent = "✓ " + new Date().toLocaleTimeString();
    } catch (e) {
        $("#refreshTag").textContent = "⚠ " + e.message;
    }
}
render();
setInterval(() => {
    // Overview enthält jetzt die komplette Worker-Tabelle (mit Live-Timern),
    // daher ist Overview der Tab, der am häufigsten frisch sein muss.
    if (["overview","timeline","products"].includes(activeTab)) render();
    // Deals-Tab: nur queue/processing automatisch nachladen (5s)
    else if (activeTab === "deals" && ["queue","processing"].includes(dealsStatus)) renderDeals();
}, 3000);

// Sekunden-Tick: Overview-Banner (Nächster Versand) zwischen den API-Polls
// flüssig herunterzählen, ohne den ganzen DOM neu zu rendern.
setInterval(() => {
    if (activeTab !== "overview") return;
    const card = document.querySelector('#tab-overview .card[data-eta]');
    if (!card) return;
    const t = parseInt(card.getAttribute('data-eta'), 10);
    if (isNaN(t)) return;
    const rem = Math.max(0, Math.round((t - Date.now()) / 1000));
    const lbl = rem >= 60 ? `${Math.floor(rem/60)}:${String(rem%60).padStart(2,'0')} min` : `${rem} s`;
    const col = rem < 15 ? '#4ade80' : (rem < 60 ? '#fde047' : '#67e8f9');
    const big = card.querySelector('div[style*="font-size:28px"]');
    if (big) { big.textContent = `⏳ ${lbl}`; big.style.color = col; }
}, 1000);
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse(_INDEX_HTML)


def main() -> None:
    init_db()
    print(
        f"\033[36m[dashboard] listening on http://{DASHBOARD_HOST}:{DASHBOARD_PORT}\033[0m",
        flush=True,
    )
    uvicorn.run(app, host=DASHBOARD_HOST, port=DASHBOARD_PORT, log_level="warning")


if __name__ == "__main__":
    main()
