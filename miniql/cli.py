"""miniql CLI: one-shot ``-c`` mode and an interactive REPL.

Both modes print result tables SQLite-style::

    +------+-----+
    | city | n   |
    +------+-----+
    | a    | 3   |
    +------+-----+

Interactive commands: ``.tables``, ``.schema <table>``, ``.help``,
``.exit`` (or ``exit``/``quit``/Ctrl-D).  Statements may end with ``;``.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .engine import Engine
from .errors import MiniQLError, ParseError, QueryError


def format_table(columns: list[str], rows: list[dict]) -> str:
    """Render rows as an aligned ASCII table (NULL shown as ``NULL``)."""
    def cell(v) -> str:
        if v is None:
            return "NULL"
        if isinstance(v, float) and v.is_integer():
            return str(int(v)) if abs(v) < 1e16 else repr(v)
        return str(v)

    headers = [str(c) for c in columns]
    body = [[cell(row.get(c)) for c in columns] for row in rows]
    widths = []
    for i, h in enumerate(headers):
        w = len(h)
        for r in body:
            w = max(w, len(r[i]))
        widths.append(w)

    def rule() -> str:
        return "+" + "+".join("-" * (w + 2) for w in widths) + "+"

    def line(values: list[str]) -> str:
        return "| " + " | ".join(
            v.ljust(w) for v, w in zip(values, widths)
        ) + " |"

    out = [rule(), line(headers)]
    out.extend(line(r) for r in body)
    out.append(rule())
    return "\n".join(out)


def run_sql(engine: Engine, sql: str, out) -> None:
    columns, rows = engine.execute(sql)
    print(format_table(columns, rows), file=out)


def repl(engine: Engine, stdin=None, out=None) -> int:
    stdin = stdin if stdin is not None else sys.stdin
    out = out if out is not None else sys.stdout
    print("miniql — type SQL, or .help for commands", file=out)
    buffer = ""
    while True:
        try:
            prompt = "miniql> " if not buffer else "      > "
            print(prompt, end="", file=out, flush=True)
            line = stdin.readline()
        except KeyboardInterrupt:
            print(file=out)
            return 130
        if line == "":
            print(file=out)
            return 0
        text = line.strip()
        if not buffer and not text:
            continue
        if not buffer:
            if text in ("exit", "quit"):
                return 0
            if text.startswith("."):
                if not handle_dot(engine, text, out):
                    return 0
                continue
        buffer = f"{buffer} {text}" if buffer else text
        if not buffer.rstrip().endswith(";"):
            continue  # keep reading until the statement terminator
        sql = buffer.rstrip()[:-1].strip()
        buffer = ""
        if not sql:
            continue
        try:
            run_sql(engine, sql, out)
        except MiniQLError as exc:
            print(f"error: {exc}", file=out)


def handle_dot(engine: Engine, command: str, out) -> bool:
    """Run a dot-command; returns False when the REPL should exit."""
    parts = command.split()
    name = parts[0]
    if name in (".exit", ".quit"):
        return False
    if name == ".help":
        print(
            "commands:\n"
            "  .tables          list tables\n"
            "  .schema <table>  show column names\n"
            "  .help            this message\n"
            "  .exit            leave (also: exit, quit)\n"
            "statements end with ';' — multi-line input is joined",
            file=out,
        )
        return True
    if name == ".tables":
        for t in engine.table_names():
            print(t, file=out)
        return True
    if name == ".schema":
        if len(parts) != 2:
            print("usage: .schema <table>", file=out)
            return True
        try:
            cols = engine.headers(parts[1])
            print(f"{parts[1]}({', '.join(cols)})", file=out)
        except QueryError as exc:
            print(f"error: {exc}", file=out)
        return True
    print(f"unknown command: {name} (try .help)", file=out)
    return True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="miniql",
        description="Run a SQL subset against CSV files in a directory",
    )
    parser.add_argument("directory", help="directory containing .csv tables")
    parser.add_argument(
        "-c", "--command", metavar="SQL",
        help="run one statement and exit (non-interactive)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    directory = Path(args.directory)
    if not directory.is_dir():
        print(f"error: not a directory: {directory}", file=sys.stderr)
        return 2
    try:
        engine = Engine(directory)
    except QueryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.command is not None:
        try:
            run_sql(engine, args.command, sys.stdout)
        except MiniQLError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return 0
    return repl(engine)


if __name__ == "__main__":
    sys.exit(main())
