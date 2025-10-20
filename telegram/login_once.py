import os
from dataclasses import dataclass
from typing import Optional, Callable

from dotenv import load_dotenv
from telethon import TelegramClient

# --- NEU: Hilfs-Funktionen für 2-stufigen Login-- #

from pathlib import Path
from typing import Tuple


@dataclass
class LoginConfig:
    api_id: int
    api_hash: str
    session_name: str = "my_session"   # ergibt ./.sessions/my_session.session
    session_dir: str = ".sessions"     # liegt auf Root-Ebene (neben /telegram)
    phone: Optional[str] = None        # +49...
    password: Optional[str] = None     # 2FA-Passwort (nur wenn 2FA aktiv)

    @classmethod
    def from_env(cls) -> "LoginConfig":
        load_dotenv()  # liest .env aus Projektwurzel
        return cls(
            api_id=int(os.getenv("API_ID", "0")),
            api_hash=os.getenv("API_HASH", ""),
            session_name=os.getenv("SESSION_NAME", "my_session"),
            session_dir=os.getenv("SESSION_DIR", ".sessions"),
            phone=os.getenv("TELEGRAM_PHONE"),
            password=os.getenv("TELEGRAM_PASSWORD"),
        )

def session_file_exists(cfg: LoginConfig) -> bool:
    """Prüft, ob die Session-Datei physisch existiert."""
    return Path(cfg.session_dir).joinpath(f"{cfg.session_name}.session").exists()

async def ensure_both_sessions_sequential(
    router_cfg: LoginConfig,
    observer_cfg: LoginConfig,
    on_step: Optional[Callable[[str], None]] = None,
) -> Tuple[bool, bool]:
    def say(msg: str):
        if on_step:
            on_step(msg)
        else:
            print(msg)

    # --- Router ---
    router_ok = False
    try:
        if session_file_exists(router_cfg):
            say("✔ Router-Session gefunden – prüfe Autorisierung …")
        else:
            say("ℹ️ Router-Session fehlt – starte Login …")
        router_client = await ensure_logged_in(router_cfg)
        try:
            me = await router_client.get_me()
            say(f"✔ Router angemeldet als: {me.username or me.phone}")
            router_ok = True
        finally:
            await router_client.disconnect()
    except Exception as e:
        say(f"❌ Router-Login fehlgeschlagen: {e}")
        return False, False  # hier früh abbrechen, Observer gar nicht starten

    # --- Observer ---
    observer_ok = False
    try:
        if session_file_exists(observer_cfg):
            say("✔ Observer-Session gefunden – prüfe Autorisierung …")
        else:
            say("ℹ️ Observer-Session fehlt – starte Login …")
        observer_client = await ensure_logged_in(observer_cfg)
        try:
            me2 = await observer_client.get_me()
            say(f"✔ Observer angemeldet als: {me2.username or me2.phone}")
            observer_ok = True
        finally:
            await observer_client.disconnect()
    except Exception as e:
        say(f"❌ Observer-Login fehlgeschlagen: {e}")
        return True, False

    return router_ok, observer_ok

def _ensure_dir(path: str) -> None:
    if not os.path.isdir(path):
        os.makedirs(path, exist_ok=True)


def _env_or_prompt(value: Optional[str], label: str) -> str:
    if value:
        print(f"{label}: ✔ (aus .env)")
        return value
    return input(f"{label}: ").strip()


async def ensure_logged_in(cfg: LoginConfig) -> TelegramClient:
    """
    Nutzt/erstellt ./.sessions/<SESSION_NAME>.session
    - vorhandene Session => kein Prompt
    - erster Login => nutzt TELEGRAM_PHONE / TELEGRAM_PASSWORD aus .env (falls gesetzt)
    """
    if not cfg.api_id or not cfg.api_hash:
        raise RuntimeError("API_ID/API_HASH fehlen in .env")

    _ensure_dir(cfg.session_dir)
    session_path = os.path.join(cfg.session_dir, cfg.session_name)

    client = TelegramClient(session_path, cfg.api_id, cfg.api_hash)
    await client.connect()

    if await client.is_user_authorized():
        print("✅ Session gültig – kein Login nötig.")
        return client

    print("ℹ️  Keine gültige Session – starte Anmelde-Flow …")
    phone_cb: Callable[[], str] = lambda: _env_or_prompt(cfg.phone, "Telefonnummer (+49...)")
    password_cb: Callable[[], str] = lambda: _env_or_prompt(cfg.password, "2FA-Passwort")

    await client.start(phone=phone_cb, password=password_cb)

    if not await client.is_user_authorized():
        raise RuntimeError("❌ Login fehlgeschlagen – nicht autorisiert.")

    print(f"✅ Angemeldet. Session gespeichert unter: {session_path}.session")
    return client


# Optional: als Skript nutzbar -> `python -m telegram.login_once`
async def _amain():
    cfg = LoginConfig.from_env()
    client = await ensure_logged_in(cfg)
    # Beispiel: wer bin ich?
    me = await client.get_me()
    print("Angemeldet als:", me.username or me.phone)
    await client.disconnect()

if __name__ == "__main__":
    import asyncio
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        print("\nAbgebrochen.")
