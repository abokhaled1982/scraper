#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test/smoke_test.py — Smoke-Test fuer die Scraper-Pipeline.

Was wird geprueft?
  1. Import aller aktiven core/-Module (catches Import-Errors).
  2. core.paths.ensure_directories() laeuft fehlerfrei.
  3. core.db.init_db() legt die SQLite-Tabellen an.
  4. Logger-Server kann instanziiert werden (kurz starten/stoppen).
  5. run_all.py --mode parser startet alle Subprozesse, laeuft ~15s
     stabil (kein sofortiger Crash) und faehrt sauber via SIGTERM herunter.

Aufruf:
    python -m test.smoke_test
oder
    python test/smoke_test.py

Exit-Code 0 = OK, !=0 = Fehler.
"""
from __future__ import annotations

import asyncio
import importlib
import os
import signal
import sys
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ──────────────────────────────────────────────────────────────────────────
# Kleine Test-Utilities
# ──────────────────────────────────────────────────────────────────────────
class Result:
    def __init__(self) -> None:
        self.passed: list[str] = []
        self.failed: list[tuple[str, str]] = []

    def ok(self, name: str) -> None:
        self.passed.append(name)
        print(f"  ✅ {name}")

    def fail(self, name: str, err: str) -> None:
        self.failed.append((name, err))
        print(f"  ❌ {name}\n       {err.strip().splitlines()[0]}")

    def summary(self) -> int:
        total = len(self.passed) + len(self.failed)
        print("\n" + "=" * 60)
        print(f"Smoke-Test Ergebnis: {len(self.passed)}/{total} bestanden")
        if self.failed:
            print("\nFehlgeschlagen:")
            for name, err in self.failed:
                print(f"  • {name}")
                for line in err.strip().splitlines():
                    print(f"      {line}")
            return 1
        print("Alle Pruefungen erfolgreich.")
        return 0


def section(title: str) -> None:
    print(f"\n── {title} " + "─" * max(0, 56 - len(title)))


# ──────────────────────────────────────────────────────────────────────────
# 1) Import-Check: alle aktiven Worker- und Core-Module
# ──────────────────────────────────────────────────────────────────────────
ACTIVE_MODULES = [
    # Core-Infrastruktur
    "core",
    "core.config",
    "core.paths",
    "core.logging",
    "core.logging.client",
    "core.logging.server",
    "core.db",
    "core.db.engine",
    "core.db.models",
    "core.db.deals_repo",
    "core.db.state_repo",
    "core.db.workers_repo",
    "core.dashboard",
    "core.migrate",
    # Amazon-Pipeline (Subprozesse aus run_all.py)
    "core.workers.amazon.ws_server",
    "core.workers.amazon.watcher",
    "core.workers.amazon.product_opener",
    "core.workers.amazon.product_parser",
    "core.workers.amazon.parser_worker",
    "core.workers.amazon.amazon_parser",
    "core.workers.amazon.amzon_dealsList_parser",
    "core.workers.amazon.utils",
    # AI-Parser
    "core.workers.ai_parser.ai_extractor",
    # Facebook / Reels
    "core.workers.facebook.fb_watcher",
    "core.workers.facebook.fb_processor",
    "core.workers.facebook.fb_service",
    "core.workers.facebook.fb_message",
    "core.workers.facebook.reels_processor",
    "core.workers.facebook.reels_service",
    "core.workers.facebook.template_interface",
    # Instagram (nur die noch lebenden Cross-Post-Helper)
    "core.workers.instagram.ig_service",
    "core.workers.instagram.ig_message",
    # Telegram
    "core.workers.telegram.login_once",
    "core.workers.telegram.telRouter",
    "core.workers.telegram.telObserver",
    "core.workers.telegram.telObserver_piraten",
    "core.workers.telegram.telSender",
    "core.workers.telegram.offer_message",
    "core.workers.telegram.image_processor",
    "core.workers.telegram.tel_video_sender",
]


def test_imports(r: Result) -> None:
    section("1) Imports aller aktiven Module")
    for mod in ACTIVE_MODULES:
        try:
            importlib.import_module(mod)
            r.ok(f"import {mod}")
        except Exception:
            r.fail(f"import {mod}", traceback.format_exc())


# ──────────────────────────────────────────────────────────────────────────
# 2) Pfade anlegen
# ──────────────────────────────────────────────────────────────────────────
def test_paths(r: Result) -> None:
    section("2) core.paths.ensure_directories()")
    try:
        from core.paths import (
            IMAGES_DIR,
            INBOX_DIR,
            PRODUCKT_DIR,
            VIDEOS_QUEUE_DIR,
            VIDEOS_SENT_DIR,
            ensure_directories,
        )

        ensure_directories()
        for p in (IMAGES_DIR, VIDEOS_QUEUE_DIR, VIDEOS_SENT_DIR, INBOX_DIR, PRODUCKT_DIR):
            if not p.is_dir():
                raise AssertionError(f"Verzeichnis fehlt: {p}")
        r.ok("ensure_directories legt alle Pfade an")
    except Exception:
        r.fail("ensure_directories", traceback.format_exc())


# ──────────────────────────────────────────────────────────────────────────
# 3) DB initialisieren
# ──────────────────────────────────────────────────────────────────────────
def test_db_init(r: Result) -> None:
    section("3) core.db.init_db()")
    try:
        from sqlalchemy import inspect

        from core.db import ENGINE, init_db

        init_db()
        inspector = inspect(ENGINE)
        tables = set(inspector.get_table_names())
        if not tables:
            raise AssertionError("init_db hat keine Tabellen erzeugt")
        r.ok(f"DB initialisiert ({len(tables)} Tabellen: {sorted(tables)})")
    except Exception:
        r.fail("init_db", traceback.format_exc())


# ──────────────────────────────────────────────────────────────────────────
# 4) Logger-Server kurz starten und stoppen
# ──────────────────────────────────────────────────────────────────────────
async def _start_logger_briefly() -> None:
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "core.logging.server",
        env=env,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    await asyncio.sleep(1.0)
    if proc.returncode is not None:
        err = (await proc.stderr.read()).decode(errors="replace") if proc.stderr else ""
        raise RuntimeError(
            f"Logger-Server beendete sich sofort mit Code {proc.returncode}\n{err}"
        )
    proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()


def test_logger_server(r: Result) -> None:
    section("4) Logger-Server Start/Stop")
    try:
        asyncio.run(_start_logger_briefly())
        r.ok("core.logging.server startet und stoppt sauber")
    except Exception:
        r.fail("logger_server", traceback.format_exc())


# ──────────────────────────────────────────────────────────────────────────
# 5) run_all.py --mode parser (End-to-End Subprozess-Check)
# ──────────────────────────────────────────────────────────────────────────
PARSER_RUN_SECONDS = float(os.getenv("SMOKE_PARSER_SECS", "12"))


async def _run_supervisor() -> tuple[int, str]:
    """Startet run_all.py --mode parser, laesst es kurz laufen, sendet SIGTERM."""
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(ROOT / "run_all.py"),
        "--mode",
        "parser",
        cwd=str(ROOT),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    start = time.monotonic()
    output_chunks: list[bytes] = []

    async def drain() -> None:
        assert proc.stdout is not None
        while True:
            chunk = await proc.stdout.read(4096)
            if not chunk:
                return
            output_chunks.append(chunk)

    drain_task = asyncio.create_task(drain())

    # Laufzeit-Phase: warte PARSER_RUN_SECONDS oder bis Prozess vorzeitig endet
    try:
        await asyncio.wait_for(proc.wait(), timeout=PARSER_RUN_SECONDS)
        # Wenn er hier landet, ist er vorzeitig beendet → Fehler
        elapsed = time.monotonic() - start
        await asyncio.sleep(0.2)
        drain_task.cancel()
        out = b"".join(output_chunks).decode(errors="replace")
        raise RuntimeError(
            f"Supervisor beendete sich vorzeitig nach {elapsed:.1f}s "
            f"(Code {proc.returncode})\n--- Ausgabe ---\n{out[-4000:]}"
        )
    except asyncio.TimeoutError:
        pass  # gewollter Pfad: lebt noch

    # geordnetes Shutdown via SIGTERM
    proc.send_signal(signal.SIGTERM)
    try:
        rc = await asyncio.wait_for(proc.wait(), timeout=15)
    except asyncio.TimeoutError:
        proc.kill()
        rc = await proc.wait()
        drain_task.cancel()
        out = b"".join(output_chunks).decode(errors="replace")
        raise RuntimeError(
            f"Supervisor reagierte nicht auf SIGTERM (gekillt). "
            f"Code {rc}\n--- Ausgabe ---\n{out[-4000:]}"
        )

    drain_task.cancel()
    out = b"".join(output_chunks).decode(errors="replace")
    return rc, out


def test_supervisor(r: Result) -> None:
    section(f"5) run_all.py --mode parser (~{PARSER_RUN_SECONDS:.0f}s)")
    try:
        rc, out = asyncio.run(_run_supervisor())

        # Erwartete Marker im Log
        required_markers = [
            "[supervisor] Modus: parser",
            "started ws_server",
            "started deals_watcher",
            "started product_opener",
            "started product_parser",
            "started logger",
            "started dashboard",
        ]
        missing = [m for m in required_markers if m not in out]
        if missing:
            tail = out[-3000:]
            raise AssertionError(
                f"Fehlende Log-Marker: {missing}\n--- Ausgabe (Tail) ---\n{tail}"
            )

        # Exit-Code: 0 (sauberes Shutdown) oder -SIGTERM tolerieren
        if rc not in (0, -signal.SIGTERM, 143):
            tail = out[-3000:]
            raise AssertionError(
                f"Unerwarteter Exit-Code {rc}\n--- Ausgabe (Tail) ---\n{tail}"
            )

        r.ok(
            f"Supervisor lief stabil, alle Worker gestartet, sauber beendet "
            f"(rc={rc})"
        )
    except Exception:
        r.fail("supervisor parser-mode", traceback.format_exc())


# ──────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────
def main() -> int:
    print("Smoke-Test fuer Scraper-Pipeline")
    print(f"Projekt-Root: {ROOT}")
    r = Result()
    test_imports(r)
    test_paths(r)
    test_db_init(r)
    test_logger_server(r)
    test_supervisor(r)
    return r.summary()


if __name__ == "__main__":
    sys.exit(main())
