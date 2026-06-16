#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_all.py – Supervisor

Modi (--mode):
  full    (Standard) – alles: Amazon-Pipeline + Facebook + Telegram
  parser             – nur Amazon-Pipeline (ws_server, watcher, opener, parser)
                       kein Telegram-Login, kein Facebook

Beispiele:
  python run_all.py                  # full
  python run_all.py --mode parser    # nur AI-Parser-Stack
"""

from __future__ import annotations
import argparse
import os
import socket
import sys
import asyncio
import signal
import time
from pathlib import Path
from typing import Optional, Dict, List, Tuple
from dotenv import load_dotenv

# ----------------------------------------------------------
# Args frühzeitig parsen (vor bedingten Imports)
# ----------------------------------------------------------
_arg_parser = argparse.ArgumentParser(add_help=True)
_arg_parser.add_argument(
    "--mode",
    choices=["full", "parser"],
    default="full",
    help="'full' startet alles; 'parser' startet nur den Amazon-Pipeline-Stack.",
)
_arg_parser.add_argument(
    "--tui",
    action="store_true",
    help="Zeigt eine kompakte Live-Konsole (statisches Layout) statt scrollender Logs.",
)
_arg_parser.add_argument(
    "--setup-profiles",
    nargs="?",
    const="all",
    choices=["all", "facebook", "amazon"],
    default=None,
    help="Einmaliger Profil-Login: \u00f6ffnet Chrome mit Worker-Profil + Addon, "
         "damit du dich einloggen kannst. Beendet sich danach.",
)
_arg_parser.add_argument(
    "--force",
    action="store_true",
    help="Killt automatisch alte Supervisor-/Worker-Prozesse, statt mit "
         "Fehler abzubrechen. Nützlich, wenn ein voriger Run ungeordnet "
         "beendet wurde und noch .session-SQLite-Locks halt.",
)
_arg_parser.add_argument(
    "--no-restart",
    action="store_true",
    help="Deaktiviert Auto-Restart abgestürzter Worker. Standard: Worker werden "
         "bei Crash neugestartet, damit z. B. ein Telegram-Hänger nicht den "
         "laufenden Facebook-Reel-Upload abbricht.",
)
ARGS = _arg_parser.parse_args()
MODE = ARGS.mode
USE_TUI = ARGS.tui
SETUP_TARGET = ARGS.setup_profiles
FORCE = ARGS.force
AUTO_RESTART = not ARGS.no_restart

# ----------------------------------------------------------
# Initial Setup & Pfade
# ----------------------------------------------------------
HERE = Path(__file__).parent.resolve()
PY = sys.executable  # Aktuelles venv-Python

# Projekt-Root in sys.path aufnehmen
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
from core.logging import get_logger  # noqa: E402
log = get_logger("run_all")  # noqa: E402

# ----------------------------------------------------------
# .env laden
# ----------------------------------------------------------
load_dotenv()

SESSION_DIR = os.getenv("SESSION_DIR", ".sessions")

# Telegram-Konfiguration + Import nur im full-Modus
if MODE == "full" and not SETUP_TARGET:
    try:
        from core.workers.telegram.login_once import LoginConfig, ensure_both_sessions_sequential
    except ImportError:
        log.error("❌ Fehler: login_once.py konnte nicht gefunden werden.")
        sys.exit(1)

    API_ID           = int(os.getenv("API_ID", "0"))
    API_HASH         = os.getenv("API_HASH", "")
    PHONE            = os.getenv("TELEGRAM_PHONE")
    PASSWORD         = os.getenv("TELEGRAM_PASSWORD")
    ROUTER_NAME      = os.getenv("SESSION_NAME", "main_session")
    OBSERVER_NAME    = os.getenv("OBS_SESSION_NAME", "observer_session")
    SENDER_NAME      = os.getenv("OBS_SEND_OBSERVER_NAME", "observer_sender_session")
    ROUTER_CHANNEL   = os.getenv("CHANNEL_INVITE_URL", "").strip()
    OBSERVER_CHANNEL = os.getenv("OBS_CHANNEL_INVITE_URL", "").strip()
    PIRATEN_NAME     = os.getenv("PIRATEN_SESSION_NAME", "piraten_session")
    PIRATEN_CHANNEL  = os.getenv("PIRATEN_CHANNEL_INVITE_URL", "").strip()

    if not all([API_ID, API_HASH, ROUTER_CHANNEL, OBSERVER_CHANNEL]):
        raise SystemExit("Fehler: Mindestens eine Telegram-Variable (API_ID, HASH, CHANNEL_INVITE_URL, OBS_CHANNEL_INVITE_URL) fehlt in .env.")

# ----------------------------------------------------------
# Supervisor Utilities
# ----------------------------------------------------------
def _ensure_dirs():
    from core.paths import ensure_directories
    ensure_directories()
    (HERE / SESSION_DIR).mkdir(parents=True, exist_ok=True)

# ----------------------------------------------------------
# Parent-Death-Signal (Linux): sorgt dafür, dass jeder Worker
# automatisch SIGTERM bekommt, sobald der Supervisor stirbt –
# auch bei kill -9, Terminal-Close ohne SIGHUP-Handling oder Crash.
# So bleibt NIE ein verwaister Worker übrig.
# ----------------------------------------------------------
def _install_parent_death_signal() -> None:
    """Wird im Subprozess via preexec_fn aufgerufen (Linux only)."""
    if sys.platform != "linux":
        return
    try:
        import ctypes
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        PR_SET_PDEATHSIG = 1
        # Wenn parent (Supervisor) stirbt → Kernel schickt uns SIGTERM
        libc.prctl(PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0)
    except Exception:
        # Best effort — wenn prctl nicht verfügbar ist, fallen wir
        # auf den normalen SIGHUP/SIGTERM-Pfad des Supervisors zurück.
        pass


async def spawn(name: str, *argv: str, env: Optional[Dict[str, str]] = None):
    log.info(f"[supervisor] spawn {name}: {' '.join(argv)}")
    # Stelle sicher, dass Worker-Subprozesse das Projekt-Root in PYTHONPATH haben,
    # damit `from core.logging import get_logger` immer funktioniert –
    # auch wenn der Worker direkt mit absolutem Pfad gestartet wird.
    existing_pp = os.environ.get("PYTHONPATH", "")
    pythonpath = str(HERE) + (os.pathsep + existing_pp if existing_pp else "")
    full_env = {**os.environ, "PYTHONPATH": pythonpath, **(env or {})}
    proc = await asyncio.create_subprocess_exec(
        *argv,
        env=full_env,
        preexec_fn=_install_parent_death_signal if sys.platform == "linux" else None,
    )
    # Merke argv + env am Prozess-Objekt, damit der Supervisor den Worker
    # bei einem Absturz mit identischen Parametern neu starten kann
    # (siehe _supervise_with_restart()).
    setattr(proc, "_spawn_argv", argv)
    setattr(proc, "_spawn_env", env)
    setattr(proc, "_spawn_name", name)
    return proc

# ----------------------------------------------------------
# Pre-Flight: alte Supervisor-/Worker-Prozesse erkennen
# ----------------------------------------------------------
# Diese Worker-Module identifizieren wir als „uns gehörend". Tauchen sie
# in der Prozessliste auf, obwohl wir gerade frisch starten, ist es ein
# Geisterprozess aus einem vorherigen, nicht sauber beendeten Run.
_WORKER_MODULE_KEYWORDS = (
    "core.workers.amazon.ws_server",
    "core.workers.amazon.watcher",
    "core.workers.amazon.product_opener",
    "core.workers.amazon.product_parser",
    "core.workers.facebook.fb_watcher",
    "core.workers.telegram.telRouter",
    "core.workers.telegram.telObserver",
    "core.workers.telegram.telObserver_piraten",
    "core.workers.telegram.telSender",
    "core.dashboard",
    "core.logging.server",
    "core.logging.tui_server",
)


def _scan_stale_processes() -> List[Tuple[int, str]]:
    """Liefert (pid, cmdline) aller verdächtigen Geisterprozesse — ohne uns selbst."""
    import subprocess as _sp
    me = os.getpid()
    stale: List[Tuple[int, str]] = []
    try:
        out = _sp.check_output(["ps", "-eo", "pid=,cmd="], text=True, errors="ignore")
    except Exception as e:
        log.warning(f"[supervisor] preflight: ps fehlgeschlagen: {e}")
        return stale
    # Auch eine andere run_all.py-Instanz ist „stale" aus unserer Sicht.
    extra = (str(HERE / "run_all.py"),)
    keywords = _WORKER_MODULE_KEYWORDS + extra
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            pid_str, _, cmd = line.partition(" ")
            pid = int(pid_str)
        except Exception:
            continue
        if pid == me:
            continue
        if not any(k in cmd for k in keywords):
            continue
        # Nur Prozesse mit unserem venv-Python — andere Python-Prozesse
        # ignorieren wir, sonst killen wir uns u.U. fremde Sachen.
        if str(PY) not in cmd and ".venv/bin/python" not in cmd:
            continue
        stale.append((pid, cmd))
    return stale


def _kill_processes(procs: List[Tuple[int, str]]) -> None:
    """Beendet PIDs zuerst per SIGTERM, dann (nach 3s) per SIGKILL."""
    import time as _time
    for pid, cmd in procs:
        try:
            os.kill(pid, signal.SIGTERM)
            log.warning(f"[supervisor] preflight: SIGTERM → {pid}  ({cmd[:80]})")
        except ProcessLookupError:
            pass
        except Exception as e:
            log.error(f"[supervisor] preflight: kill {pid} failed: {e}")
    _time.sleep(3)
    for pid, cmd in procs:
        try:
            os.kill(pid, 0)  # noch da?
            os.kill(pid, signal.SIGKILL)
            log.warning(f"[supervisor] preflight: SIGKILL → {pid}  ({cmd[:80]})")
        except ProcessLookupError:
            pass
        except Exception:
            pass


def preflight_kill_stale() -> None:
    """Vor dem Start checken, ob alte Scraper-Prozesse noch laufen.

    - Ohne --force: klare Meldung + Liste + Exit (so kann der User entscheiden).
    - Mit --force: alte Prozesse werden geterminated.
    Verhindert das „database is locked"-Problem auf den .session-Dateien.
    """
    stale = _scan_stale_processes()
    if not stale:
        return
    log.error("─" * 60)
    log.error(f"❌ Es laufen noch {len(stale)} alte(r) Scraper-Prozess(e):")
    for pid, cmd in stale:
        log.error(f"   PID {pid:>7}  {cmd[:100]}")
    log.error("─" * 60)
    if FORCE:
        log.warning("[supervisor] --force aktiv → killing …")
        _kill_processes(stale)
        # Kurze Restprüfung
        leftover = _scan_stale_processes()
        if leftover:
            log.error(f"❌ {len(leftover)} Prozess(e) liessen sich nicht beenden. Abbruch.")
            sys.exit(2)
        log.info("✅ Alte Prozesse beendet — Start läuft weiter.")
        return
    log.error("Diese halten die Telegram-.session-Dateien (SQLite) gesperrt.")
    log.error("Lösung A:  python run_all.py --force      (killt sie automatisch)")
    log.error("Lösung B:  manuell:  kill -TERM <PID>     (dann run_all neu starten)")
    sys.exit(2)


# ----------------------------------------------------------
# Port-Helpers: belegte Ports automatisch hochzählen
# ----------------------------------------------------------
def _port_in_use(host: str, port: int) -> bool:
    """True wenn (host, port) bereits gebunden ist (TCP)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        try:
            s.bind((host, port))
        except OSError:
            return True
    return False


def _find_free_port(host: str, preferred: int, *, max_tries: int = 50) -> int:
    """Liefert den ersten freien Port ab `preferred` (inkl.). Springt nach oben."""
    for offset in range(max_tries):
        candidate = preferred + offset
        if candidate > 65535:
            break
        if not _port_in_use(host, candidate):
            return candidate
    # Fallback: OS-vergebener Port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


def _resolve_service_ports() -> Tuple[str, int, str, int, str, int]:
    """Sorgt dafür, dass Logger-, Dashboard- und WS-Port frei sind.

    Liest die Default-Hosts/Ports aus core.config (inkl. evtl. gesetzter
    Env-Vars), prüft alle drei Ports und springt bei Belegung automatisch
    auf den nächsten freien Port. Setzt die finalen Werte als
    CORE_LOG_PORT / CORE_DASHBOARD_PORT / CORE_WS_PORT in os.environ,
    damit alle Subprozesse (Logger, Dashboard, TUI, Worker, ws_server) sie sehen.
    """
    from core.config import (
        LOG_HOST, LOG_PORT,
        DASHBOARD_HOST, DASHBOARD_PORT,
        WS_HOST, WS_PORT,
    )

    log_port = _find_free_port(LOG_HOST, LOG_PORT)
    if log_port != LOG_PORT:
        log.warning(
            f"[supervisor] LOG_PORT {LOG_PORT} belegt → nutze {log_port}"
        )
    dash_port = _find_free_port(DASHBOARD_HOST, DASHBOARD_PORT)
    if dash_port != DASHBOARD_PORT:
        log.warning(
            f"[supervisor] DASHBOARD_PORT {DASHBOARD_PORT} belegt → nutze {dash_port}"
        )
    ws_port = _find_free_port(WS_HOST, WS_PORT)
    if ws_port != WS_PORT:
        # Die Browser-Addons (addons/proudct_parser, addons/mydealz) probieren
        # beim Reconnect die Port-Liste 8765..8768 durch und finden den neuen
        # Port automatisch. Nur ausserhalb dieses Bereichs ist Handarbeit nötig.
        msg = f"[supervisor] WS_PORT {WS_PORT} belegt → nutze {ws_port}"
        if not (8765 <= ws_port <= 8768):
            msg += "  ⚠ ausserhalb 8765..8768 — Addon muss angepasst werden!"
        log.warning(msg)

    # Für ALLE Subprozesse + spätere Config-Reloads sichtbar machen
    os.environ["CORE_LOG_HOST"] = LOG_HOST
    os.environ["CORE_LOG_PORT"] = str(log_port)
    os.environ["CORE_DASHBOARD_HOST"] = DASHBOARD_HOST
    os.environ["CORE_DASHBOARD_PORT"] = str(dash_port)
    os.environ["CORE_WS_HOST"] = WS_HOST
    os.environ["CORE_WS_PORT"] = str(ws_port)
    return LOG_HOST, log_port, DASHBOARD_HOST, dash_port, WS_HOST, ws_port


def _print_service_banner(log_host: str, log_port: int,
                          dash_host: str, dash_port: int,
                          ws_host: str, ws_port: int) -> None:
    """Zeigt die finalen Service-Ports gross & farbig auf der Konsole."""
    cyan = "\033[36m"
    bold = "\033[1m"
    green = "\033[32m"
    yellow = "\033[33m"
    red = "\033[31m"
    reset = "\033[0m"
    bar = "─" * 60
    ws_color = green if 8765 <= ws_port <= 8768 else red
    ws_note = "" if 8765 <= ws_port <= 8768 else f" {red}(ausserhalb 8765..8768 — Addon anpassen!){reset}"
    lines = [
        f"{cyan}{bar}{reset}",
        f"{bold}{cyan}  📡  SCRAPER SERVICES{reset}",
        f"{cyan}{bar}{reset}",
        f"  {bold}Logger    {reset}: {green}tcp://{log_host}:{log_port}{reset}",
        f"  {bold}Dashboard {reset}: {green}http://{dash_host}:{dash_port}{reset}",
        f"  {bold}WS-Server {reset}: {ws_color}ws://{ws_host}:{ws_port}{reset}{ws_note}",
        f"  {bold}Modus     {reset}: {yellow}{MODE}{'  (TUI)' if USE_TUI else ''}{reset}",
        f"{cyan}{bar}{reset}",
    ]
    print("\n".join(lines), flush=True)
    # Auch ins Logger-File-Log schreiben (sobald Logger steht)
    log.info(f"[supervisor] Logger    → tcp://{log_host}:{log_port}")
    log.info(f"[supervisor] Dashboard → http://{dash_host}:{dash_port}")
    log.info(f"[supervisor] WS-Server → ws://{ws_host}:{ws_port}")


async def terminate(proc: asyncio.subprocess.Process | None, name: str, timeout: float = 5.0):
    if not proc or proc.returncode is not None:
        return
    log.info(f"[supervisor] terminate {name}")
    try:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            log.info(f"[supervisor] kill {name}")
            proc.kill()
            await asyncio.wait_for(proc.wait(), timeout=timeout)
    except ProcessLookupError:
        pass


# ----------------------------------------------------------
# Core-Services (Logger + Dashboard + DB-Init)
# ----------------------------------------------------------
async def _start_core_services() -> List[Tuple[str, asyncio.subprocess.Process]]:
    """
    Startet zuerst Logger-Server, dann Dashboard. Initialisiert die DB.
    Liefert die Liste der gestarteten Prozesse, damit der Supervisor sie
    beim Shutdown ebenfalls beendet.
    """
    # 0) Ports prüfen / hochzählen + Banner mit den FINALEN Ports zeigen.
    log_host, log_port, dash_host, dash_port, ws_host, ws_port = _resolve_service_ports()
    _print_service_banner(log_host, log_port, dash_host, dash_port, ws_host, ws_port)

    # DB einmalig initialisieren (Tabellen anlegen, falls noch nicht vorhanden)
    try:
        from core.db import init_db
        init_db()
        log.info("[supervisor] core.db initialized")
    except Exception as e:
        log.error(f"[supervisor] WARN: core.db init failed: {e}")

    logger_module = "core.logging.tui_server" if USE_TUI else "core.logging.server"
    logger_proc = await spawn("logger", PY, "-m", logger_module)
    # kurz warten, damit der TCP-Port wirklich offen ist, bevor Worker starten
    await asyncio.sleep(0.5)
    dash_proc = await spawn("dashboard", PY, "-m", "core.dashboard")

    return [("logger", logger_proc), ("dashboard", dash_proc)]

# ----------------------------------------------------------
# Sequentieller Telegram Login (nur full-Modus)
# ----------------------------------------------------------
def print_login_step(msg: str):
    log.info(f"[Telegram Login] {msg}")

async def do_telegram_login_check():
    log.info("\n--- Starte sequentiellen Telegram-Login-Check (4 Sessions) ---")

    router_cfg   = LoginConfig(API_ID, API_HASH, ROUTER_NAME,   SESSION_DIR, PHONE, PASSWORD)
    observer_cfg = LoginConfig(API_ID, API_HASH, OBSERVER_NAME, SESSION_DIR, PHONE, PASSWORD)
    sender_cfg   = LoginConfig(API_ID, API_HASH, SENDER_NAME,   SESSION_DIR, PHONE, PASSWORD)
    piraten_cfg  = LoginConfig(API_ID, API_HASH, PIRATEN_NAME,  SESSION_DIR, PHONE, PASSWORD)

    ok1, ok2, ok3, ok4 = await ensure_both_sessions_sequential(
        router_cfg, observer_cfg, sender_cfg, piraten_cfg, on_step=print_login_step
    )

    if not (ok1 and ok2 and ok3 and ok4):
        raise SystemExit("❌ Einer der 4 Telegram-Logins fehlgeschlagen. Abbruch.")

    log.info("✅ Alle Telegram-Sessions (Router, Obs, Sender, Piraten) bereit.")
    log.info("---------------------------------------------------\n")

# ----------------------------------------------------------
# Main Supervisor
# ----------------------------------------------------------
def setup_profiles(target: str) -> int:
    """Einmaliger interaktiver Profil-Login (Facebook/Amazon).

    \u00d6ffnet Chrome mit dem jeweiligen Worker-Profil + geladener Extension,
    damit der Nutzer sich einloggen kann. Sessions bleiben anschlie\u00dfend
    persistent im Profil-Ordner.
    """
    import subprocess
    from core.workers.chrome_launcher import (
        ChromeProfile,
        _resolve_chrome_bin,
        _resolve_addon_paths,
    )

    targets = {
        "facebook": (
            os.environ.get("FACEBOOK_CHROME_PROFILE", "facebook"),
            os.environ.get("FACEBOOK_ADDON_DIR", "addons/facebook"),
            os.environ.get("FACEBOOK_START_URL", "https://www.facebook.com/"),
        ),
        "amazon": (
            os.environ.get("AMAZON_CHROME_PROFILE", "amazon"),
            os.environ.get("AMAZON_ADDON_DIR", "addons/proudct_parser"),
            os.environ.get("AMAZON_START_URL", "https://www.amazon.de/"),
        ),
    }
    order = ["facebook", "amazon"] if target == "all" else [target]

    try:
        chrome_bin = _resolve_chrome_bin()
    except FileNotFoundError as e:
        log.error(str(e))
        return 1

    for key in order:
        name, addon_rel, url = targets[key]
        prof = ChromeProfile(name, addons=[addon_rel])
        addon_abs = _resolve_addon_paths([addon_rel])
        if not addon_abs:
            log.error(f"[setup] Addon nicht gefunden: {addon_rel}")
            return 2

        log.info("\n" + "=" * 60)
        log.info(f"\u25b6  Setup f\u00fcr Profil: {name}")
        log.info(f"   Profile-Dir : {prof.user_data_dir}")
        log.info(f"   Extension   : {addon_abs[0]}")
        log.info(f"   URL         : {url}")
        log.info("=" * 60)
        log.info("\u23f3  Bitte einloggen / Berechtigungen erteilen,")
        log.info("   danach das Fenster SCHLIESSEN, um zum n\u00e4chsten Schritt zu kommen.\n")

        cmd = [
            chrome_bin,
            f"--user-data-dir={prof.user_data_dir}",
            f"--load-extension={addon_abs[0]}",
            "--no-first-run",
            "--no-default-browser-check",
            url,
        ]
        try:
            subprocess.run(cmd, check=False)
        except KeyboardInterrupt:
            log.info("[setup] Abgebrochen.")
            return 130

    log.info("\n\u2705 Setup abgeschlossen. Du kannst jetzt 'python run_all.py' starten.")
    return 0


async def main():
    os.chdir(HERE)
    _ensure_dirs()
    # Pre-Flight: alte Supervisor-/Worker-Prozesse abfangen, damit wir
    # nicht in „sqlite3.OperationalError: database is locked" beim
    # Telegram-Login laufen.
    preflight_kill_stale()

    if MODE == "parser":
        log.info("[supervisor] Modus: parser — nur Amazon-Pipeline (kein Telegram, kein Facebook)")
        await _run_parser_only()
    else:
        log.info("[supervisor] Modus: full — alle Services")
        await _run_full()


async def _run_parser_only():
    """Startet nur den Amazon-Pipeline-Stack."""
    # 0. Core: Logger + Dashboard (Foundation)
    procs: List[Tuple[str, asyncio.subprocess.Process]] = []
    procs += await _start_core_services()

    ws_server      = await spawn("ws_server",      PY, "-m", "core.workers.amazon.ws_server")
    deals_watcher  = await spawn("deals_watcher",  PY, "-m", "core.workers.amazon.watcher")
    product_opener = await spawn("product_opener", PY, "-m", "core.workers.amazon.product_opener")
    product_parser = await spawn("product_parser", PY, "-m", "core.workers.amazon.product_parser")

    procs += [
        ("ws_server",      ws_server),
        ("deals_watcher",  deals_watcher),
        ("product_opener", product_opener),
        ("product_parser", product_parser),
    ]
    for n, p in procs:
        log.info(f"[supervisor] started {n} (pid={p.pid})")

    await _wait_and_shutdown(procs)


async def _run_full():
    """Startet alle Services inkl. Telegram-Login, Facebook und Telegram-Clients."""
    # 0. Core: Logger + Dashboard (Foundation) – muss als erstes laufen
    procs: List[Tuple[str, asyncio.subprocess.Process]] = []
    procs += await _start_core_services()

    # 1. Telegram Login
    await do_telegram_login_check()

    # 2. Instagram deaktiviert
    ig_session_ok = False  # Instagram ist deaktiviert

    # 3. Alle Services starten
    ws_server      = await spawn("ws_server",      PY, "-m", "core.workers.amazon.ws_server")
    deals_watcher  = await spawn("deals_watcher",  PY, "-m", "core.workers.amazon.watcher")
    product_opener = await spawn("product_opener", PY, "-m", "core.workers.amazon.product_opener")
    product_parser = await spawn("product_parser", PY, "-m", "core.workers.amazon.product_parser")
    fb_watcher     = await spawn("fb_watcher",     PY, "-m", "core.workers.facebook.fb_watcher")
    ig_watcher     = None  # Instagram deaktiviert
    tel_router     = await spawn("telegram_router",   PY, "-m", "core.workers.telegram.telRouter")
    tel_observer   = await spawn("telegram_observer", PY, "-m", "core.workers.telegram.telObserver")
    tel_sender     = await spawn("telegram_sender",   PY, "-m", "core.workers.telegram.telSender")
    # Piraten-Observer IMMER starten — der Prozess prüft den Dashboard-Toggle
    # (piraten.enabled) selbst und ignoriert eingehende Nachrichten im OFF-Zustand.
    # So bleibt die Telethon-Session offen und der Toggle wirkt sofort, ohne dass
    # ein Neustart des Supervisors nötig wäre.
    tel_piraten = await spawn("telegram_piraten", PY, "-m", "core.workers.telegram.telObserver_piraten")

    procs += [
        ("ws_server",          ws_server),
        ("deals_watcher",      deals_watcher),
        ("product_opener",     product_opener),
        ("product_parser",     product_parser),
        ("fb_watcher",         fb_watcher),
        *([("ig_watcher", ig_watcher)] if ig_watcher else []),
        ("telegram_router",    tel_router),
        ("telegram_observer",  tel_observer),
        ("telegram_sender",    tel_sender),
        *([("telegram_piraten", tel_piraten)] if tel_piraten else []),
    ]
    for n, p in procs:
        log.info(f"[supervisor] started {n} (pid={p.pid})")

    await _wait_and_shutdown(procs)


async def _wait_and_shutdown(procs: List[Tuple[str, asyncio.subprocess.Process]]):
    """Wartet auf Signal (SIGINT/SIGTERM) und hält Worker am Leben.

    Wenn AUTO_RESTART aktiv ist (Standard), startet diese Routine abgestürzte
    Worker automatisch neu — so reißt z. B. ein crashender Telegram-Observer
    nicht den `fb_watcher` mitten im Reel-Upload mit. Ein sauberes Beenden
    aller Worker passiert nur bei SIGINT/SIGTERM.

    Mit --no-restart fällt sie auf das alte Verhalten zurück: sobald ein
    Worker exitet, werden alle anderen mit-getötet.
    """
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    # SIGHUP zusätzlich abfangen: schließt der User das Terminal,
    # schickt das System SIGHUP – ohne Handler würde der Supervisor
    # sofort sterben und Worker als verwaiste Prozesse weiterlaufen.
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, ValueError):
            # SIGHUP gibt's z. B. unter Windows nicht – einfach überspringen.
            pass

    if not AUTO_RESTART:
        await _wait_first_exit_then_kill_all(procs, stop_event)
        return

    await _supervise_with_restart(procs, stop_event)


async def _wait_first_exit_then_kill_all(
    procs: List[Tuple[str, asyncio.subprocess.Process]],
    stop_event: asyncio.Event,
) -> None:
    """Altverhalten (vor Auto-Restart): erstes Exit → alle anderen killen."""

    async def wait_any():
        tasks = [asyncio.create_task(p.wait()) for _, p in procs]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finished_task = next(iter(done))
        idx = tasks.index(finished_task)
        name, proc = procs[idx]
        return name, proc.returncode

    w_task = asyncio.create_task(wait_any())
    s_task = asyncio.create_task(stop_event.wait())

    done, _ = await asyncio.wait({w_task, s_task}, return_when=asyncio.FIRST_COMPLETED)
    if w_task in done:
        name, code = await w_task
        log.info(f"[supervisor] process {name} exited with code {code}; stopping others …")
    else:
        log.info("[supervisor] stop requested; shutting down …")

    for name, proc in reversed(procs):
        await terminate(proc, name)
    log.info("[supervisor] all stopped")


# Maximale Restarts je Worker innerhalb des Beobachtungsfensters –
# verhindert Endlosschleifen bei einem dauerhaft kaputten Worker.
_RESTART_WINDOW_SECS = 60
_RESTART_MAX_IN_WINDOW = 5
# Liste der Worker, deren cleaner Exit (Code 0) als „Job erledigt" gilt –
# diese werden NICHT neu gestartet. Alle anderen Worker sind Daemons.
_ONESHOT_WORKERS: set[str] = set()


async def _supervise_with_restart(
    procs: List[Tuple[str, asyncio.subprocess.Process]],
    stop_event: asyncio.Event,
) -> None:
    """Hält Worker am Leben: bei Crash neu starten, bei Signal alle stoppen.

    `procs` wird in-place aktualisiert, damit das endgültige Shutdown immer
    die aktuellen Prozess-Handles trifft (auch nach mehreren Restarts).
    """
    # name → Anzahl Restarts im aktuellen Beobachtungsfenster + Startzeitpunkt
    restart_counts: Dict[str, List[float]] = {}

    # Map name → Index in `procs`, damit wir nach Restart das Tupel ersetzen können.
    def _index_of(name: str) -> int:
        for i, (n, _) in enumerate(procs):
            if n == name:
                return i
        return -1

    async def watch(name: str, proc: asyncio.subprocess.Process):
        """Wartet bis dieser eine Prozess endet. Triggert dann den Watcher-Loop."""
        await proc.wait()
        return name, proc.returncode

    # Pro Worker einen Watch-Task. Diese werden bei Restart neu erzeugt.
    watch_tasks: Dict[str, asyncio.Task] = {
        name: asyncio.create_task(watch(name, proc)) for name, proc in procs
    }
    stop_task = asyncio.create_task(stop_event.wait())

    log.info(f"[supervisor] auto-restart aktiv ({len(watch_tasks)} worker(s) überwacht)")

    while True:
        pending = set(watch_tasks.values()) | {stop_task}
        done, _ = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)

        if stop_task in done:
            log.info("[supervisor] stop requested; shutting down …")
            break

        # Mindestens ein Worker hat sich beendet → ggf. neu starten.
        for finished in done:
            if finished is stop_task:
                continue
            try:
                name, code = finished.result()
            except Exception as e:
                log.error(f"[supervisor] watch-task Fehler: {e}")
                continue

            # Aus dem Dict entfernen, damit wir den Eintrag ersetzen können.
            watch_tasks.pop(name, None)

            idx = _index_of(name)
            if idx < 0:
                log.warning(f"[supervisor] {name} exited (code={code}) – nicht im procs-Index gefunden, kein Restart")
                continue

            old_proc = procs[idx][1]
            argv = getattr(old_proc, "_spawn_argv", None)
            spawn_env = getattr(old_proc, "_spawn_env", None)

            # One-Shot Worker (z. B. Setup-Skripte) nicht respawnen, wenn sauber raus.
            if name in _ONESHOT_WORKERS and code == 0:
                log.info(f"[supervisor] {name} sauber beendet (code=0) – kein Restart (one-shot)")
                continue

            if argv is None:
                log.warning(
                    f"[supervisor] {name} exited (code={code}) – keine spawn-Args bekannt, kein Restart"
                )
                continue

            # Restart-Throttling: max N Restarts pro 60s. Sonst geben wir auf,
            # damit ein dauerhaft kaputter Worker nicht ewig in der Schleife läuft.
            now = time.monotonic()
            hist = restart_counts.setdefault(name, [])
            hist[:] = [t for t in hist if now - t < _RESTART_WINDOW_SECS]
            if len(hist) >= _RESTART_MAX_IN_WINDOW:
                log.error(
                    f"[supervisor] {name} >= {_RESTART_MAX_IN_WINDOW} Restarts in "
                    f"{_RESTART_WINDOW_SECS}s — gebe diesen Worker auf."
                )
                # Aus procs entfernen, damit Shutdown ihn nicht doppelt zu killen versucht.
                procs.pop(idx)
                continue

            backoff = min(2 ** len(hist), 30)  # 1,2,4,8,16,30…
            level = "warning" if code in (0, None) else "error"
            getattr(log, level)(
                f"[supervisor] {name} exited (code={code}) – Restart #{len(hist)+1} in {backoff}s"
            )
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=backoff)
                # stop_event ist gesetzt → raus aus der Restart-Schleife
                break
            except asyncio.TimeoutError:
                pass

            try:
                new_proc = await spawn(name, *argv, env=spawn_env)
            except Exception as e:
                log.error(f"[supervisor] Restart von {name} fehlgeschlagen: {e}")
                continue

            hist.append(time.monotonic())
            procs[idx] = (name, new_proc)
            watch_tasks[name] = asyncio.create_task(watch(name, new_proc))
            log.info(f"[supervisor] {name} neu gestartet (pid={new_proc.pid})")

        # Wenn stop_event innerhalb des Backoffs gesetzt wurde, raus.
        if stop_event.is_set():
            log.info("[supervisor] stop requested während Restart-Backoff …")
            break

    # Sauberes Shutdown aller noch laufenden Worker.
    for t in watch_tasks.values():
        t.cancel()
    for name, proc in reversed(procs):
        await terminate(proc, name)
    log.info("[supervisor] all stopped")

if __name__ == "__main__":
    # Setup-Modus läuft synchron, ohne DB/Telegram-Init und ohne Supervisor.
    if SETUP_TARGET:
        sys.exit(setup_profiles(SETUP_TARGET))
    try:
        asyncio.run(main())
    except Exception as e:
        log.error(f"❌ Critical Error in main runner: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        log.info("\n[supervisor] Abgebrochen durch Benutzer.")