"""
core.logging.tui_server — Logger-Server mit LIVE-TUI-Konsole.

Statt einer endlos scrollenden Konsole zeigt dieser Server einen
festen Bildschirm mit Panels:

  ┌──────────────────────────────────────────────────────────┐
  │  Header (Uhrzeit, Uptime, Modus)                         │
  ├────────────────────────┬─────────────────────────────────┤
  │  Workers (Status)      │  Deals (queue/sent/failed/…)    │
  ├────────────────────────┴─────────────────────────────────┤
  │  Letzte WICHTIGE Events (Link rein, Deal raus, Fehler)  │
  └──────────────────────────────────────────────────────────┘

Drop-in-Ersatz für `core.logging.server` — selber Port, selbes Protokoll.
ALLE Logs werden weiterhin VOLLSTÄNDIG in `.log/<DATUM>/<worker>.log`
geschrieben. Nur die Konsole zeigt eine kompakte Übersicht.

Start:
    python -m core.logging.tui_server
"""
from __future__ import annotations

import logging
import os
import pickle
import re
import socketserver
import struct
import sys
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Deque, Dict, List

from rich.console import Console, Group
from rich.live import Live
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.align import Align

from core.config import LOG_HOST, LOG_PORT, LOG_DIR


# ───────────────────────────────────────────────────────────────
# Was IST wichtig? (Filter für das Event-Panel)
# Wir matchen den Roh-Text der LogRecords. Alles was nicht matched
# läuft trotzdem in die Datei, aber nicht auf den Schirm.
#
# Reihenfolge = Priorität. Erstes Match gewinnt.
# ───────────────────────────────────────────────────────────────
_EVENT_PATTERNS: List[tuple[str, re.Pattern, str]] = [
    # (Kategorie, Pattern, Farb-Tag)

    # --- LINKS REIN (Observer, Watcher, Parser) ---
    ("LINK",   re.compile(r"neue Links zur DB|Received Product URL", re.I), "bold cyan"),

    # --- OPEN (nur die echten Chrome-Öffnungen, kein Skip/Polling) ---
    ("OPEN",   re.compile(r"\bOPEN\b\s+[A-Z0-9]{8,}\s*->"), "cyan"),

    # --- AI-Extraktion ---
    ("AI",     re.compile(r"Sende Extraktionsanfrage|Antwort erhalten|extracted_data", re.I), "magenta"),

    # --- Facebook posts ---
    ("FB",     re.compile(r"\[FACEBOOK\]|send_post|Pause beendet|🚀 Guter Deal", re.I), "blue"),

    # --- Reels / Video ---
    ("REEL",   re.compile(r"\[VIDEO\]|render_reel|Reel erfolgreich|Creatomate", re.I), "yellow"),

    # --- Telegram Sender (eigene Posts), nicht Observer-Spam ---
    ("TG",     re.compile(r"telSender|telRouter|✉️.*Telegram", re.I), "cyan"),

    # --- Instagram ---
    ("IG",     re.compile(r"\[INSTAGRAM\]|ig_service", re.I), "magenta"),

    # --- Erfolgreich gepostet ---
    ("SENT",   re.compile(r"mark_sent|als 'sent' markiert|✅.*gepostet", re.I), "bold green"),

    # --- Gescheitert ---
    ("FAIL",   re.compile(r"\[FILTER\]|mark_failed|🗑️|fehlgeschlagen", re.I), "bold red"),
]

# Explizit unterdrücken — diese Logs landen NUR in der Datei, nie im Event-Panel,
# selbst wenn sie WARN/ERROR wären (rare). Hier nur INFO-Spam von opener etc.
_EVENT_SUPPRESS = re.compile(
    r"waiting for items|Cycle complete|Considering \d+|opened=|Nichts Neues|"
    r"\[SKIP\]|Nächster Start in:|Letzter Deal:|STATUS-REPORT|Fortschritt|"
    r"Noch offen|Pause beendet\.\s*Suche",
    re.I,
)


def _classify(record: logging.LogRecord) -> tuple[str, str] | None:
    """Liefert (Kategorie, Farbe) wenn das Event wichtig ist – sonst None."""
    try:
        msg = record.getMessage()
    except Exception:
        return None

    # Erst Suppress prüfen — auch ERROR-Level: diese Zeilen sind Status-Spam.
    if _EVENT_SUPPRESS.search(msg):
        return None

    # Errors immer durchlassen (außer suppressed oben)
    if record.levelno >= logging.ERROR:
        return ("ERROR", "bold red")

    for cat, pat, color in _EVENT_PATTERNS:
        if pat.search(msg):
            return (cat, color)

    # Warnings nur generisch, wenn nichts gematcht
    if record.levelno >= logging.WARNING:
        return ("WARN", "yellow")
    return None


# ───────────────────────────────────────────────────────────────
# Spezial-Extraktoren (für Header-Widgets)
# ───────────────────────────────────────────────────────────────
_FB_TIMER_RE = re.compile(
    r"Letzter Deal:\s*(\d{2}:\d{2}:\d{2}).*?Nächster Start in:\s*\[\s*(\d{2}):(\d{2})\s*\]"
)
_FB_BEEN_RE  = re.compile(r"Pause beendet\.\s*Suche nach neuen Deals", re.I)


# ───────────────────────────────────────────────────────────────
# TUI-State (Thread-safe)
# ───────────────────────────────────────────────────────────────
class _State:
    def __init__(self, max_events: int = 18) -> None:
        self.lock = threading.Lock()
        self.events: Deque[tuple[datetime, str, str, str, str]] = deque(maxlen=max_events)
        #                   ^ts        ^cat ^color ^worker ^msg
        self.start_time = datetime.now()
        # Aggregierte Counter pro Kategorie für die "Health"-Spalte
        self.counters: Dict[str, int] = {}
        # Facebook-Timer (live aus fb_watcher.safety_wait)
        self.fb_last_deal: str | None = None        # "HH:MM:SS"
        self.fb_next_secs: int | None = None        # Restsekunden bis nächster Post
        self.fb_last_update: datetime | None = None

    def add_event(self, record: logging.LogRecord) -> None:
        try:
            raw = record.getMessage()
        except Exception:
            raw = ""

        # 1) FB-Timer extrahieren (auch wenn Event sonst suppressed wird)
        m = _FB_TIMER_RE.search(raw)
        if m:
            with self.lock:
                self.fb_last_deal = m.group(1)
                self.fb_next_secs = int(m.group(2)) * 60 + int(m.group(3))
                self.fb_last_update = datetime.now()
            return  # nicht ins Event-Panel
        if _FB_BEEN_RE.search(raw):
            with self.lock:
                self.fb_next_secs = 0
                self.fb_last_update = datetime.now()
            # Pause beendet ist wichtig genug fürs Event-Panel
            with self.lock:
                self.events.append((datetime.now(), "FB", "bold green",
                                    record.name or "?", "🟢 Pause beendet – suche neuen Deal"))
                self.counters["FB"] = self.counters.get("FB", 0) + 1
            return

        # 2) Normale Klassifikation
        cls = _classify(record)
        if not cls:
            return
        cat, color = cls
        with self.lock:
            self.events.append((datetime.now(), cat, color, record.name or "?", raw))
            self.counters[cat] = self.counters.get(cat, 0) + 1


STATE = _State()


# ───────────────────────────────────────────────────────────────
# File-Handler (alles, ohne Filter — wie im normalen Server)
# ───────────────────────────────────────────────────────────────
_FILE_FORMATTER = logging.Formatter(
    fmt="%(asctime)s %(name)-18s %(levelname)-7s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


class _FileDispatcher:
    def __init__(self) -> None:
        self._handlers: Dict[str, logging.Handler] = {}
        self._current_day: str = ""

    def _today(self) -> str:
        return datetime.now().strftime("%Y-%m-%d")

    def _ensure(self) -> Path:
        today = self._today()
        if today != self._current_day:
            for h in self._handlers.values():
                try:
                    h.close()
                except Exception:
                    pass
            self._handlers.clear()
            self._current_day = today
        d = LOG_DIR / today
        d.mkdir(parents=True, exist_ok=True)
        return d

    def write(self, record: logging.LogRecord) -> None:
        day_dir = self._ensure()
        worker = (record.name or "unknown").replace("/", "_")
        h = self._handlers.get(worker)
        if h is None:
            h = logging.FileHandler(day_dir / f"{worker}.log", encoding="utf-8")
            h.setFormatter(_FILE_FORMATTER)
            self._handlers[worker] = h
        try:
            h.handle(record)
        except Exception as e:
            sys.stderr.write(f"[tui-server] file write failed: {e}\n")


FILES = _FileDispatcher()


# ───────────────────────────────────────────────────────────────
# TCP-Empfänger (Standard logging.SocketHandler-Protokoll)
# ───────────────────────────────────────────────────────────────
class _LogHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        while True:
            chunk = self.connection.recv(4)
            if len(chunk) < 4:
                break
            slen = struct.unpack(">L", chunk)[0]
            chunk = self.connection.recv(slen)
            while len(chunk) < slen:
                more = self.connection.recv(slen - len(chunk))
                if not more:
                    break
                chunk += more
            try:
                record = logging.makeLogRecord(pickle.loads(chunk))
            except Exception as e:
                sys.stderr.write(f"[tui-server] bad record: {e}\n")
                continue
            # 1) Datei (alles)
            FILES.write(record)
            # 2) TUI-State (nur wichtige Events)
            STATE.add_event(record)


class _LogServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


# ───────────────────────────────────────────────────────────────
# UI-Rendering
# ───────────────────────────────────────────────────────────────
_WORKER_STATE_COLOR = {
    "idle":      "green",
    "busy":      "yellow",
    "error":     "red",
    "stale":     "magenta",
    "stopped":   "white",
}

_DEAL_STATE_COLOR = {
    "queue":      "yellow",
    "processing": "cyan",
    "sent":       "green",
    "failed":     "red",
}


def _fmt_uptime(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m {s:02d}s"
    return f"{m:02d}m {s:02d}s"


def _render_header() -> Panel:
    now = datetime.now().strftime("%H:%M:%S")
    up = _fmt_uptime((datetime.now() - STATE.start_time).total_seconds())

    # Live-Countdown: Restzeit seit letztem fb_watcher-Tick weiter herunterzählen
    with STATE.lock:
        last = STATE.fb_last_deal
        secs = STATE.fb_next_secs
        upd  = STATE.fb_last_update
    if secs is not None and upd is not None:
        delta = int((datetime.now() - upd).total_seconds())
        secs = max(0, secs - delta)

    if secs is None:
        fb_part = Text("FB-Timer: — (warte auf fb_watcher)", style="dim")
    elif secs == 0:
        fb_part = Text("FB-Timer: 🟢 BEREIT", style="bold green")
    else:
        m, s = divmod(secs, 60)
        color = "green" if secs < 30 else ("yellow" if secs < 120 else "cyan")
        fb_part = Text(
            f"FB-Timer: ⏳ {m:02d}:{s:02d}  (letzter Deal {last or '—'})",
            style=f"bold {color}",
        )

    line1 = Text()
    line1.append("📊 SCRAPER LIVE CONSOLE   ", style="bold cyan")
    line1.append(f"{now}   ", style="white")
    line1.append("Uptime: ", style="dim")
    line1.append(up, style="green")

    body = Group(Align.center(line1), Align.center(fb_part))
    return Panel(body, border_style="cyan", height=4)


def _render_workers() -> Panel:
    try:
        from core.db import workers_repo
        rows = workers_repo.list_all()
    except Exception as e:
        return Panel(f"(workers_repo error: {e})", title="Workers", border_style="red")

    t = Table(show_header=True, header_style="bold cyan",
              expand=True, padding=(0, 1))
    t.add_column("Worker", style="white", no_wrap=True)
    t.add_column("State", justify="center", no_wrap=True)
    t.add_column("Aktuell", style="dim", overflow="ellipsis")
    t.add_column("Heartbeat", justify="right", style="dim", no_wrap=True)
    if not rows:
        t.add_row("(keine Worker registriert)", "", "", "")
    for w in rows:
        state = (w.get("state") or "?").lower()
        color = _WORKER_STATE_COLOR.get(state, "white")
        hb = w.get("last_heartbeat") or ""
        if hb:
            try:
                dt = datetime.fromisoformat(hb)
                # heartbeat ist UTC, lokale Zeit für Anzeige
                age = (datetime.utcnow() - dt).total_seconds()
                hb = f"{int(age)}s"
            except Exception:
                hb = "?"
        cur = (w.get("current_task") or "").strip()
        if len(cur) > 40:
            cur = cur[:37] + "…"
        t.add_row(
            w["name"],
            Text(state.upper(), style=color),
            cur,
            hb,
        )
    return Panel(t, title="👷 Workers", border_style="blue")


def _render_deals() -> Panel:
    try:
        from core.db import deals_repo
        counts = deals_repo.counts_by_status()
    except Exception as e:
        return Panel(f"(deals_repo error: {e})", title="Deals", border_style="red")

    t = Table.grid(expand=True, padding=(0, 2))
    t.add_column(style="white", no_wrap=True)
    t.add_column(justify="right")

    total = 0
    order = ["queue", "processing", "sent", "failed"]
    for st in order:
        n = counts.get(st, 0)
        total += n
        col = _DEAL_STATE_COLOR.get(st, "white")
        emoji = {"queue": "⏳", "processing": "⚙️ ", "sent": "✅", "failed": "❌"}[st]
        t.add_row(
            Text(f"{emoji} {st.capitalize():<11}", style=col),
            Text(str(n), style=f"bold {col}"),
        )
    t.add_row(Text("─" * 18, style="dim"), Text("─" * 6, style="dim"))
    t.add_row(Text("Σ Gesamt", style="bold"), Text(str(total), style="bold"))

    # Counter (was lief seit Start?)
    t.add_row("", "")
    t.add_row(Text("Events seit Start:", style="bold dim"), "")
    with STATE.lock:
        items = sorted(STATE.counters.items(), key=lambda x: -x[1])
    for cat, n in items[:6]:
        t.add_row(Text(f"  {cat}", style="dim"), Text(str(n), style="dim"))

    return Panel(t, title="📦 Deals", border_style="green")


def _render_events() -> Panel:
    t = Table(show_header=True, header_style="bold magenta",
              expand=True, padding=(0, 1))
    t.add_column("Zeit", style="dim", no_wrap=True, width=8)
    t.add_column("Cat", no_wrap=True, width=6)
    t.add_column("Worker", style="cyan", no_wrap=True, width=18)
    t.add_column("Meldung", overflow="ellipsis")
    with STATE.lock:
        events = list(STATE.events)
    if not events:
        t.add_row("--:--:--", "INIT", "tui_server",
                  "Warte auf Ereignisse … (Links, AI-Calls, FB/TG-Posts, Fehler)")
    for ts, cat, color, worker, msg in events:
        # Mehrzeiliges abkürzen — nur erste Zeile
        first = msg.splitlines()[0] if msg else ""
        if len(first) > 200:
            first = first[:197] + "…"
        t.add_row(
            ts.strftime("%H:%M:%S"),
            Text(cat, style=color),
            worker,
            first,
        )
    return Panel(t, title="📜 Letzte Events (wichtige)", border_style="magenta")


def _render_footer() -> Panel:
    return Panel(
        Align.center(Text(
            "Strg+C zum Beenden  |  Volle Logs: " + str(LOG_DIR)
            + "/<Datum>/<worker>.log  |  Dashboard: http://127.0.0.1:8000",
            style="dim",
        )),
        border_style="dim",
        height=3,
    )


def _build_layout() -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=4),
        Layout(name="middle", ratio=2),
        Layout(name="events", ratio=3),
        Layout(name="footer", size=3),
    )
    layout["middle"].split_row(
        Layout(name="workers", ratio=2),
        Layout(name="deals",   ratio=1),
    )
    return layout


def _refresh(layout: Layout) -> None:
    layout["header"].update(_render_header())
    layout["middle"]["workers"].update(_render_workers())
    layout["middle"]["deals"].update(_render_deals())
    layout["events"].update(_render_events())
    layout["footer"].update(_render_footer())


def main() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    # TCP-Server in eigenem Thread starten
    srv = _LogServer((LOG_HOST, LOG_PORT), _LogHandler)
    thr = threading.Thread(target=srv.serve_forever, name="log-tcp", daemon=True)
    thr.start()

    # rich-Live-Render
    console = Console()
    layout = _build_layout()
    refresh_rate = float(os.getenv("CORE_TUI_REFRESH_SECS", "1.0"))

    try:
        with Live(layout, console=console, refresh_per_second=1 / refresh_rate,
                  screen=True, redirect_stdout=False, redirect_stderr=False):
            while True:
                _refresh(layout)
                time.sleep(refresh_rate)
    except KeyboardInterrupt:
        pass
    finally:
        srv.shutdown()
        srv.server_close()


if __name__ == "__main__":
    main()
