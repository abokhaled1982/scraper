import os
import json # NEU: Import für json
from dataclasses import dataclass
from typing import Optional, Callable

from dotenv import load_dotenv
from telethon import TelegramClient
# NEU: Import für die Join-Logik
from telethon.errors import UserAlreadyParticipantError
from telethon.tl.functions.messages import ImportChatInviteRequest
import re 


# --- HILFS-FUNKTIONEN ---

from pathlib import Path
from typing import Tuple


@dataclass
class LoginConfig:
    api_id: int
    api_hash: str
    session_name: str = "my_session"
    session_dir: str = ".sessions"
    phone: Optional[str] = None
    password: Optional[str] = None

    @classmethod
    def from_env(cls) -> "LoginConfig":
        load_dotenv()
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

# NEU: Helper zum Kanalbeitritt und Auflösen der Entity
async def _ensure_join_and_resolve(client: TelegramClient, ref: str):
    invite_match = re.search(r"(?:t\.me\/joinchat\/|t\.me\/\+|invite\/)([A-Za-z0-9_-]+)", ref)
    if invite_match:
        invite_hash = invite_match.group(1)
        try:
            await client(ImportChatInviteRequest(invite_hash))
            # Keine Print-Ausgabe hier, die Clients machen das selbst, wenn sie es brauchen
        except UserAlreadyParticipantError:
            pass
        except Exception:
            pass
            
    return await client.get_entity(ref)


async def ensure_both_sessions_sequential(
    router_cfg: LoginConfig,
    observer_cfg: LoginConfig,
    # Hinzufügen der neuen Sender-Konfiguration zum Login-Check
    sender_cfg: LoginConfig,
    on_step: Optional[Callable[[str], None]] = None,
) -> Tuple[bool, bool, bool]: # Gibt jetzt drei Status zurück

    def say(msg: str):
        if on_step:
            on_step(msg)
        else:
            print(msg)

    # --- Router ---
    router_ok = False
    try:
        # Login-Logik... (unverändert)
        router_client = await ensure_logged_in(router_cfg)
        try:
            me = await router_client.get_me()
            say(f"✔ Router angemeldet als: {me.username or me.phone}")
            router_ok = True
        finally:
            await router_client.disconnect()
    except Exception as e:
        say(f"❌ Router-Login fehlgeschlagen: {e}")
        return False, False, False

    # --- Receiver (Observer) ---
    observer_ok = False
    try:
        # Login-Logik... (unverändert)
        observer_client = await ensure_logged_in(observer_cfg)
        try:
            me2 = await observer_client.get_me()
            say(f"✔ Receiver (Observer) angemeldet als: {me2.username or me2.phone}")
            observer_ok = True
        finally:
            await observer_client.disconnect()
    except Exception as e:
        say(f"❌ Receiver-Login fehlgeschlagen: {e}")
        return True, False, False

    # --- Sender (Observer) ---
    sender_ok = False
    try:
        # Login-Logik... (unverändert)
        sender_client = await ensure_logged_in(sender_cfg)
        try:
            me3 = await sender_client.get_me()
            say(f"✔ Sender (Observer) angemeldet als: {me3.username or me3.phone}")
            sender_ok = True
        finally:
            await sender_client.disconnect()
    except Exception as e:
        say(f"❌ Sender-Login fehlgeschlagen: {e}")
        return True, True, False


    return router_ok, observer_ok, sender_ok

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
    """
    if not cfg.api_id or not cfg.api_hash:
        raise RuntimeError("API_ID/API_HASH fehlen in .env")

    _ensure_dir(cfg.session_dir)
    session_path = os.path.join(cfg.session_dir, cfg.session_name)
    
    # 🌟 Workaround zur Erhöhung des SQLite-Timeouts 🌟
    sqlite_timeout_config = json.dumps({"db_timeout": 5.0}) 

    client = TelegramClient(
        session_path, 
        cfg.api_id, 
        cfg.api_hash,
        device_model=sqlite_timeout_config # WICHTIG: Setzt das Timeout
    )
    
    # Client MUSS verbunden sein, um den Login-Status zu prüfen
    await client.connect() 

    if await client.is_user_authorized():
        print(f"✅ Session '{cfg.session_name}' gültig – kein Login nötig.")
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