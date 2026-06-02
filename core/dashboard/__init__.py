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

from fastapi import FastAPI, HTTPException, Query, Body
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
import uvicorn

from core.config import DASHBOARD_HOST, DASHBOARD_PORT, LOG_DIR
from core.db import workers_repo, deals_repo, state_repo, init_db

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
# ──────────────────────────────────────────────────────────────

@app.get("/api/deals")
def api_deals(status: str = Query("queue"), limit: int = 200) -> list[dict]:
    return deals_repo.list_by_status(status, limit=limit)


@app.get("/api/deals/{deal_id}")
def api_deal_detail(deal_id: int) -> dict:
    d = deals_repo.get(deal_id)
    if not d:
        raise HTTPException(404, "deal not found")
    d["events"] = deals_repo.get_events(deal_id, limit=100)
    return d


@app.post("/api/deals/{deal_id}/requeue")
def api_deal_requeue(deal_id: int) -> dict:
    ok = deals_repo.requeue(deal_id)
    if not ok:
        raise HTTPException(404, "deal not found")
    return {"ok": True}


@app.get("/api/timeline")
def api_timeline(limit: int = 300, hours: int = 72) -> list[dict]:
    """Chronologischer Verlauf aller Pipeline-Events (link-erfasst → ai → sent / failed)."""
    return deals_repo.list_recent_events(limit=limit, hours=hours)


@app.delete("/api/deals/{deal_id}")
def api_deal_delete(deal_id: int) -> dict:
    ok = deals_repo.delete(deal_id)
    if not ok:
        raise HTTPException(404, "deal not found")
    return {"ok": True}


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
  </nav>
</header>
<main>
  <section id="tab-overview"></section>
  <section id="tab-timeline" hidden></section>
  <section id="tab-deals" hidden></section>
  <section id="tab-workers" hidden></section>
  <section id="tab-logs" hidden></section>
  <section id="tab-state" hidden></section>
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

// ── Deals ─────────────────────────────────────────────────
let dealsStatus = "queue";
async function renderDeals() {
    const sec = $("#tab-deals");
    const counts = (await api("/api/status")).deals;
    const tabs = ["queue","processing","sent","failed"].map(s =>
        `<button class="act ${s===dealsStatus?'':''}"
                 style="${s===dealsStatus?'background:#0369a1':''}"
                 onclick="dealsStatus='${s}';renderDeals()">${s} (${counts[s]??0})</button>`
    ).join("");
    const deals = await api(`/api/deals?status=${dealsStatus}&limit=200`);
    const rows = deals.length ? deals.map(d => `
        <tr>
          <td>${d.id}</td>
          <td><span class="tag">${esc(d.product_id)}</span></td>
          <td>${esc(d.market)}</td>
          <td>${esc((d.title||"").slice(0,80))}</td>
          <td>${esc(d.retry_count||0)}</td>
          <td><small>${esc(d.created_at||"")}</small></td>
          <td class="row-actions">
            <button class="act" onclick="showDealDetail(${d.id})">🔍 Details</button>
            <button class="act" onclick="requeueDeal(${d.id})">↻ Requeue</button>
            <button class="act danger" onclick="deleteDeal(${d.id})">🗑 Delete</button>
          </td>
        </tr>`).join("")
        : `<tr><td colspan="7"><small>keine Einträge</small></td></tr>`;
    sec.innerHTML = `
        <div class="toolbar">${tabs}</div>
        <table><thead><tr>
            <th>ID</th><th>Product</th><th>Market</th><th>Title</th>
            <th>Retry</th><th>Created</th><th></th>
        </tr></thead><tbody>${rows}</tbody></table>`;
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
          ${groups || "<p><small>Kein Payload vorhanden</small></p>"}
          <h3>Roh-Payload (JSON)</h3>
          <pre class="json">${rawJson}</pre>
          <h3>Events</h3>
          <table><thead><tr><th>Event</th><th>Zeit</th><th>Detail</th></tr></thead>
            <tbody>${events}</tbody></table>
        </div>
      </div>`;
}
function closeModal() { $("#modalRoot").innerHTML = ""; }
document.addEventListener("keydown", e => { if (e.key === "Escape") closeModal(); });

// ── Workers ───────────────────────────────────────────────
async function renderWorkers() {
    const data = await api("/api/status");
    const rows = data.workers.map(w => `
        <tr>
          <td>${esc(w.name)}</td>
          <td><span class="pill ${esc(w.state)}">${esc(w.state)}</span></td>
          <td>${esc(w.current_task || "")}</td>
          <td>${esc(w.pid || "")}</td>
          <td><small>${esc(w.started_at || "")}</small></td>
          <td><small>${esc(w.last_heartbeat || "")}</small></td>
          <td class="row-actions">
            <button class="act" onclick="signalWorker('${esc(w.name)}','stop')">⏻ Stop</button>
            <button class="act danger" onclick="signalWorker('${esc(w.name)}','kill')">⚡ Kill</button>
          </td>
        </tr>`).join("");
    $("#tab-workers").innerHTML = `
        <p><small>Stop sendet SIGTERM (sauber), Kill sendet SIGKILL.
        Restart muss über <code>run_all.py</code> erfolgen.</small></p>
        <table><thead><tr>
            <th>Name</th><th>Status</th><th>Aufgabe</th><th>PID</th>
            <th>Started</th><th>Heartbeat</th><th></th>
        </tr></thead><tbody>${rows}</tbody></table>`;
}
async function signalWorker(name, action) {
    if (!confirm(`${action.toUpperCase()} → ${name}?`)) return;
    try {
        await api(`/api/workers/${name}/${action}`, { method: "POST" });
    } catch (e) { alert(e.message); }
    renderWorkers();
}

// ── Logs ──────────────────────────────────────────────────
let logWorker = null;
let logGrep = "";
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
    if (logWorker) {
        const q = new URLSearchParams({ lines: 300 });
        if (logGrep) q.set("grep", logGrep);
        body = await api(`/api/logs/${logWorker}?${q}`);
    } else {
        body = "# keine Logs vorhanden";
    }
    $("#tab-logs").innerHTML = `
        <div class="toolbar">
          <label>Worker:</label>
          <select id="logSel">${opts}</select>
          <input id="logGrep" placeholder="filter…" value="${esc(logGrep)}">
          <button class="act" onclick="renderLogs()">↻ Refresh</button>
        </div>
        <pre class="log">${esc(body)}</pre>`;
    $("#logSel").onchange = e => { logWorker = e.target.value; renderLogs(); };
    $("#logGrep").onchange = e => { logGrep = e.target.value; renderLogs(); };
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

// ── Dispatcher / Auto-Refresh ─────────────────────────────
async function render() {
    try {
        if (activeTab === "overview") await renderOverview();
        else if (activeTab === "timeline") await renderTimeline();
        else if (activeTab === "deals") await renderDeals();
        else if (activeTab === "workers") await renderWorkers();
        else if (activeTab === "logs") await renderLogs();
        else if (activeTab === "state") await renderState();
        $("#refreshTag").textContent = "✓ " + new Date().toLocaleTimeString();
    } catch (e) {
        $("#refreshTag").textContent = "⚠ " + e.message;
    }
}
render();
setInterval(() => {
    if (["overview","workers","deals","timeline"].includes(activeTab)) render();
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
