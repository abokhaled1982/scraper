#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Daemon-Worker, der einzelne HTML-Dateien verarbeitet und die Ergebnisse schreibt.
WICHTIG: Hier wird die Ausgabe jetzt auf das BO…-Schema gemappt (to_b0_schema),
damit die Struktur exakt so aussieht wie in deiner B0DSLBN5FS.json.
"""

from __future__ import annotations
import hashlib, json, time, traceback, shutil
from pathlib import Path
from typing import Optional, Tuple
import sys

# Projekt-Config
sys.path.append(str(Path(__file__).resolve().parent.parent))
from config import PRODUCKT_DIR, OUT_DIR, FAILED_DIR, INTERVAL_SECS, REGISTRY_PATH, SUMMARY_PATH

# Parser + Mapping
from parser import AmazonProductParser, to_b0_schema


# ----------------------------- Helpers --------------------------------------

def _read_text(fp: Path) -> str:
    try:
        return fp.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return fp.read_bytes().decode("utf-8", errors="ignore")

def _sha1_bytes(b: bytes) -> str:
    return hashlib.sha1(b).hexdigest()

def _sha1_file(fp: Path) -> str:
    try:
        return _sha1_bytes(fp.read_bytes())
    except Exception:
        return _sha1_bytes(_read_text(fp).encode("utf-8", errors="ignore"))

def _load_registry() -> dict:
    if REGISTRY_PATH.exists():
        try:
            return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"asins": {}, "hashes": {}}

def _save_registry(reg: dict) -> None:
    tmp = REGISTRY_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(reg, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(REGISTRY_PATH)

def _pick_oldest_html() -> Optional[Path]:
    files = [p for p in PRODUCKT_DIR.glob("*.html")]
    if not files:
        return None
    files.sort(key=lambda p: p.stat().st_mtime)
    return files[0]

def _out_name(asin: Optional[str], src: Path, page_hash: str) -> str:
    """
    Für konsistente Filenamen weiterhin ASIN.json bevorzugen.
    (Die INHALTE sind im BO-Schema, nicht der Dateiname.)
    """
    return f"{asin}.json" if asin else f"{src.stem}.{page_hash[:8]}.json"

def _write_summary_append(obj: dict) -> None:
    with SUMMARY_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")

def _move_to_failed(src: Path, err: str) -> None:
    FAILED_DIR.mkdir(parents=True, exist_ok=True)
    dst = FAILED_DIR / src.name
    try:
        shutil.move(str(src), str(dst))
    except Exception:
        try:
            shutil.copy2(str(src), str(dst))
            src.unlink(missing_ok=True)
        except Exception:
            pass
    (dst.with_suffix(dst.suffix + ".error.txt")).write_text(err, encoding="utf-8")


# ----------------------------- Core -----------------------------------------

def process_one(fp: Path, reg: dict) -> Tuple[bool, str]:
    """
    Liest eine HTML-Datei, parsed sie, mappt auf BO-Schema und persistiert JSON.
    Dedupe: per Seiten-Hash und ASIN.
    """
    try:
        raw = _read_text(fp)
        page_hash = _sha1_file(fp)

        # Dedupe über Seiten-Hash
        if page_hash in reg["hashes"]:
            fp.unlink(missing_ok=True)
            return True, f"SKIP hash={page_hash[:8]} already processed"

        parser = AmazonProductParser(raw)
        product = parser.parse()
        # Quelle notieren (optional – wird im Mapping NICHT ausgegeben)
        # product._source_file = str(fp.resolve())

        asin = getattr(product, "asin", None)
        out_path = OUT_DIR / _out_name(asin, fp, page_hash)

        # Dedupe über ASIN (falls vorhanden)
        if asin and asin in reg["asins"]:
            fp.unlink(missing_ok=True)
            return True, f"SKIP asin={asin} already present"

        # *** HIER die entscheidende Änderung: BO…-Schema erzeugen ***
        data_mapped = to_b0_schema(product)

        # Schreiben (atomic)
        tmp = out_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data_mapped, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(out_path)

        # Summary-Log im BO-Schema, damit alles konsistent bleibt
        _write_summary_append(data_mapped)

        # Registry aktualisieren
        reg["hashes"][page_hash] = out_path.name
        if asin:
            reg["asins"][asin] = out_path.name
        _save_registry(reg)

        # Quelle löschen (verarbeitet)
        fp.unlink(missing_ok=True)
        return True, f"OK -> {out_path.name}"

    except Exception as e:
        tb = traceback.format_exc()
        _move_to_failed(fp, f"{e}\n\n{tb}")
        return False, f"ERR {fp.name}: {e}"


def daemon_loop(interval: int = INTERVAL_SECS) -> None:
    """
    Watch-Loop: zieht regelmäßig die älteste HTML-Datei und verarbeitet sie.
    """
    print(f"[product-parser] watching {PRODUCKT_DIR} every {interval}s -> {OUT_DIR}")
    reg = _load_registry()
    while True:
        try:
            fp = _pick_oldest_html()
            if not fp:
                time.sleep(interval)
                continue
            ok, msg = process_one(fp, reg)
            print(f"[product-parser] {msg}")
            time.sleep(0.1 if ok else 1.0)
        except KeyboardInterrupt:
            print("[product-parser] stopped by user")
            break
        except Exception:
            print("[product-parser] loop error; sleep 1s")
            traceback.print_exc()
            time.sleep(1)


if __name__ == "__main__":
    daemon_loop()
