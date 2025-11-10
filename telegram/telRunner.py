# telegram_runner.py (Projektwurzel, neben /telegram)
import os, sys, subprocess, signal, time
from pathlib import Path
from dotenv import load_dotenv

# .env laden
load_dotenv()

# Pfade & Module
PROJECT_ROOT = Path(__file__).resolve().parent
PY = sys.executable  # aktuelles venv-Python

# Gemeinsame App-Creds
API_ID  = os.getenv("API_ID", "")
API_HASH = os.getenv("API_HASH", "")
if not API_ID or not API_HASH:
    raise SystemExit("API_ID/API_HASH fehlen in .env")

# Sessions & Kanäle
SESSION_DIR   = os.getenv("SESSION_DIR", ".sessions")
ROUTER_NAME   = os.getenv("SESSION_NAME", "main_session")
OBSERVER_NAME = os.getenv("OBS_SESSION_NAME", "observer_session")

ROUTER_CHANNEL = os.getenv("CHANNEL_INVITE_URL", "").strip()
OBSERVER_CHANNEL = os.getenv("OBS_CHANNEL_INVITE_URL", "").strip()
if not ROUTER_CHANNEL or not OBSERVER_CHANNEL:
    raise SystemExit("CHANNEL_INVITE_URL und OBS_CHANNEL_INVITE_URL müssen in .env gesetzt sein.")

# Login-Helfer importieren
sys.path.insert(0, str(PROJECT_ROOT))
from login_once import LoginConfig, ensure_both_sessions_sequential

def info(msg: str): print(msg, flush=True)

async def _do_login():
    # Beide Konfigurationen: gleiche App, unterschiedliche Sessions
    router_cfg = LoginConfig(
        api_id=int(API_ID),
        api_hash=API_HASH,
        session_name=ROUTER_NAME,
        session_dir=SESSION_DIR,
        phone=os.getenv("TELEGRAM_PHONE"),
        password=os.getenv("TELEGRAM_PASSWORD"),
    )
    observer_cfg = LoginConfig(
        api_id=int(API_ID),
        api_hash=API_HASH,
        session_name=OBSERVER_NAME,
        session_dir=SESSION_DIR,
        phone=os.getenv("TELEGRAM_PHONE"),
        password=os.getenv("TELEGRAM_PASSWORD"),
    )
    ok1, ok2 = await ensure_both_sessions_sequential(
        router_cfg, observer_cfg, on_step=info
    )
    if not (ok1 and ok2):
        raise SystemExit("Login fehlgeschlagen.")

def _start_processes():
    """
    Startet Router & Observer erst nach erfolgreichem Login.
    Setzt pro Prozess die relevanten ENV Variablen, falls du je Prozess andere Werte brauchst.
    """
    # Gemeinsame Basis-Umgebung aus dem venv übernehmen
    base_env = os.environ.copy()

    # Router-Prozess
    router_env = base_env.copy()
    router_env["SESSION_NAME"] = ROUTER_NAME
    router_env["CHANNEL_INVITE_URL"] = ROUTER_CHANNEL
    router_cmd = [PY, "-m", "telegram.telRouter"]  # dein bestehendes Modul

    # Observer-Prozess
    observer_env = base_env.copy()
    observer_env["OBS_SESSION_NAME"] = OBSERVER_NAME
    observer_env["OBS_CHANNEL_INVITE_URL"] = OBSERVER_CHANNEL
    observer_cmd = [PY, "-m", "telegram.telObserver"]  # das neue Modul

    print("▶ Starte Router …")
    p1 = subprocess.Popen(router_cmd, env=router_env)
    print("▶ Starte Observer …")
    p2 = subprocess.Popen(observer_cmd, env=observer_env)

    return p1, p2

def _wait_and_forward(p1, p2):
    """
    Einfache Lauf-Schleife: wenn einer endet, beenden wir den anderen sauber.
    """
    try:
        while True:
            rc1 = p1.poll()
            rc2 = p2.poll()
            if rc1 is not None or rc2 is not None:
                print("ℹ️ Ein Prozess ist beendet – fahre alles herunter …")
                break
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n⛔ Stop angefordert – beende Prozesse …")
    finally:
        for p in (p1, p2):
            if p and p.poll() is None:
                try:
                    if os.name == "nt":
                        p.terminate()
                    else:
                        p.send_signal(signal.SIGTERM)
                except Exception:
                    pass

if __name__ == "__main__":
    import asyncio
    # 1) ZUERST: sequentieller Login (Router -> Observer)
  # ... (oberer Teil unverändert)

if __name__ == "__main__":
    import asyncio, argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["login", "run"], default="run",
                        help="login: nur Login durchführen und beenden; run: Login + Router/Observer starten")
    args = parser.parse_args()

    if args.mode == "login":
        # ✅ Nur Login sicherstellen und beenden (blocking)
        asyncio.run(_do_login())
        print("✅ Telegram-Login für Router & Observer abgeschlossen. (mode=login)")
        sys.exit(0)

    # mode=run: Login + beide Prozesse starten
    asyncio.run(_do_login())
    print("✅ Login für Router & Observer abgeschlossen.\n")

    p_router, p_observer = _start_processes()
    _wait_and_forward(p_router, p_observer)
    print("✅ Runner beendet.")
