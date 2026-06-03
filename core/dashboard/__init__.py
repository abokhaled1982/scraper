"""
core.dashboard — FastAPI-Web-Dashboard (Control Center).

Tabs:
    - Overview : Counts + Worker-Liste mit Status/Heartbeat
    - Deals    : Queue/Processing/Sent/Failed inkl. Requeue + Delete
    - Workers  : Detailansicht + Stop/Kill via PID-Signal
    - Logs     : Letzte N Zeilen pro Worker aus .log/<DATE>/<worker>.log
    - State    : StateKV-Browser/Editor (sent_ids, product_list, ...)

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

app = FastAPI(title="Scraper Dashboard")


@app.on_event("startup")
def _on_startup() -> None:
    init_db()


# ──────────────────────────────────────────────────────────────
# Status / Overview
# ──────────────────────────────────────────────────────────────

@app.get("/api/status")
def api_status() -> dict[str, Any]:
    return {
        "workers": workers_repo.list_all(),
        "deals": deals_repo.counts_by_status(),
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
    sehen würde). product_opener pickt sie auf."""
    url = (body.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "invalid url")
    try:
        current = state_repo.get("product_list", []) or []
        if isinstance(current, dict):
            current = list(current.values())
        if not isinstance(current, list):
            current = []
        if url not in current:
            current.append(url)
            state_repo.put("product_list", current)
            return {"ok": True, "added": True, "size": len(current)}
        return {"ok": True, "added": False, "size": len(current)}
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
    # Entfernt aus sent_ids:facebook, sonst springt der fb_watcher nicht an
    try:
        sent = state_repo.get_set("sent_ids:facebook")
        if d.get("product_id") in sent:
            sent.discard(d["product_id"])
            state_repo.put("sent_ids:facebook", sorted(sent))
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
</style>
</head>
<body>
<header>
  <h1>📊 Scraper Control Center</h1>
  <span class="tag" id="refreshTag">– offline –</span>
  <nav>
    <button data-tab="overview" class="active">Overview</button>
    <button data-tab="timeline">Verlauf</button>
    <button data-tab="deals">Deals</button>
    <button data-tab="workers">Workers</button>
    <button data-tab="logs">Logs</button>
    <button data-tab="state">State</button>
    <button data-tab="settings">⚙ Settings</button>
  </nav>
</header>
<main>
  <section id="tab-overview"></section>
  <section id="tab-timeline" hidden></section>
  <section id="tab-deals" hidden></section>
  <section id="tab-workers" hidden></section>
  <section id="tab-logs" hidden></section>
  <section id="tab-state" hidden></section>
  <section id="tab-settings" hidden></section>
</main>
<div id="modalRoot"></div>
<script>
const $ = sel => document.querySelector(sel);
const esc = s => (s ?? "").toString()
    .replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;");

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
    const c = data.deals;
    const cards = [
        ["queue", "Queue"], ["processing", "Processing"],
        ["sent", "Sent"], ["failed", "Failed"]
    ].map(([k,l]) =>
        `<div class="card"><h2>${l}</h2><div class="v ${k}">${c[k] ?? 0}</div></div>`
    ).join("");
    const rows = data.workers.length ? data.workers.map(w => `
        <tr>
          <td>${esc(w.name)}</td>
          <td><span class="pill ${esc(w.state)}">${esc(w.state)}</span></td>
          <td>${esc(w.current_task || "")}</td>
          <td>${esc(w.pid || "")}</td>
          <td><small>${esc(w.last_heartbeat || "")}</small></td>
        </tr>`).join("")
        : `<tr><td colspan="5"><small>noch keine Worker registriert</small></td></tr>`;
    $("#tab-overview").innerHTML = `
        <div class="grid">${cards}</div>
        <h2 style="font-size:13px; color:#94a3b8; text-transform:uppercase;
                   letter-spacing:.5px; margin:0 0 8px;">Worker</h2>
        <table><thead><tr>
            <th>Name</th><th>Status</th><th>Aufgabe</th><th>PID</th><th>Heartbeat</th>
        </tr></thead><tbody>${rows}</tbody></table>`;
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
            <th>Prio</th><th>Retry</th><th>Created</th><th></th>
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
    // Top-Level Deal-Spalten zusaetzlich in payload mergen fuer einheitliche Anzeige
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
    // Restliche Felder (die KI kann variabel weitere Felder liefern)
    const restKeys = Object.keys(merged).filter(k => !usedKeys.has(k));
    const restHtml = renderKV(merged, restKeys);
    if (restHtml) groups += `<h3>Weitere KI-Felder</h3>` + restHtml;

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
          ${groups || "<p><small>Kein Payload vorhanden</small></p>"}
          <h3>Roh-Payload (JSON)</h3>
          <pre class="json">${rawJson}</pre>
          <h3>Events</h3>
          <table><thead><tr><th>Event</th><th>Zeit</th><th>Detail</th></tr></thead>
            <tbody>${events}</tbody></table>
        </div>
      </div>`;
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
    const data = await api("/api/status");
    let cfg = [];
    try { cfg = await api("/api/config"); } catch(e) {}
    const pausedMap = {};
    cfg.filter(c => c.key.startsWith("worker.") && c.key.endsWith(".paused"))
       .forEach(c => { pausedMap[c.key.split(".")[1]] = !!c.value; });

    const rows = data.workers.map(w => {
        const paused = pausedMap[w.name];
        return `
        <tr>
          <td>${esc(w.name)}</td>
          <td><span class="pill ${esc(w.state)}">${esc(w.state)}</span>
              ${paused?'<span class="badge" style="background:#451a03;color:#fdba74;">⏸ pause</span>':''}</td>
          <td>${esc(w.current_task || "")}</td>
          <td>${esc(w.pid || "")}</td>
          <td><small>${esc(w.started_at || "")}</small></td>
          <td><small>${esc(w.last_heartbeat || "")}</small></td>
          <td class="row-actions">
            ${paused
              ? `<button class="act" onclick="workerCfg('${esc(w.name)}','resume')">▶ Resume</button>`
              : `<button class="act" onclick="workerCfg('${esc(w.name)}','pause')">⏸ Pause</button>`}
            <button class="act" onclick="signalWorker('${esc(w.name)}','stop')">⏻ Stop</button>
            <button class="act danger" onclick="signalWorker('${esc(w.name)}','kill')">⚡ Kill</button>
          </td>
        </tr>`;
    }).join("");
    $("#tab-workers").innerHTML = `
        <p><small>Pause/Resume setzt ein Flag in der DB — der Worker pollt es.
        Stop = SIGTERM, Kill = SIGKILL. Restart braucht <code>run_all.py</code>.</small></p>
        <table><thead><tr>
            <th>Name</th><th>Status</th><th>Aufgabe</th><th>PID</th>
            <th>Started</th><th>Heartbeat</th><th></th>
        </tr></thead><tbody>${rows}</tbody></table>

        <h3 style="font-size:13px;color:#94a3b8;margin:18px 0 6px;text-transform:uppercase;letter-spacing:.5px;">
            URL manuell einreichen</h3>
        <div class="toolbar">
          <input id="subUrl" placeholder="https://www.amazon.de/dp/B0..." style="min-width:480px;">
          <button class="act" onclick="submitUrl()">📨 In product_list einreihen</button>
        </div>
        <small>Wird vom <code>product_opener</code> aufgegriffen wie ein Telegram-Observer-Link.</small>`;
}
async function workerCfg(name, action) {
    await api(`/api/workers/${name}/${action}`, { method:"POST" });
    renderWorkers();
}
async function signalWorker(name, action) {
    if (!confirm(`${action.toUpperCase()} → ${name}?`)) return;
    try {
        await api(`/api/workers/${name}/${action}`, { method: "POST" });
    } catch (e) { alert(e.message); }
    renderWorkers();
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

// ── Settings (Runtime-Config: Channel-Toggles, Limits, …) ─
async function renderSettings() {
    const items = await api("/api/config");
    // Gruppiere nach Namespace (vor erstem Punkt)
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
        </div>
        ${html}`;
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
        else if (activeTab === "workers") await renderWorkers();
        else if (activeTab === "logs") await renderLogs();
        else if (activeTab === "state") await renderState();
        else if (activeTab === "settings") await renderSettings();
        $("#refreshTag").textContent = "✓ " + new Date().toLocaleTimeString();
    } catch (e) {
        $("#refreshTag").textContent = "⚠ " + e.message;
    }
}
render();
setInterval(() => {
    if (["overview","workers","timeline"].includes(activeTab)) render();
}, 3000);
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse(_INDEX_HTML)


def main() -> None:
    init_db()
    uvicorn.run(app, host=DASHBOARD_HOST, port=DASHBOARD_PORT, log_level="warning")


if __name__ == "__main__":
    main()
