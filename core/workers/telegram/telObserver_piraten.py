# telegram/telObserver_piraten.py  (angepasst)
import os
import sys
import asyncio
import re
import json
import hashlib
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Tuple, Any
from telethon.tl.functions.channels import JoinChannelRequest

from dotenv import load_dotenv
load_dotenv()

# Projektwurzel in sys.path aufnehmen
PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from core.logging import get_logger  # noqa: E402
log = get_logger("telObserver_piraten")  # noqa: E402

# Telethon Importe
try:
    from telethon import events, TelegramClient
    from telethon.errors import UserAlreadyParticipantError
    from telethon.tl.functions.messages import ImportChatInviteRequest
except Exception as e:
    log.error("❌ Fehlende Abhängigkeit: telethon.")
    raise

# Login Helper Import (falls vorhanden)
try:
    from core.workers.telegram.login_once import LoginConfig, ensure_logged_in
except Exception:
    # Fallback-Implementierung (einfacher)
    class LoginConfig:
        def __init__(self, api_id, api_hash, session_name, session_dir, phone, password):
            self.api_id = api_id
            self.api_hash = api_hash
            self.session_name = session_name
            self.session_dir = session_dir
            self.phone = phone
            self.password = password

    async def ensure_logged_in(cfg):
        session_file = os.path.join(cfg.session_dir, cfg.session_name)
        client = TelegramClient(session_file, cfg.api_id, cfg.api_hash)
        await client.start(phone=cfg.phone, password=cfg.password)
        return client

# ------------------------
# ENV / Konfiguration (PIRATEN SPEZIFISCH)
# ------------------------
LOCK_FILE = None
try:
    from core import paths as config
    LOCK_FILE = Path(getattr(config, "LOCK_FILE", ".locks/product_list.lock"))
except Exception:
    LOCK_FILE = Path(os.getenv("LOCK_FILE", ".locks/product_list.lock"))

from core.db import state_repo
_PRODUCT_LIST_KEY = "product_list"


API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
SESSION_DIR = os.getenv("SESSION_DIR", ".sessions")
PHONE = os.getenv("TELEGRAM_PHONE")
PASSWORD = os.getenv("TELEGRAM_PASSWORD")

# WICHTIG: Env-Variable für die Piraten-Kanäle. Mehrere Kanäle Komma-getrennt.
# Optional kann jedem Kanal ein Label vorangestellt werden: "Label=URL".
# Unterstützte URL-Formate:
#   - https://t.me/Username                     (öffentlich)
#   - https://t.me/+InviteHash                  (privat)
#   - https://web.telegram.org/a/#-100XXXXXXXXXX (Kanal-ID; Account muss Mitglied sein)
#   - -100XXXXXXXXXX                            (rohe Kanal-ID)
_RAW_REFS = (os.getenv("PIRATEN_CHANNEL_INVITE_URL") or "").strip()

def _parse_refs_with_labels(raw: str):
    """Parst "Label=URL,Label2=URL2,URLohneLabel" → Liste von (label_or_None, url)."""
    items = []
    for chunk in re.split(r"[,\s;]+", raw):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" in chunk and not chunk.lstrip().startswith("http"):
            label, _, url = chunk.partition("=")
            label = label.strip() or None
            url = url.strip()
            if url:
                items.append((label, url))
        else:
            items.append((None, chunk))
    return items

PIRATEN_CHANNEL_ENTRIES = _parse_refs_with_labels(_RAW_REFS)
PIRATEN_CHANNEL_REFS = [u for _, u in PIRATEN_CHANNEL_ENTRIES]
PIRATEN_CHANNEL_LABELS = {u: lbl for lbl, u in PIRATEN_CHANNEL_ENTRIES if lbl}
PIRATEN_SESSION_NAME = os.getenv("PIRATEN_SESSION_NAME", "piraten_session")

if not API_ID or not API_HASH:
    raise SystemExit("❌ API_ID und API_HASH fehlen in .env")
if not PIRATEN_CHANNEL_REFS:
    raise SystemExit("❌ PIRATEN_CHANNEL_INVITE_URL fehlt in .env! (z.B. PirateDeals=https://t.me/PirateDeals)")

PIRATEN_CFG = LoginConfig(API_ID, API_HASH, PIRATEN_SESSION_NAME, SESSION_DIR, PHONE, PASSWORD)

# ------------------------
# ATOMARE DATEI-HELPER
# ------------------------
try:
    import fcntl
    @contextmanager
    def _locked_file(lock_path: Path):
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "a+") as fp:
            fcntl.flock(fp.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fp.fileno(), fcntl.LOCK_UN)
except Exception:
    @contextmanager
    def _locked_file(lock_path: Path):
        # Windows / fallback: no-op
        yield

def load_store(path: Path | None = None) -> Dict[str, Dict]:
    return state_repo.get_dict(_PRODUCT_LIST_KEY)

def save_store(path: Path | None, store: Dict[str, Dict]):
    state_repo.put(_PRODUCT_LIST_KEY, store)

def product_key(item: Dict[str, Any]) -> str:
    url = item.get("product_url", "").strip()
    if not url:
        return f"R-{hashlib.sha1(str(time.time()).encode()).hexdigest()[:10]}"
    # Versuch ASIN-Extraktion (Amazon)
    m = re.search(r'/(?:dp|gp/product|d|o)/([A-Z0-9]{10})(?:[\/?]|$)', url, re.IGNORECASE)
    if m:
        asin = m.group(1).upper()
        return f"A-{asin}"
    return f"U-{hashlib.sha1(url.encode()).hexdigest()[:10]}"

def add_link_to_product_list(url: str) -> Tuple[bool, str]:
    minimal = {
        "product_url": url,
        "source": "telegram_piraten",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    key = product_key(minimal)
    with _locked_file(LOCK_FILE):
        # Lesen direkt vor Schreiben: vermeidet, dass ein veralteter
        # In-Memory-Snapshot einen Dashboard-DB-Reset überschreibt.
        store = load_store()
        if key in store:
            return False, "Link bereits vorhanden"
        # Atomare Merge-Operation: bei DB-Reset bleibt der Reset bestehen,
        # nur der EINE neue Eintrag wird hinzugefügt.
        state_repo.update_dict(_PRODUCT_LIST_KEY, {key: minimal})
    return True, f"Hinzugefügt (Key: {key})"

# ------------------------
# Kurzlink → Amazon-URL auflösen
# ------------------------
def resolve_shortlink(url: str, timeout: int = 8) -> str:
    """Folgt Weiterleitungen und gibt die finale URL zurück. Gibt url zurück bei Fehler."""
    try:
        import urllib.request
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None

        opener = urllib.request.build_opener(NoRedirect())
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}, method="HEAD")
        try:
            opener.open(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            loc = e.headers.get("Location")
            if loc:
                # Relative URLs auflösen
                if loc.startswith("/"):
                    from urllib.parse import urlparse
                    p = urlparse(url)
                    loc = f"{p.scheme}://{p.netloc}{loc}"
                return loc
        # Fallback GET für Server die kein HEAD unterstützen
        req2 = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        response = urllib.request.urlopen(req2, timeout=timeout)
        final = response.geturl()
        if final and final != url:
            return final
    except Exception:
        pass
    return url

def extract_best_url(raw_url: str) -> str:
    """Löst Kurzlinks auf und gibt Amazon-URL zurück falls möglich."""
    # Wenn bereits Amazon → direkt zurück
    if re.search(r'amazon\.(de|com|co\.uk|fr|it|es)', raw_url, re.I):
        return raw_url
    # Bekannte Kurzlink-Domains → auflösen
    if re.search(r'(pirat\.deals|amzn\.to|amzn\.eu|t\.co|bit\.ly|tinyurl)', raw_url, re.I):
        log.info(f"[PIRATEN] 🔗 Löse Kurzlink auf: {raw_url}")
        resolved = resolve_shortlink(raw_url)
        if resolved != raw_url:
            log.info(f"[PIRATEN]    → {resolved}")
        return resolved
    return raw_url

# ------------------------
# Channel Management (robust)
# ------------------------
def _parse_channel_ref(ref: str):
    """Übersetzt einen Roh-String in das, was Telethon auflösen kann.
    Gibt entweder PeerChannel(id) (int-ID) oder den (gesäuberten) Username/URL zurück.
    Zweiter Rückgabewert: optionaler Invite-Hash (für private t.me/+ Links).
    """
    ref = ref.strip()

    # 1) Private Invite-Hash (t.me/+ABC oder t.me/joinchat/ABC)
    m_inv = re.search(r'(?:t\.me/joinchat/|t\.me/\+|invite/)([A-Za-z0-9_-]+)', ref, re.I)
    if m_inv:
        return None, m_inv.group(1)

    # 2) web.telegram.org/a/#-100XXXXXXXXXX oder /k/#-100XXX...
    m_web = re.search(r'web\.telegram\.org/[ak]/#(-?\d+)', ref, re.I)
    if m_web:
        cid = int(m_web.group(1))
        return _peer_from_int(cid), None

    # 3) Rohe Channel-ID, z.B. -1001609138702 oder 1609138702
    if re.fullmatch(r'-?\d{6,}', ref):
        cid = int(ref)
        return _peer_from_int(cid), None

    # 4) Öffentlicher Username / t.me/Username
    clean = re.sub(r'https?://t\.me/', '', ref).strip('/ ')
    if clean.startswith('@'):
        clean = clean[1:]
    return clean, None


def _peer_from_int(cid: int):
    """Wandelt eine numerische Bot-API-ID in einen Telethon-Peer um."""
    from telethon.tl.types import PeerChannel, PeerChat, PeerUser
    # -100... → Channel/Supergroup
    if cid <= -1000000000000:
        return PeerChannel(int(str(cid)[4:]))
    if cid < 0:
        return PeerChat(-cid)
    # Positive ID kann sowohl Channel als auch User sein – probiere Channel zuerst
    return PeerChannel(cid)


async def _ensure_join_and_resolve(client: TelegramClient, ref: str):
    log.info(f"ℹ️ Versuche Kanal/Entität zu lösen: {ref}")
    target, invite_hash = _parse_channel_ref(ref)

    # Privater Invite-Link: erst beitreten
    if invite_hash:
        try:
            updates = await client(ImportChatInviteRequest(invite_hash))
            log.info(f"✅ Kanal beigetreten via Invite-Hash: {invite_hash}")
            chats = getattr(updates, 'chats', None) or []
            if chats:
                return chats[0]
        except UserAlreadyParticipantError:
            log.info("ℹ️ Bereits Teilnehmer (Invite-Hash).")
        except Exception as e:
            log.warning(f"⚠️ Invite via Hash fehlgeschlagen: {e}")
        target = invite_hash  # Marker für Dialog-Scan

    # Bei numerischen IDs / PeerChannel: zuerst Dialoge in den Cache laden,
    # damit get_entity() den access_hash kennt.
    from telethon.tl.types import PeerChannel
    needs_cache_warmup = isinstance(target, (int, PeerChannel)) or invite_hash is not None
    if needs_cache_warmup:
        try:
            log.info("ℹ️ Lade Dialoge in den Cache ...")
            await client.get_dialogs()
        except Exception as e:
            log.warning(f"⚠️ get_dialogs() fehlgeschlagen: {e}")

    # Auflösung versuchen
    try:
        entity = await client.get_entity(target) if target is not None else None
        if entity is not None:
            title = getattr(entity, 'title', None) or getattr(entity, 'username', None) or str(target)
            log.info(f"✅ Entity gefunden: {title}")
            try:
                await client(JoinChannelRequest(entity))
                log.info("✅ Beitritt OK (oder war bereits Mitglied).")
            except UserAlreadyParticipantError:
                pass
            except Exception as e_join:
                log.info(f"ℹ️ Kein expliziter Join nötig/möglich: {e_join}")
            return entity
    except Exception as e:
        log.warning(f"⚠️ get_entity({target!r}) fehlgeschlagen: {e} — durchsuche Dialoge ...")

    # Robuster Dialog-Scan: vergleicht ID in mehreren Formen
    target_ids = set()
    if isinstance(target, int):
        cid = abs(target)
        if cid > 1_000_000_000_000:  # -100... entfernen
            cid = int(str(cid)[3:])
        target_ids.add(cid)
        target_ids.add(target)
    elif isinstance(target, PeerChannel):
        target_ids.add(target.channel_id)
        target_ids.add(-1_000_000_000_000 - target.channel_id)

    try:
        from telethon.utils import get_peer_id
        async for dialog in client.iter_dialogs():
            ent = dialog.entity
            try:
                peer_marked = get_peer_id(ent)  # -100<id> Form
            except Exception:
                peer_marked = None
            ent_id = getattr(ent, 'id', None)
            if ent_id in target_ids or peer_marked in target_ids:
                log.info(f"✅ Entity in Dialogs gefunden: {getattr(ent, 'title', ent_id)} (id={ent_id})")
                return ent
    except Exception as e:
        log.error(f"❌ Dialog-Scan fehlgeschlagen: {e}")

    raise RuntimeError(
        f"Konnte {ref} nicht auflösen.\n"
        f"   → Der Account ist offenbar (noch) NICHT Mitglied dieses Kanals.\n"
        f"   → Mit nur einer Kanal-ID kann Telethon nicht beitreten – "
        f"bitte öffne den Kanal einmal manuell in der Telegram-App (über Invite-Link "
        f"oder Username) oder setze in der .env stattdessen einen Username (https://t.me/xyz) "
        f"bzw. Invite-Link (https://t.me/+ABC...) für diesen Kanal."
    )
# ------------------------
# Message Handling (wie telObserver)
# ------------------------
def extract_links_from_msg(msg) -> list:
    """Extrahiert alle URLs aus Text, Entity-URLs und Inline-Button-URLs."""
    text = (msg.message or "").strip()
    url_pattern = re.compile(
        r'(https?://(?!t\.me/)[^\s<>"\'\)\]]+[^\s\.,;:!?\)\]\<>"\'])',
        re.IGNORECASE
    )
    links = list({f for f in url_pattern.findall(text)})

    # Entity-URLs (Text hinter "Zum Angebot" etc.)
    if msg.entities:
        for ent in msg.entities:
            url = getattr(ent, 'url', None)
            if url and not re.match(r'https?://t\.me/', url, re.I):
                if url not in links:
                    links.append(url)

    # Inline-Keyboard-Button-URLs (pirat.deals nutzt oft Buttons statt Text-Links)
    if msg.reply_markup:
        try:
            for row in msg.reply_markup.rows:
                for button in row.buttons:
                    url = getattr(button, 'url', None)
                    if url and not re.match(r'https?://t\.me/', url, re.I):
                        if url not in links:
                            links.append(url)
        except Exception:
            pass

    return links

async def process_message_links(links: list, chat_name: str, log_preview: str = ""):
    """Löst Kurzlinks auf und trägt sie in die DB ein. Loggt pro Link „URL  ←  [Kanal]“."""
    added_count = 0
    if log_preview:
        log.info(f"[PIRATEN:{chat_name}] Links gefunden ({len(links)}): {log_preview[:120]}")
    for raw_url in links:
        try:
            # In Thread ausführen damit async loop nicht blockiert
            url = await asyncio.get_event_loop().run_in_executor(None, extract_best_url, raw_url)
            success, reason = add_link_to_product_list(url)
            if success:
                added_count += 1
                log.info(f"🔗 {url}   ←  [{chat_name}]")
            else:
                log.info(f"[PIRATEN] ℹ️ {reason}: {url}   ←  [{chat_name}]")
        except Exception as e:
            log.error(f"[PIRATEN] Fehler bei {raw_url}: {e}")
    if added_count > 0:
        log.info(f"[PIRATEN:{chat_name}] ✅ {added_count} neue Links gespeichert.")
    return added_count

async def handle_message(evt: events.NewMessage.Event, chat_name_override: str | None = None):
    msg = evt.message
    if chat_name_override:
        chat_name = chat_name_override
    else:
        try:
            chat = await evt.get_chat()
            chat_name = getattr(chat, "title", None) or getattr(chat, "username", None) or "Kanal"
        except Exception:
            chat_name = "Kanal"

    text = (msg.message or "").strip()
    log_preview = text.replace('\n', ' ')
    links = extract_links_from_msg(msg)

    if links:
        await process_message_links(links, chat_name, log_preview)
    else:
        if log_preview:
            log.info(f"[PIRATEN:{chat_name}] {log_preview[:120]}")
        else:
            log.info(f"[PIRATEN:{chat_name}] [Medien/Leer]")

# ------------------------
# Main Loop
# ------------------------
CATCHUP_MESSAGES = int(os.getenv("PIRATEN_CATCHUP", "0"))  # 0 = nur zukünftige Nachrichten verarbeiten

async def _amain():
    from core.db import workers_repo, config_repo
    _WORKER = "tel_observer_piraten"
    workers_repo.register(_WORKER)
    log.info(f"🏴‍☠️ Starte Piraten-Observer Session: {PIRATEN_SESSION_NAME}")
    log.info(f"🏴‍☠️ Kanäle ({len(PIRATEN_CHANNEL_REFS)}): {PIRATEN_CHANNEL_REFS}")
    client = await ensure_logged_in(PIRATEN_CFG)
    async with client:
        entities = []
        # Mapping: entity.id -> Anzeigename (Label aus .env hat Vorrang)
        id_to_name: Dict[int, str] = {}
        for ref in PIRATEN_CHANNEL_REFS:
            try:
                ent = await _ensure_join_and_resolve(client, ref)
                title = (
                    PIRATEN_CHANNEL_LABELS.get(ref)
                    or getattr(ent, 'title', None)
                    or getattr(ent, 'username', None)
                    or str(ref)
                )
                entities.append(ent)
                id_to_name[ent.id] = title
                log.info(f"   ✅ {ref}  →  {title} (id={ent.id})")
            except Exception as e:
                log.error(f"❌ Kanal {ref} übersprungen: {e}")

        if not entities:
            raise SystemExit("❌ Kein einziger Kanal konnte aufgelöst werden – Abbruch.")

        log.info(f"🏴‍☠️ Überwache {len(entities)} Kanal/Kanäle: {list(id_to_name.values())}")
        workers_repo.set_task(_WORKER, f"watching {len(entities)} channel(s)")

        # ── Catch-Up: letzte N Nachrichten je Kanal beim Start verarbeiten ──
        if CATCHUP_MESSAGES > 0:
            log.info(f"⏪ Catch-Up: verarbeite letzte {CATCHUP_MESSAGES} Nachrichten je Kanal...")
            total_added = 0
            for ent in entities:
                name = id_to_name.get(ent.id, "Kanal")
                async for msg in client.iter_messages(ent, limit=CATCHUP_MESSAGES):
                    links = extract_links_from_msg(msg)
                    if links:
                        added = await process_message_links(links, name)
                        total_added += added
            log.info(f"⏪ Catch-Up abgeschlossen: {total_added} neue Links hinzugefügt.\n")

        # ── Live-Listener: neue Nachrichten ───────────────────────────────
        @client.on(events.NewMessage(chats=entities))
        async def _on(evt):
            try:
                # Dashboard-Toggle: piraten.enabled = False → komplett ignorieren.
                # Session bleibt verbunden, damit Sofort-Reaktivierung möglich ist.
                # set_stopped wurde bereits vom _status_loop gesetzt – kein extra DB-Write.
                if not config_repo.is_enabled("piraten"):
                    return
                # Label aus .env nutzen, sonst Telegram-Titel
                chat_id = getattr(evt.chat_id, "real", None) or evt.chat_id
                # evt.chat_id ist bereits int (-100... Form); auf raw id mappen
                raw = abs(chat_id)
                if raw > 1_000_000_000_000:
                    raw = int(str(raw)[3:])
                chat_name = id_to_name.get(raw) or id_to_name.get(chat_id) or "Kanal"
                if chat_name == "Kanal":
                    try:
                        chat = await evt.get_chat()
                        chat_name = getattr(chat, "title", None) or getattr(chat, "username", None) or "Kanal"
                    except Exception:
                        pass
                await handle_message(evt, chat_name_override=chat_name)
                workers_repo.set_task(_WORKER, "processed message")
            except Exception as e:
                workers_repo.set_error(_WORKER, str(e)[:200])
                log.error(f"❌ Piraten-Error: {e}")

        # ── Statusfähnchen: zeigt im Dashboard/TUI an, ob Observer aktiv ist ──
        async def _status_loop():
            last = None
            while True:
                try:
                    on = config_repo.is_enabled("piraten")
                    if on != last:
                        if on:
                            # Reaktiviert: Worker wieder als idle/aktiv registrieren
                            workers_repo.register(_WORKER)
                            workers_repo.set_task(
                                _WORKER, f"👀 aktiv — überwache {len(entities)} Kanal/Kanäle"
                            )
                            log.info("✅ Piraten-Observer aktiviert (Dashboard-Toggle).")
                        else:
                            # Deaktiviert: Worker als stopped markieren damit Dashboard
                            # korrekt "stopped" zeigt und nicht fälschlich "idle/active".
                            workers_repo.set_stopped(_WORKER)
                            log.info("⏸ Piraten-Observer deaktiviert (Dashboard-Toggle).")
                        last = on
                except Exception:
                    pass
                await asyncio.sleep(2.0)

        asyncio.create_task(_status_loop())

        log.info("🔴 Live-Listener aktiv – warte auf neue Nachrichten...")
        await client.run_until_disconnected()

if __name__ == "__main__":
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        pass
