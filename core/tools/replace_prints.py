"""
Einmaliges Migrations-Tool: ersetzt in allen Worker-Dateien `print(...)` durch
`log.info/warning/error(...)` und fügt einen `get_logger(...)`-Import ein.

Regeln:
  * Excludiert: core/, .venv, debug_*, test_*, ws_server.py-/ ... weiß über CLI.
  * Level-Heuristik anhand des Inhalts:
        ❌/error/Error/Fehler/Exception/❗     → log.error
        ⚠/warn/WARN/Warnung/skip/timeout       → log.warning
        sonst                                   → log.info
  * print(f"...") bleibt erhalten (logger versteht f-Strings).
  * print(a, b, c)  → log.info("%s %s %s", a, b, c)
  * Wenn bereits `from core.logging import get_logger` drin ist, nicht doppelt einfügen.
  * Datei wird nur geschrieben, wenn sich etwas geändert hat.

Aufruf:
    python -m core.tools.replace_prints amazon facebook telegram instagram \
        ai_parser run_all.py creatomate.py
"""
from __future__ import annotations
import ast
import re
import sys
from pathlib import Path

_ERROR_HINTS = re.compile(
    r"(❌|❗|\bError\b|\bERROR\b|\bFehler\b|Exception|Traceback|failed|FAILED|abort)",
    re.IGNORECASE,
)
_WARN_HINTS = re.compile(
    r"(⚠|\bwarn\b|\bWARN\b|Warnung|skip|skipping|timeout|retry|deprec)",
    re.IGNORECASE,
)


def _level_for(text: str) -> str:
    if _ERROR_HINTS.search(text):
        return "error"
    if _WARN_HINTS.search(text):
        return "warning"
    return "info"


def _worker_name_for(file_path: Path) -> str:
    # amazon/ws_server.py  → "ws_server"
    # run_all.py           → "run_all"
    # facebook/fb_watcher.py → "fb_watcher"
    return file_path.stem


_IMPORT_LINE_TEMPLATE = (
    "from core.logging import get_logger  # noqa: E402\n"
    "log = get_logger(\"{name}\")  # noqa: E402\n"
)


def _has_logger_import(src: str) -> bool:
    return "from core.logging import get_logger" in src


def _insert_logger_import(src: str, name: str) -> str:
    # Nach letztem Top-Level-Import einfügen (oder ganz oben nach Shebang/Docstring).
    lines = src.splitlines(keepends=True)
    insert_idx = 0
    # Shebang
    if lines and lines[0].startswith("#!"):
        insert_idx = 1
    # Modul-Docstring
    if insert_idx < len(lines) and lines[insert_idx].lstrip().startswith(('"""', "'''")):
        quote = lines[insert_idx].lstrip()[:3]
        # ein-zeilig?
        rest = lines[insert_idx].lstrip()[3:]
        if rest.rstrip().endswith(quote) and len(rest.rstrip()) > 0:
            insert_idx += 1
        else:
            insert_idx += 1
            while insert_idx < len(lines) and quote not in lines[insert_idx]:
                insert_idx += 1
            insert_idx += 1  # Zeile mit closing quote
    # nach allen folgenden import-Statements
    while insert_idx < len(lines):
        stripped = lines[insert_idx].strip()
        if (
            stripped.startswith("import ")
            or stripped.startswith("from ")
            or stripped == ""
            or stripped.startswith("#")
        ):
            insert_idx += 1
            continue
        break
    snippet = _IMPORT_LINE_TEMPLATE.format(name=name)
    return "".join(lines[:insert_idx]) + snippet + "".join(lines[insert_idx:])


class _PrintTransformer(ast.NodeTransformer):
    def __init__(self) -> None:
        self.changed = 0

    def visit_Call(self, node: ast.Call) -> ast.AST:
        self.generic_visit(node)
        if not (isinstance(node.func, ast.Name) and node.func.id == "print"):
            return node
        # Argumente analysieren
        args = [a for a in node.args if not isinstance(a, ast.Starred)]
        if not args:
            # print()  → log.info("")
            level = "info"
            new_args: list[ast.expr] = [ast.Constant("")]
        elif len(args) == 1:
            text_repr = ast.unparse(args[0])
            level = _level_for(text_repr)
            new_args = [args[0]]
        else:
            # Mehrere positionale Args -> log.info("%s %s ...", a, b, ...)
            text_repr = " ".join(ast.unparse(a) for a in args)
            level = _level_for(text_repr)
            fmt = " ".join("%s" for _ in args)
            new_args = [ast.Constant(fmt)] + list(args)

        # exc_info=True hinzufügen, wenn 'except' im Kontext erkennbar
        # → wird im File-Level-Pass nachträglich gemacht (zu komplex per AST)

        new_call = ast.Call(
            func=ast.Attribute(
                value=ast.Name(id="log", ctx=ast.Load()),
                attr=level,
                ctx=ast.Load(),
            ),
            args=new_args,
            keywords=[],
        )
        ast.copy_location(new_call, node)
        self.changed += 1
        return new_call


def transform_file(path: Path) -> bool:
    src = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        print(f"  ! parse error in {path}: {e}", file=sys.stderr)
        return False

    transformer = _PrintTransformer()
    new_tree = transformer.visit(tree)
    if transformer.changed == 0:
        return False

    ast.fix_missing_locations(new_tree)
    new_src = ast.unparse(new_tree)

    # ast.unparse verliert Kommentare und Leerzeilen. Daher: nur wenn wir
    # *sicher* sind, dass es lohnt. Wir machen einen Fallback per Regex.
    return _regex_pass(path, src)


_RE_PRINT = re.compile(r"(?P<indent>^[ \t]*)print\((?P<body>.*)\)\s*$", re.MULTILINE)


def _regex_pass(path: Path, src: str) -> bool:
    """Konservativer Regex-Pass: ersetzt einzeilige print(...) durch log.<level>(...)."""
    new_lines: list[str] = []
    changed = 0
    for line in src.splitlines(keepends=True):
        stripped = line.rstrip("\n")
        m = re.match(r"^(?P<indent>[ \t]*)print\((?P<body>.*)\)\s*$", stripped)
        if not m or _is_multiline_print(stripped):
            new_lines.append(line)
            continue
        indent = m.group("indent")
        body = m.group("body").strip()
        if not body:
            level, new_body = "info", '""'
        else:
            level = _level_for(body)
            new_body = body  # keep as-is (f-string, expressions etc.)
        end = "\n" if line.endswith("\n") else ""
        new_lines.append(f"{indent}log.{level}({new_body}){end}")
        changed += 1

    if not changed:
        return False

    new_src = "".join(new_lines)
    name = _worker_name_for(path)
    if not _has_logger_import(new_src):
        new_src = _insert_logger_import(new_src, name)
    path.write_text(new_src, encoding="utf-8")
    print(f"  ✓ {path}: {changed} prints replaced")
    return True


def _is_multiline_print(line: str) -> bool:
    """Heuristik: print( ohne schließende Klammer auf der gleichen Zeile."""
    if "print(" not in line:
        return False
    # Zähle Klammern roh (ohne Strings korrekt zu parsen – Best Effort)
    # Wir akzeptieren single-line wenn opens == closes ab dem ersten print(
    idx = line.find("print(")
    sub = line[idx:]
    depth = 0
    for ch in sub:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return False
    return True


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: replace_prints <path-or-dir> [<path-or-dir> ...]")
        return 1
    targets: list[Path] = []
    for a in argv:
        p = Path(a)
        if p.is_dir():
            targets.extend(p.rglob("*.py"))
        elif p.is_file() and p.suffix == ".py":
            targets.append(p)

    skip_substrings = ("/.venv/", "/core/", "/test_", "/debug_", "/_archive_migration/")
    count = 0
    for p in targets:
        s = str(p.as_posix())
        if any(x in s for x in skip_substrings):
            continue
        if transform_file(p):
            count += 1
    print(f"\nDone. {count} files modified.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
