"""
Fixer: in Worker-Dateien, die sys.path-Manipulationen enthalten, soll der
Logger-Import nach dem sys.path-Block stehen (sonst ModuleNotFoundError beim
direkten Start eines Workers).

Strategie:
  1. Datei lesen.
  2. Erstes Vorkommen von 'from core.logging import get_logger' merken.
  3. Letztes Vorkommen einer sys.path-Mutation suchen
     (sys.path.append / sys.path.insert).
  4. Wenn (3) > (1), die zwei Logger-Zeilen (import + get_logger(...))
     ausschneiden und direkt NACH dem sys.path-Block einfügen.
"""
from __future__ import annotations
import re
import sys
from pathlib import Path


_IMPORT_LINE_RE = re.compile(r"^\s*from core\.logging import get_logger\b.*$")
_LOG_VAR_RE = re.compile(r"^\s*log\s*=\s*get_logger\(.*\).*$")
_SYS_PATH_RE = re.compile(r"^\s*sys\.path\.(append|insert)\b")


def _fix(path: Path) -> bool:
    src = path.read_text(encoding="utf-8")
    lines = src.splitlines(keepends=True)

    import_idx = None
    var_idx = None
    last_syspath_idx = None
    for i, line in enumerate(lines):
        if import_idx is None and _IMPORT_LINE_RE.match(line):
            import_idx = i
        elif var_idx is None and _LOG_VAR_RE.match(line):
            var_idx = i
        if _SYS_PATH_RE.match(line):
            last_syspath_idx = i

    if import_idx is None or var_idx is None or last_syspath_idx is None:
        return False
    if last_syspath_idx < import_idx:
        return False  # bereits korrekt

    # Bestimme den Bereich der Logger-Zeilen (import + var, kann unmittelbar
    # aufeinander folgen oder durch eine Leerzeile getrennt sein).
    block_start = import_idx
    block_end = var_idx + 1  # exklusiv
    moved_block = lines[block_start:block_end]

    # Entferne den Block
    remaining = lines[:block_start] + lines[block_end:]
    # Index des letzten sys.path-Eintrags nach Entfernung neu berechnen
    new_last_syspath_idx = last_syspath_idx - (block_end - block_start)
    insert_at = new_last_syspath_idx + 1

    # Optional Leerzeile davor, wenn nicht schon eine da ist
    sep = []
    if insert_at < len(remaining) and remaining[insert_at].strip():
        sep = ["\n"]
    new_lines = remaining[:insert_at] + sep + moved_block + remaining[insert_at:]
    new_src = "".join(new_lines)
    if new_src == src:
        return False
    path.write_text(new_src, encoding="utf-8")
    print(f"  ✓ moved logger import in {path}")
    return True


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: fix_logger_import_order <dir> [...]")
        return 1
    targets: list[Path] = []
    for a in argv:
        p = Path(a)
        if p.is_dir():
            targets.extend(p.rglob("*.py"))
        elif p.is_file() and p.suffix == ".py":
            targets.append(p)
    skip = ("/.venv/", "/core/", "/_archive_migration/")
    n = 0
    for p in targets:
        if any(x in str(p.as_posix()) for x in skip):
            continue
        if _fix(p):
            n += 1
    print(f"\nDone. {n} files fixed.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
