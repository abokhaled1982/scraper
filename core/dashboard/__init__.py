"""
core.dashboard — FastAPI-Web-Dashboard.

Routes:
    GET /            → HTML-Übersicht (auto-refresh 3s)
    GET /api/status  → JSON: {workers, deals_by_status}
    GET /api/deals?status=queue → JSON

Start als Modul:
    python -m core.dashboard
"""
from __future__ import annotations
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse
import uvicorn

from core.config import DASHBOARD_HOST, DASHBOARD_PORT
from core.db import workers_repo, deals_repo, init_db

app = FastAPI(title="Scraper Dashboard")


@app.on_event("startup")
def _on_startup():
    init_db()


@app.get("/api/status")
def api_status():
    return {
        "workers": workers_repo.list_all(),
        "deals": deals_repo.counts_by_status(),
    }


@app.get("/api/deals")
def api_deals(status: str = Query("queue"), limit: int = 100):
    return deals_repo.list_by_status(status, limit=limit)


_INDEX_HTML = """<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <title>Scraper Dashboard</title>
  <meta http-equiv="refresh" content="3">
  <style>
    body { font-family: -apple-system, Segoe UI, Roboto, sans-serif; margin: 0; padding: 20px;
           background:#0f172a; color:#e2e8f0; }
    h1 { margin: 0 0 16px; font-size: 20px; color:#7dd3fc; }
    .grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 24px; }
    .card { background:#1e293b; border-radius: 10px; padding: 16px; }
    .card h2 { margin:0; font-size:13px; color:#94a3b8; text-transform: uppercase; letter-spacing:.5px;}
    .card .v { font-size: 32px; margin-top: 6px; font-weight: 600; }
    .queue   { color:#facc15; }
    .processing { color:#7dd3fc; }
    .sent    { color:#4ade80; }
    .failed  { color:#f87171; }
    table { width:100%; border-collapse: collapse; background:#1e293b; border-radius:10px; overflow:hidden; }
    th, td { padding: 10px 14px; text-align: left; font-size: 13px; border-bottom: 1px solid #334155; }
    th { background:#0b1220; color:#94a3b8; text-transform: uppercase; font-size: 11px; letter-spacing:.5px; }
    .pill { display:inline-block; padding: 2px 8px; border-radius: 999px; font-size: 11px; font-weight: 600; }
    .pill.idle  { background:#0b1220; color:#94a3b8; }
    .pill.busy  { background:#082f49; color:#7dd3fc; }
    .pill.error { background:#450a0a; color:#fca5a5; }
    .pill.stale { background:#451a03; color:#fdba74; }
    .pill.stopped { background:#1f2937; color:#9ca3af; }
    small { color:#64748b; }
  </style>
</head>
<body>
  <h1>📊 Scraper Dashboard <small>– auto-refresh 3s</small></h1>

  <div class="grid" id="cards">__CARDS__</div>

  <h2 style="font-size:14px; color:#94a3b8; text-transform:uppercase; letter-spacing:.5px;">Worker</h2>
  <table>
    <thead><tr><th>Name</th><th>Status</th><th>Aktuelle Aufgabe</th><th>PID</th><th>Letzter Heartbeat</th></tr></thead>
    <tbody>__WORKERS__</tbody>
  </table>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def index():
    counts = deals_repo.counts_by_status()
    cards = "".join(
        f'<div class="card"><h2>{label}</h2>'
        f'<div class="v {cls}">{counts.get(key, 0)}</div></div>'
        for key, label, cls in [
            ("queue", "Queue", "queue"),
            ("processing", "Processing", "processing"),
            ("sent", "Sent", "sent"),
            ("failed", "Failed", "failed"),
        ]
    )
    rows = []
    for w in workers_repo.list_all():
        rows.append(
            f"<tr>"
            f"<td>{w['name']}</td>"
            f"<td><span class='pill {w['state']}'>{w['state']}</span></td>"
            f"<td>{w.get('current_task') or '<small>–</small>'}</td>"
            f"<td>{w.get('pid') or ''}</td>"
            f"<td><small>{w.get('last_heartbeat') or ''}</small></td>"
            f"</tr>"
        )
    if not rows:
        rows.append('<tr><td colspan="5"><small>noch keine Worker registriert</small></td></tr>')
    html = _INDEX_HTML.replace("__CARDS__", cards).replace("__WORKERS__", "".join(rows))
    return HTMLResponse(html)


def main() -> None:
    init_db()
    uvicorn.run(app, host=DASHBOARD_HOST, port=DASHBOARD_PORT, log_level="warning")


if __name__ == "__main__":
    main()
