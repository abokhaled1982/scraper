#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
creatomate/scripts/fallback_test.py
====================================

Test-Skript für den **Creatomate Account-Fallback**.

Zweck
-----
Bevor wir den Fallback fest in ``core/workers/facebook/reels_service.py``
einbauen, validieren wir hier isoliert:

  1. Parsen aller Accounts unter ``creatomate/accounts/*.txt``
     (inkl. ``expired.txt`` als "abgelaufenes" Konto, das den Switch auslöst).
  2. Pro Kategorie (``all`` / ``fashion``) der Reihe nach POSTen wir an
     ``https://api.creatomate.com/v2/renders`` mit dem Bearer-Key und
     der ``template_id`` des Accounts.
  3. Erkennen wir "keine Credits / Plan abgelaufen" (HTTP 402 oder
     entsprechende Fehlermeldung), wandern wir zum **nächsten** Account.
  4. Sobald ein Account erfolgreich rendert, schreiben wir dessen
     ``api_key`` + ``template_id`` als Top-Level-Felder in
     ``creatomate/all.json`` bzw. ``creatomate/fashion.json``.

Wichtig
-------
* ``expired.txt`` wird beim ``all``-Lauf an die **erste Stelle** gesetzt, damit
  der Fallback-Pfad aktiv getriggert wird. (Die ``template_id`` aus
  ``expired.txt`` gehört zur ``all``-Kategorie.)
* Das Skript ändert **NICHT** den Worker/Service-Code. Es liest nur die
  Account-Dateien und aktualisiert ``all.json`` / ``fashion.json``.
* ``--dry`` rendert nicht wirklich – es prüft nur, dass der POST von
  Creatomate **angenommen** wird (kein Credit-Verbrauch beim 422-Fall, beim
  Erfolg fällt aber eine Render-Einheit an).

CLI
---
    # Beides testen (all + fashion), echte Renders, all.json/fashion.json updaten
    python -m creatomate.scripts.fallback_test

    # Nur 'all' testen
    python -m creatomate.scripts.fallback_test --kind all

    # Ohne JSON-Update
    python -m creatomate.scripts.fallback_test --no-write

    # Nur den POST-Schritt (kein Polling), trotzdem werden Credits verbraucht
    # falls der POST akzeptiert wird – nutze --no-write zusätzlich, wenn nichts
    # persistiert werden soll.
    python -m creatomate.scripts.fallback_test --no-poll
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import requests

# Eigene Imports (Account-Parser wiederverwenden) -------------------------------
HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from creatomate.scripts.parse_accounts import (  # noqa: E402
    AccountRecord,
    parse_accounts_dir,
)

# ── Konstanten ────────────────────────────────────────────────────────────────
CREATOMATE_DIR = PROJECT_ROOT / "creatomate"
ACCOUNTS_DIR   = CREATOMATE_DIR / "accounts"
ALL_JSON       = CREATOMATE_DIR / "all.json"
FASHION_JSON   = CREATOMATE_DIR / "fashion.json"
EXPIRED_FILE   = ACCOUNTS_DIR / "expired.txt"

API_URL   = "https://api.creatomate.com/v2/renders"
POLL_EVERY = 5         # Sek
POLL_MAX   = 300       # Sek

# Modifications werden direkt aus den curl-Bodies der .txt-Dateien gezogen,
# damit jeder Account exakt das rendert, was Creatomate ihm beim Export
# vorgeschlagen hat (keine Format-Mismatches).
_JSON_BLOCK = re.compile(r"-d\s+'(?P<body>\{.*?\})'\s*$", re.DOTALL)
_BEARER_RE  = re.compile(r"Authorization:\s*Bearer\s+([A-Za-z0-9_\-]+)", re.IGNORECASE)


# ── Farb-Helpers ─────────────────────────────────────────────────────────────
def _c(s: str, code: str) -> str: return f"\033[{code}m{s}\033[0m"
def _bold(s: str)   -> str: return _c(s, "1")
def _green(s: str)  -> str: return _c(s, "32")
def _cyan(s: str)   -> str: return _c(s, "36")
def _yellow(s: str) -> str: return _c(s, "33")
def _red(s: str)    -> str: return _c(s, "31")
def _grey(s: str)   -> str: return _c(s, "90")


def _mask(key: str, head: int = 6, tail: int = 4) -> str:
    if len(key) <= head + tail:
        return "***"
    return f"{key[:head]}…{key[-tail:]}"


# ── Account-Record + Modifications-Parsing ────────────────────────────────────
@dataclass
class TestAccount:
    """Account-Eintrag mit zusätzlichen Modifications für den Render-Test."""
    name: str
    kind: str          # "all" | "fashion"
    api_key: str
    template_id: str
    modifications: dict
    source_file: str
    expired_marker: bool = False   # True für die expired.txt


def _extract_modifications(text: str) -> dict:
    """Holt das ``modifications``-Dict aus dem -d '...'-Block einer curl-Datei."""
    m = _JSON_BLOCK.search(text)
    if not m:
        return {}
    try:
        body = json.loads(m.group("body"))
    except json.JSONDecodeError:
        return {}
    mods = body.get("modifications")
    return mods if isinstance(mods, dict) else {}


def _account_from_record(rec: AccountRecord) -> TestAccount | None:
    src = ACCOUNTS_DIR / Path(rec.source_file).name
    if not src.is_file():
        return None
    mods = _extract_modifications(src.read_text(encoding="utf-8", errors="replace"))
    if not mods:
        return None
    return TestAccount(
        name=rec.account_name,
        kind=rec.kind,
        api_key=rec.api_key,
        template_id=rec.template_id,
        modifications=mods,
        source_file=rec.source_file,
    )


def _expired_account() -> TestAccount | None:
    """Parst ``expired.txt`` als Pseudo-Account (ohne Kind-Suffix im Dateinamen)."""
    if not EXPIRED_FILE.is_file():
        return None
    text = EXPIRED_FILE.read_text(encoding="utf-8", errors="replace")
    bearer = _BEARER_RE.search(text)
    body_match = _JSON_BLOCK.search(text)
    if not bearer or not body_match:
        return None
    try:
        body = json.loads(body_match.group("body"))
    except json.JSONDecodeError:
        return None
    tpl_id = str(body.get("template_id") or "").strip()
    mods   = body.get("modifications") or {}
    if not tpl_id or not isinstance(mods, dict):
        return None
    # Heuristik: Welche Kategorie? "all" hat Product-/Normal-/Discounted-Felder,
    # "fashion" hat Background-/Discount-/Title-Felder.
    keys = set(mods.keys())
    if {"Product-Name.text", "Normal-Price.text", "Discounted-Price.text"} & keys:
        kind = "all"
    elif {"Background-Image.source", "Discount.text", "Title.text"} & keys:
        kind = "fashion"
    else:
        kind = "all"  # konservativer Default
    return TestAccount(
        name="expired",
        kind=kind,
        api_key=bearer.group(1).strip(),
        template_id=tpl_id,
        modifications=mods,
        source_file="accounts/expired.txt",
        expired_marker=True,
    )


def _list_unparsable_files(kind: str, parsed_names: set[str]) -> list[Path]:
    """
    Findet ``*_<kind>.txt`` / ``*_<kind_alt>.txt`` Dateien, die nicht als
    Account-Record geparst werden konnten (z.B. weil sie ein nacktes
    Template-JSON statt einer curl-Zeile enthalten).
    """
    suffixes = ("_all",) if kind == "all" else ("_fashion", "_fashon")
    unparsed: list[Path] = []
    for fp in sorted(ACCOUNTS_DIR.glob("*.txt")):
        stem_lower = fp.stem.lower()
        if not any(stem_lower.endswith(s) for s in suffixes):
            continue
        # Account-Name vor dem Suffix berechnen.
        matched_suffix = next(s for s in suffixes if stem_lower.endswith(s))
        account = fp.stem[: -len(matched_suffix)]
        if account in parsed_names:
            continue
        unparsed.append(fp)
    return unparsed


def build_account_chain(kind: str) -> tuple[list[TestAccount], list[Path]]:
    """
    Baut die Fallback-Kette für eine Kategorie.

    Reihenfolge:
        1. ``expired.txt`` (wenn passend für diese Kategorie) → triggert Fallback.
        2. Restliche Accounts alphabetisch (so wie parse_accounts_dir sie liefert).

    Returns:
        (chain, skipped_files) – ``skipped_files`` listet Dateien, die zum
        Kind passen, aber nicht parsebar waren (kaputtes Format).
    """
    chain: list[TestAccount] = []

    # 1) Expired vorne anhängen, wenn es für diese Kategorie gilt.
    exp = _expired_account()
    if exp and exp.kind == kind:
        chain.append(exp)

    # 2) Reguläre Accounts.
    parsed_names: set[str] = set()
    for rec in parse_accounts_dir(ACCOUNTS_DIR):
        if rec.kind != kind:
            continue
        acc = _account_from_record(rec)
        if acc:
            chain.append(acc)
            parsed_names.add(acc.name)

    skipped = _list_unparsable_files(kind, parsed_names)
    return chain, skipped


# ── Fehlererkennung "keine Credits" ───────────────────────────────────────────
# Stichwörter, die Creatomate (oder ein generischer 4xx-Body) bei
# "keine Credits / Plan abgelaufen / Trial aus" verwendet.
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


def _is_no_credit_error(status_code: int, body_text: str) -> bool:
    """Heuristik: war das ein "keine Credits"-Fehler?"""
    if status_code == 402:    # Payment Required
        return True
    if status_code in (401, 403):
        low = (body_text or "").lower()
        # 401/403 + Credit/Plan-Keyword im Body → Credit-Problem.
        if any(k in low for k in _NO_CREDIT_KEYWORDS):
            return True
    if status_code >= 400 and status_code < 500:
        low = (body_text or "").lower()
        if any(k in low for k in _NO_CREDIT_KEYWORDS):
            return True
    return False


# ── Render-Versuch pro Account ────────────────────────────────────────────────
@dataclass
class RenderOutcome:
    ok: bool
    account: TestAccount
    status_code: int | None = None
    error: str | None = None
    no_credit: bool = False
    render_id: str | None = None
    url: str | None = None
    elapsed_s: float = 0.0


def try_render(
    account: TestAccount,
    *,
    poll: bool = True,
    poll_max: int = POLL_MAX,
) -> RenderOutcome:
    """Postet einen Render-Job und (optional) wartet auf das Ergebnis."""
    t0 = time.time()
    payload = {
        "template_id":   account.template_id,
        "modifications": account.modifications,
    }
    headers = {
        "Content-Type":  "application/json",
        "Authorization": f"Bearer {account.api_key}",
    }

    try:
        resp = requests.post(API_URL, headers=headers, json=payload, timeout=120)
    except requests.RequestException as e:
        return RenderOutcome(
            ok=False, account=account,
            error=f"Netzwerkfehler: {e}",
            elapsed_s=time.time() - t0,
        )

    body_text = resp.text or ""
    if resp.status_code >= 400:
        no_credit = _is_no_credit_error(resp.status_code, body_text)
        return RenderOutcome(
            ok=False,
            account=account,
            status_code=resp.status_code,
            error=body_text[:600],
            no_credit=no_credit,
            elapsed_s=time.time() - t0,
        )

    # POST akzeptiert – Render-ID extrahieren.
    try:
        data = resp.json()
    except ValueError:
        return RenderOutcome(
            ok=False, account=account, status_code=resp.status_code,
            error="Antwort war kein gültiges JSON.",
            elapsed_s=time.time() - t0,
        )
    render = data[0] if isinstance(data, list) and data else data
    render_id = render.get("id") if isinstance(render, dict) else None
    if not render_id:
        return RenderOutcome(
            ok=False, account=account, status_code=resp.status_code,
            error=f"Keine Render-ID in Antwort: {str(render)[:300]}",
            elapsed_s=time.time() - t0,
        )

    if not poll:
        return RenderOutcome(
            ok=True, account=account, status_code=resp.status_code,
            render_id=render_id,
            url=str(render.get("url") or ""),
            elapsed_s=time.time() - t0,
        )

    # Polling bis fertig.
    status_url = f"{API_URL}/{render_id}"
    deadline = time.time() + poll_max
    last_status = ""
    while time.time() < deadline:
        try:
            sr = requests.get(status_url, headers=headers, timeout=15)
        except requests.RequestException as e:
            return RenderOutcome(
                ok=False, account=account, render_id=render_id,
                error=f"Polling-Fehler: {e}",
                elapsed_s=time.time() - t0,
            )
        if sr.status_code >= 400:
            return RenderOutcome(
                ok=False, account=account, render_id=render_id,
                status_code=sr.status_code,
                error=f"Polling HTTP {sr.status_code}: {sr.text[:300]}",
                elapsed_s=time.time() - t0,
            )
        sd = sr.json()
        last_status = str(sd.get("status") or "")
        if last_status == "succeeded":
            return RenderOutcome(
                ok=True, account=account, render_id=render_id,
                url=str(sd.get("url") or ""),
                elapsed_s=time.time() - t0,
            )
        if last_status in ("failed", "error"):
            return RenderOutcome(
                ok=False, account=account, render_id=render_id,
                error=f"Render-Status: {last_status} – {str(sd.get('error') or sd)[:300]}",
                elapsed_s=time.time() - t0,
            )
        time.sleep(POLL_EVERY)

    return RenderOutcome(
        ok=False, account=account, render_id=render_id,
        error=f"Polling-Timeout nach {poll_max}s (zuletzt: {last_status!r})",
        elapsed_s=time.time() - t0,
    )


# ── Fallback-Kette durchlaufen ────────────────────────────────────────────────
def run_fallback_chain(
    chain: Iterable[TestAccount],
    *,
    poll: bool = True,
) -> RenderOutcome | None:
    """Geht die Accounts der Reihe nach durch – first-success-wins."""
    last: RenderOutcome | None = None
    for idx, acc in enumerate(chain, start=1):
        tag = _yellow("EXPIRED") if acc.expired_marker else _grey(f"#{idx}")
        print(_bold(
            f"\n── [{acc.kind}] Versuch {idx}: {tag} "
            f"account={acc.name!r} template_id={acc.template_id} "
            f"key={_mask(acc.api_key)} ──"
        ))
        out = try_render(acc, poll=poll)
        last = out

        if out.ok:
            print(_green(
                f"  ✅ OK in {out.elapsed_s:.1f}s | render_id={out.render_id} "
                f"url={out.url}"
            ))
            return out

        if out.no_credit:
            print(_yellow(
                f"  💸 Keine Credits / Plan aus (HTTP {out.status_code}) "
                f"→ wechsle zum nächsten Account."
            ))
            if out.error:
                print(_grey(f"     Body: {out.error[:200]}"))
            continue

        # Anderer Fehler – Kette abbrechen (sonst verbrennen wir nur Credits).
        print(_red(
            f"  ❌ Fehler (kein Credit-Problem) HTTP={out.status_code}: "
            f"{(out.error or '')[:200]}"
        ))
        print(_red("     → Kette abgebrochen, kein weiterer Account wird versucht."))
        return out

    return last


# ── all.json / fashion.json mit aktivem Key+Template ergänzen ────────────────
def update_kind_json(kind: str, account: TestAccount) -> Path | None:
    """
    Fügt ``api_key`` + ``template_id`` als Top-Level-Felder in das jeweilige
    Template-JSON ein. Behält die Reihenfolge bei (neue Felder ganz oben).
    """
    target = ALL_JSON if kind == "all" else FASHION_JSON
    if not target.is_file():
        print(_red(f"  ⚠️  {target} existiert nicht – Update übersprungen."))
        return None

    try:
        original = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(_red(f"  ⚠️  {target.name} ist kein valides JSON ({e}) – Update übersprungen."))
        return None
    if not isinstance(original, dict):
        print(_red(f"  ⚠️  {target.name} ist kein Objekt auf Top-Level – Update übersprungen."))
        return None

    # Neue Reihenfolge: api_key + template_id zuerst, alle alten Keys danach
    # (api_key/template_id werden überschrieben, falls schon vorhanden).
    new_obj: dict = {}
    new_obj["api_key"]     = account.api_key
    new_obj["template_id"] = account.template_id
    new_obj["account"]     = account.name
    for k, v in original.items():
        if k in ("api_key", "template_id", "account"):
            continue
        new_obj[k] = v

    target.write_text(json.dumps(new_obj, indent=2, ensure_ascii=False) + "\n",
                      encoding="utf-8")
    return target


# ── CLI ──────────────────────────────────────────────────────────────────────
def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="fallback_test",
        description="Creatomate Account-Fallback-Test (read-only Worker/Services).",
    )
    p.add_argument(
        "--kind", choices=["all", "fashion", "both"], default="both",
        help="Welche Kategorie testen? (default: both)",
    )
    p.add_argument(
        "--no-poll", action="store_true",
        help="Nur POST testen, nicht auf Render-Fertigstellung warten "
             "(Credits werden trotzdem reserviert, sobald POST akzeptiert wird).",
    )
    p.add_argument(
        "--no-write", action="store_true",
        help="all.json / fashion.json NICHT aktualisieren – nur Report ausgeben.",
    )
    return p


def _print_chain_preview(
    kind: str,
    chain: list[TestAccount],
    skipped: list[Path],
) -> None:
    print(_bold(f"\n📋 Fallback-Kette für kind={kind!r}  ({len(chain)} Account(s))"))
    if not chain:
        print(_yellow("   (keine Accounts gefunden)"))
    else:
        for i, a in enumerate(chain, start=1):
            marker = _yellow(" [EXPIRED]") if a.expired_marker else ""
            print(
                f"  {i:>2}. {a.name:<18} template_id={a.template_id}  "
                f"key={_mask(a.api_key)}{marker}"
            )
    if skipped:
        print(_yellow(f"  ⚠️  {len(skipped)} Datei(en) übersprungen (kein curl-Format):"))
        for fp in skipped:
            print(_yellow(f"      - {fp.relative_to(PROJECT_ROOT)}"))


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    kinds = ["all", "fashion"] if args.kind == "both" else [args.kind]

    print(_bold(_cyan("\n═══ Creatomate Fallback-Test ═══")))
    print(f"  Accounts-Dir : {ACCOUNTS_DIR}")
    print(f"  all.json     : {ALL_JSON}")
    print(f"  fashion.json : {FASHION_JSON}")
    print(f"  Poll         : {'NEIN (--no-poll)' if args.no_poll else 'ja'}")
    print(f"  Write        : {'NEIN (--no-write)' if args.no_write else 'ja'}")

    summary: dict[str, RenderOutcome | None] = {}

    for kind in kinds:
        chain, skipped = build_account_chain(kind)
        _print_chain_preview(kind, chain, skipped)
        if not chain:
            summary[kind] = None
            continue

        result = run_fallback_chain(chain, poll=not args.no_poll)
        summary[kind] = result

        if result and result.ok and not args.no_write:
            updated = update_kind_json(kind, result.account)
            if updated:
                print(_green(
                    f"  📝 {updated.name} aktualisiert "
                    f"(api_key + template_id + account = {result.account.name!r})"
                ))

    # Schluss-Report
    print(_bold(_cyan("\n═══ Zusammenfassung ═══")))
    exit_code = 0
    for kind in kinds:
        res = summary.get(kind)
        if res is None:
            print(f"  • {kind:<8}: {_yellow('keine Accounts')}")
            exit_code = max(exit_code, 1)
        elif res.ok:
            print(
                f"  • {kind:<8}: {_green('OK')} "
                f"via account={res.account.name!r} "
                f"({_mask(res.account.api_key)}) "
                f"url={res.url or '-'}"
            )
        else:
            tag = _yellow("KEIN CREDIT") if res.no_credit else _red("FEHLER")
            print(
                f"  • {kind:<8}: {tag} "
                f"(letzter Versuch: {res.account.name!r}, HTTP={res.status_code})"
            )
            exit_code = max(exit_code, 2)

    return exit_code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print(_yellow("\nAbgebrochen durch Benutzer."))
        sys.exit(130)
