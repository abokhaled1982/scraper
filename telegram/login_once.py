import os
from dataclasses import dataclass
from typing import Optional, Callable

from dotenv import load_dotenv
from telethon import TelegramClient


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
