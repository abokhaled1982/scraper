"""
core/workers/facebook/creatomate_accounts.py
=============================================

Production-Modul fuer den **Creatomate Account-Fallback**.

Aufgabe
-------
Wenn ein Render an Creatomate fehlschlaegt, weil der aktuell konfigurierte
Account keine Credits mehr hat (HTTP 402 / 401-403 mit credit/plan/quota im
Body), waehlen wir transparent den naechsten Account aus dem Pool unter
``creatomate/accounts/<name>_(all|fashon).txt`` und persistieren den
Sieger-Account zurueck in die Template-Registry
(``core/workers/facebook/templates/*.json``).

Was hier _nicht_ gemacht wird:
* Kein eigener Renderer – die HTTP-Logik bleibt in
  ``reels_service.render_template`` / ``render_typ3_audio``. Dieses Modul
  liefert nur den Pool, die Fehlererkennung und die Persistenz.
* Kein automatisches Reaktivieren "ablaufender" Accounts – sobald wir einen
  Account in dieser Prozess-Laufzeit als "kein Credit" markiert haben, wird
  er bis zum Prozess-Neustart uebersprungen.

Mapping template_type -> account_kind
-------------------------------------
1. Wenn die Template-JSON ein Feld ``account_kind`` setzt: gewinnt.
2. Sonst: Heuristik per Substring ("fashion" -> fashion, sonst all).
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from pathlib import Path

from core.logging import get_logger

log = get_logger("creatomate_accounts")


# ── Pfade ─────────────────────────────────────────────────────────────────────
# core/workers/facebook/creatomate_accounts.py
#   parent  -> core/workers/facebook
#   .parent -> core/workers
#   .parent -> core
#   .parent -> <repo root>
_REPO_ROOT      = Path(__file__).resolve().parent.parent.parent.parent
_ACCOUNTS_DIR   = _REPO_ROOT / "creatomate" / "accounts"
_TEMPLATES_DIR  = Path(__file__).resolve().parent / "templates"


# ── Parsing (curl-Datei -> api_key + template_id + modifications) ────────────
_BEARER_RE  = re.compile(r"Authorization:\s*Bearer\s+([A-Za-z0-9_\-]+)", re.IGNORECASE)
_JSON_BLOCK = re.compile(r"-d\s+'(?P<body>\{.*?\})'\s*$", re.DOTALL)

# Suffix-Konvention der Account-Dateinamen.
# Reihenfolge ist wichtig: laengere Suffixe zuerst pruefen.
_KIND_SUFFIXES: tuple[tuple[str, str], ...] = (
    ("_fashion", "fashion"),
    ("_fashon",  "fashion"),
    ("_all",     "all"),
)


@dataclass(frozen=True)
class CreatomateAccount:
    """Ein Creatomate-Account (Mapping pro account+kind = pro .txt)."""
    name: str
    kind: str          # "all" | "fashion"
    api_key: str
    template_id: str
    source_file: str   # relativer Pfad, fuers Logging


def _split_account_kind(stem: str) -> tuple[str, str] | None:
    low = stem.lower()
    for suffix, kind in _KIND_SUFFIXES:
        if low.endswith(suffix):
            return stem[: -len(suffix)], kind
    return None


def _parse_account_file(path: Path) -> CreatomateAccount | None:
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    bearer = _BEARER_RE.search(text)
    if not bearer:
        return None

    body_match = _JSON_BLOCK.search(text)
    if not body_match:
        return None
    try:
        body = json.loads(body_match.group("body"))
    except json.JSONDecodeError:
        return None

    tpl_id = str(body.get("template_id") or "").strip()
    if not tpl_id:
        return None

    split = _split_account_kind(path.stem)
    if split is None:
        return None
    name, kind = split

    return CreatomateAccount(
        name=name,
        kind=kind,
        api_key=bearer.group(1).strip(),
        template_id=tpl_id,
        source_file=str(path.relative_to(_REPO_ROOT)),
    )


def load_accounts(kind: str) -> list[CreatomateAccount]:
    """
    Lädt alle Accounts einer Kategorie aus ``creatomate/accounts/``.

    Sortierung: alphabetisch nach ``name``. Dateien, die nicht parsbar sind
    (z.B. nacktes Template-JSON statt curl), werden uebersprungen.
    Die Pseudo-Datei ``expired.txt`` ist **nicht** Teil des Pools – sie ist
    nur zum Testen unter ``creatomate/scripts/fallback_test.py`` vorgesehen.
    """
    if not _ACCOUNTS_DIR.is_dir():
        return []
    accounts: list[CreatomateAccount] = []
    for fp in sorted(_ACCOUNTS_DIR.glob("*.txt")):
        if fp.stem.lower() == "expired":
            continue
        acc = _parse_account_file(fp)
        if acc and acc.kind == kind:
            accounts.append(acc)
    accounts.sort(key=lambda a: a.name.lower())
    return accounts


# ── Credit-Fehler-Heuristik (von fallback_test.py uebernommen) ───────────────
_NO_CREDIT_KEYWORDS = (
    "credit",
    "credits",
    "insufficient",
    "payment required",
    "out of credits",
    "no credits",
    "plan",
    "subscription",
    "quota",
    "trial",
    "expired",
    "upgrade",
)


def is_no_credit_error(status_code: int, body_text: str | None) -> bool:
    """True, wenn der Fehler "Account hat keine Credits/Plan abgelaufen" ist."""
    if status_code == 402:                # Payment Required
        return True
    if 400 <= status_code < 500:
        low = (body_text or "").lower()
        if any(k in low for k in _NO_CREDIT_KEYWORDS):
            return True
    return False


# ── Mapping template_type -> kind ────────────────────────────────────────────
def kind_for_template_type(template_type: str, template_cfg: dict | None = None) -> str:
    """
    Welcher Account-Pool gehoert zu diesem template_type?

    Priorität:
      1. ``template_cfg["account_kind"]`` (explizit in templates/*.json)
      2. Heuristik nach template_type-Namen.
    """
    if template_cfg:
        explicit = str(template_cfg.get("account_kind") or "").strip().lower()
        if explicit in ("all", "fashion"):
            return explicit

    low = (template_type or "").lower()
    if "fashion" in low:
        return "fashion"
    return "all"


# ── Persistenz: Sieger-Account in templates/*.json schreiben ─────────────────
_FILE_LOCK = threading.Lock()


def _find_template_file(template_type: str) -> Path | None:
    """Sucht die templates/*.json, die diesen template_type definiert."""
    if not _TEMPLATES_DIR.is_dir():
        return None
    for fp in sorted(_TEMPLATES_DIR.glob("*.json")):
        try:
            cfg = json.loads(fp.read_text(encoding="utf-8"))
        except Exception:
            continue
        if str(cfg.get("template_type") or "").strip() == template_type:
            return fp
    return None


def persist_active_account(template_type: str, account: CreatomateAccount) -> Path | None:
    """
    Schreibt ``api_key`` + ``template_id`` (und ``account``) in die zugehoerige
    Template-JSON, damit beim nächsten Lauf direkt der funktionierende Account
    verwendet wird.

    Andere Felder der JSON bleiben unveraendert. Die geaenderten Felder werden
    an den Anfang des Objekts sortiert – das macht den Live-Account beim
    Diff/Review sofort sichtbar.
    """
    fp = _find_template_file(template_type)
    if not fp:
        log.warning(
            "[CM-ACCT] templates/*.json fuer template_type=%r nicht gefunden – "
            "kann Sieger-Account nicht persistieren.",
            template_type,
        )
        return None

    with _FILE_LOCK:
        try:
            current = json.loads(fp.read_text(encoding="utf-8"))
        except Exception as exc:
            log.error("[CM-ACCT] %s kann nicht gelesen werden: %s", fp.name, exc)
            return None
        if not isinstance(current, dict):
            log.error("[CM-ACCT] %s ist kein JSON-Objekt – uebersprungen.", fp.name)
            return None

        merged: dict = {
            "template_type": current.get("template_type") or template_type,
            "template_id":   account.template_id,
            "api_key":       account.api_key,
            "account":       account.name,
        }
        for k, v in current.items():
            if k in merged:
                continue
            merged[k] = v

        try:
            fp.write_text(
                json.dumps(merged, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            log.error("[CM-ACCT] Konnte %s nicht schreiben: %s", fp.name, exc)
            return None

    log.info(
        "[CM-ACCT] 📝 Sieger-Account persistiert: template_type=%s account=%s "
        "template_id=%s file=%s",
        template_type, account.name, account.template_id, fp.name,
    )
    return fp


# ── Prozess-lokale Skip-Liste fuer aufgebrauchte Keys ────────────────────────
_SKIP_LOCK = threading.Lock()
_SKIPPED_KEYS: set[str] = set()


def mark_key_exhausted(api_key: str, reason: str = "") -> None:
    """Markiert einen api_key als "kein Credit" fuer den Rest dieser Laufzeit."""
    if not api_key:
        return
    with _SKIP_LOCK:
        _SKIPPED_KEYS.add(api_key)
    log.warning(
        "[CM-ACCT] 💸 Key markiert als aufgebraucht (%s): %s",
        reason or "no-credit",
        _mask(api_key),
    )


def is_key_exhausted(api_key: str) -> bool:
    with _SKIP_LOCK:
        return api_key in _SKIPPED_KEYS


def build_fallback_chain(
    kind: str,
    *,
    exclude_keys: set[str] | None = None,
) -> list[CreatomateAccount]:
    """
    Fallback-Reihenfolge fuer eine Kategorie.

    * Schliesst alle ``exclude_keys`` aus (typischerweise der aktuell aktive
      Key, der gerade 402 geliefert hat).
    * Schliesst alle prozess-weit als "aufgebraucht" markierten Keys aus.
    """
    excl = set(exclude_keys or ())
    return [
        a for a in load_accounts(kind)
        if a.api_key not in excl and not is_key_exhausted(a.api_key)
    ]


def _mask(key: str, head: int = 6, tail: int = 4) -> str:
    if len(key) <= head + tail:
        return "***"
    return f"{key[:head]}…{key[-tail:]}"
