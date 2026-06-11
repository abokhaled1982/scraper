"""
Parst alle Creatomate-Account-Dateien unter ``creatomate/accounts/*.txt``.

Jede Datei ist ein ``curl``-Beispiel, das von Creatomate exportiert wurde und
genau einen ``Authorization: Bearer <api_key>`` plus einen JSON-Body mit
``template_id`` enthaelt.

Dateinamen-Konvention (alle Varianten werden unterstuetzt):
    <account>[_:]all.txt        -> kind = "all"
    <account>[_:]fashon.txt     -> kind = "fashion"   (auch "fashion" Schreibweise)

Beispiele:
    sara_all.txt                  -> account="sara",            kind="all"
    sara_fashon.txt               -> account="sara",            kind="fashion"
    hanan_yahoo:fashon.txt        -> account="hanan_yahoo",     kind="fashion"
    walghobari_yahho_all.txt      -> account="walghobari_yahho", kind="all"

Public API:
    parse_account_file(path)  -> dict | None
    parse_accounts_dir(path)  -> list[dict]
    group_by_kind(records)    -> dict[str, list[dict]]

CLI:
    python -m creatomate.scripts.parse_accounts
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

ACCOUNTS_DIR = Path(__file__).resolve().parent.parent / "accounts"

# Reihenfolge ist relevant: laengster Suffix zuerst, damit "fashion" nicht als "all" matcht.
_KIND_SUFFIXES: tuple[tuple[str, str], ...] = (
    ("fashion", "fashion"),
    ("fashon",  "fashion"),
    ("all",     "all"),
)

_BEARER_RE   = re.compile(r"Authorization:\s*Bearer\s+([A-Za-z0-9_\-]+)", re.IGNORECASE)
_JSON_BLOCK  = re.compile(r"-d\s+'(?P<body>\{.*?\})'\s*$", re.DOTALL)
_TEMPLATE_RE = re.compile(r'"template_id"\s*:\s*"([0-9a-fA-F\-]+)"')


@dataclass
class AccountRecord:
    """Ein geparster Creatomate-Account (eine Datei = ein Eintrag)."""
    account_name: str
    kind: str            # "all" | "fashion"
    template_id: str
    api_key: str
    source_file: str     # relativer Pfad fuer Debugging


def _split_name_and_kind(stem: str) -> tuple[str, str] | None:
    """Trennt 'sara_fashon' -> ('sara', 'fashion'). Trennt auch ':' Separator."""
    lower = stem.lower()
    for suffix, normalized in _KIND_SUFFIXES:
        # akzeptiere _suffix und :suffix am Ende
        for sep in ("_", ":"):
            marker = f"{sep}{suffix}"
            if lower.endswith(marker):
                account = stem[: len(stem) - len(marker)]
                return account, normalized
        # Edge-Case: nur der Suffix ohne Trenner (selten, aber sicher)
        if lower == suffix:
            return stem, normalized
    return None


def parse_account_file(path: Path) -> AccountRecord | None:
    """Parst eine einzelne Account-curl-Datei. Gibt ``None`` bei unparsbaren Dateien."""
    if not path.is_file():
        return None

    text = path.read_text(encoding="utf-8", errors="replace")

    bearer = _BEARER_RE.search(text)
    if not bearer:
        return None
    api_key = bearer.group(1).strip()

    # Template-ID: zuerst aus JSON-Block, sonst direkter Regex (curl mit -d "..." statt -d '...')
    tpl_id = ""
    body_match = _JSON_BLOCK.search(text)
    if body_match:
        body_text = body_match.group("body")
        try:
            body = json.loads(body_text)
            tpl_id = str(body.get("template_id") or "").strip()
        except json.JSONDecodeError:
            pass
    if not tpl_id:
        m = _TEMPLATE_RE.search(text)
        if m:
            tpl_id = m.group(1).strip()
    if not tpl_id:
        return None

    split = _split_name_and_kind(path.stem)
    if split is None:
        return None
    account_name, kind = split

    return AccountRecord(
        account_name=account_name,
        kind=kind,
        template_id=tpl_id,
        api_key=api_key,
        source_file=str(path.relative_to(ACCOUNTS_DIR.parent)),
    )


def parse_accounts_dir(directory: Path | None = None) -> list[AccountRecord]:
    """Parst alle ``*.txt`` Dateien im Accounts-Ordner und sortiert alphabetisch."""
    directory = Path(directory) if directory else ACCOUNTS_DIR
    if not directory.is_dir():
        return []
    records: list[AccountRecord] = []
    for fp in sorted(directory.iterdir()):
        if fp.suffix.lower() != ".txt":
            continue
        rec = parse_account_file(fp)
        if rec:
            records.append(rec)
    return sorted(records, key=lambda r: (r.kind, r.account_name.lower()))


def group_by_kind(records: Iterable[AccountRecord]) -> dict[str, list[AccountRecord]]:
    """Gruppiert die geparsten Records nach ``kind`` (``all`` / ``fashion``)."""
    out: dict[str, list[AccountRecord]] = {"all": [], "fashion": []}
    for r in records:
        out.setdefault(r.kind, []).append(r)
    return out


def _mask(key: str, head: int = 6, tail: int = 4) -> str:
    if len(key) <= head + tail:
        return "***"
    return f"{key[:head]}…{key[-tail:]}"


def _main() -> int:
    records = parse_accounts_dir()
    if not records:
        print(f"⚠️  Keine parsbaren Account-Dateien in {ACCOUNTS_DIR}")
        return 1

    grouped = group_by_kind(records)
    print(f"✅ {len(records)} Accounts geparst aus {ACCOUNTS_DIR}\n")
    for kind in ("all", "fashion"):
        items = grouped.get(kind) or []
        print(f"── kind = {kind!r}  ({len(items)} Account(s)) ─────────────────────")
        for r in items:
            print(
                f"  • {r.account_name:<20}  "
                f"template_id={r.template_id}  "
                f"api_key={_mask(r.api_key)}  "
                f"({r.source_file})"
            )
        print()

    # JSON-Dump fuer Pipe-Verarbeitung
    print("──── JSON ────")
    print(json.dumps([asdict(r) for r in records], indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
