"""Execution engine: CSV loading, expression evaluation, query pipeline.

Pipeline for a :class:`~miniql.parser.SelectStatement`::

    load FROM table (+ JOIN via nested loops)
      → WHERE filter (row kept only when the predicate is *True*)
      → projection / GROUP BY aggregation
      → ORDER BY (multi-key, stable, per-key ASC/DESC)
      → LIMIT

Value model (documented semantics, tested):

* CSV cells are auto-typed: ``42`` → int, ``3.5`` → float, empty cell
  → NULL (``None``), anything else → str.  Leading-zero ids become ints
  (quote them in your schema if that matters — miniql keeps it simple).
* Comparisons involving NULL yield NULL; WHERE keeps a row only on True
  (so NULL behaves like SQL's UNKNOWN → filtered out).
* Comparing a number with text yields NULL (never a crash).
* ``COUNT(*)`` counts rows, ``COUNT(expr)`` counts non-NULL values.
  ``SUM``/``AVG`` ignore NULLs and are NULL on empty input; ``AVG``
  returns a float; ``MIN``/``MAX`` ignore NULLs and order numbers before
  text (a documented total order, NULLs excluded).
* Without GROUP BY, aggregate queries return exactly one row even when
  the input is empty (COUNT = 0, the rest NULL).  Non-aggregate queries
  over empty input return zero rows.
* Duplicate column names across a JOIN are only reachable through the
  qualified ``table.column`` form; the bare name raises a QueryError.
"""
from __future__ import annotations

import csv
from pathlib import Path

from .errors import QueryError
from .parser import (
    Aggregate,
    BoolOp,
    ColumnRef,
    Compare,
    Literal,
    Node,
    Not,
    SelectStatement,
    Star,
    parse,
)

AGG_FUNCS = {"COUNT", "SUM", "AVG", "MIN", "MAX"}


def infer(value: str):
    """Auto-type a raw CSV cell: int → float → NULL (empty) → str."""
    text = value.strip()
    if text == "":
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    return text


def compare(op: str, left, right):
    """Apply a comparison; NULL anywhere → NULL (None)."""
    if left is None or right is None:
        return None
    if isinstance(left, (int, float)) != isinstance(right, (int, float)):
        return None  # number vs text never compares
    try:
        if op == "=":
            return left == right
        if op == "!=":
            return left != right
        if op == "<":
            return left < right
        if op == "<=":
            return left <= right
        if op == ">":
            return left > right
        if op == ">=":
            return left >= right
    except TypeError:
        return None
    raise QueryError(f"unknown operator {op!r}")


class Engine:
    """Executes parsed statements against a directory of CSV tables."""

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        if not self.directory.is_dir():
            raise QueryError(f"not a directory: {self.directory}")
        self._tables: dict[str, list[dict]] = {}
        self._headers: dict[str, list[str]] = {}

    # ------------------------------------------------------------- data
    def table_names(self) -> list[str]:
        return sorted(p.stem for p in self.directory.glob("*.csv"))

    def headers(self, table: str) -> list[str]:
        self._load(table)
        return list(self._headers[table])

    def rows(self, table: str) -> list[dict]:
        self._load(table)
        return [dict(r) for r in self._tables[table]]

    def _load(self, table: str) -> None:
        if table in self._tables:
            return
        path = self.directory / f"{table}.csv"
        if not path.is_file():
            raise QueryError(f"unknown table: {table!r}")
        with open(path, newline="", encoding="utf-8") as fh:
            reader = csv.reader(fh)
            try:
                header = next(reader)
            except StopIteration:
                raise QueryError(f"table {table!r} is empty (no header)", 0) from None
            names = [h.strip().lower() for h in header]
            if any(not n for n in names):
                raise QueryError(f"table {table!r} has an empty column name", 0)
            records = []
            for line_no, raw in enumerate(reader, start=2):
                if not any(cell.strip() for cell in raw):
                    continue  # blank line
                if len(raw) != len(names):
                    raise QueryError(
                        f"table {table!r} line {line_no}: expected "
                        f"{len(names)} columns, got {len(raw)}",
                        0,
                    )
                records.append({n: infer(c) for n, c in zip(names, raw)})
        self._tables[table] = records
        self._headers[table] = names

    # ------------------------------------------------------------ public
    def execute(self, sql: str) -> tuple[list[str], list[dict]]:
        """Run ``sql``; return ``(output column names, result rows)``."""
        return self.run(parse(sql))

    def run(self, stmt: SelectStatement) -> tuple[list[str], list[dict]]:
        base = stmt.table
        joined = self._from_join(stmt)
        # WHERE
        if stmt.where is not None:
            joined = [r for r in joined if self._eval(stmt.where, r) is True]
        has_agg = any(
            self._find_aggregate(item.expr) is not None for item in stmt.items
        )
        if stmt.group_by or has_agg:
            columns, result = self._project_grouped(stmt, joined)
            result = self._order(stmt, columns, result)
        else:
            # sort before projection so ORDER BY may reference columns
            # that are not part of the select list
            joined = self._order_plain(stmt, joined)
            columns, result = self._project_plain(stmt, joined, base)
        if stmt.limit is not None:
            result = result[: stmt.limit]
        return columns, result

    # -------------------------------------------------------- FROM/JOIN
    def _from_join(self, stmt: SelectStatement) -> list[dict]:
        rows = self._qualified_rows(stmt.table)
        if stmt.join is None:
            return rows
        right = self._qualified_rows(stmt.join.table)
        out = []
        for left in rows:
            for r in right:
                merged = dict(left)
                merged.update(r)
                merged["__tables__"] = left["__tables__"] + r["__tables__"]
                if self._eval(stmt.join.on, merged) is True:
                    out.append(merged)
        return out

    def _qualified_rows(self, table: str) -> list[dict]:
        """Rows keyed by both bare and ``table.column`` names.

        A bare name is included only when unambiguous across the joined
        relation; ambiguity is detected later when a bare reference is
        evaluated.
        """
        result = []
        for rec in self.rows(table):
            row = {f"{table}.{k}": v for k, v in rec.items()}
            row.update(rec)
            row["__tables__"] = (table,)
            result.append(row)
        return result

    # ------------------------------------------------------- projection
    def _project_plain(self, stmt, rows, base):
        star = any(isinstance(item.expr, Star) for item in stmt.items)
        if star:
            if len(stmt.items) > 1:
                raise QueryError("'*' cannot be combined with other columns")
            tables = [base]
            if stmt.join is not None:
                tables.append(stmt.join.table)
                columns = [f"{t}.{c}" for t in tables for c in self.headers(t)]
            else:
                columns = list(self.headers(base))
            result = [{c: r.get(c) for c in columns} for r in rows]
            return columns, result

        columns = self._output_columns(stmt)
        result = []
        for r in rows:
            row = {columns[i]: self._eval(item.expr, r)
                   for i, item in enumerate(stmt.items)}
            result.append(row)
        return columns, result

    def _output_columns(self, stmt) -> list[str]:
        """Output column names; bare column names stay unqualified unless
        two select items would collide (then both become ``t.col``)."""
        names = [self._output_name(item, i) for i, item in enumerate(stmt.items)]
        counts: dict[str, int] = {}
        for name in names:
            counts[name] = counts.get(name, 0) + 1
        disambiguated = []
        for item, name in zip(stmt.items, names):
            if counts[name] > 1 and item.alias is None and isinstance(item.expr, ColumnRef):
                disambiguated.append(item.expr.qualified)
            else:
                disambiguated.append(name)
        return disambiguated

    def _output_name(self, item, ordinal: int) -> str:
        if item.alias:
            return item.alias
        expr = item.expr
        if isinstance(expr, ColumnRef):
            return expr.name
        if isinstance(expr, Aggregate):
            return _agg_label(expr)
        if isinstance(expr, Literal):
            return str(expr.value)
        raise QueryError(f"select item #{ordinal + 1} needs an alias")

    def _project_grouped(self, stmt, rows):
        # resolve group keys
        group_keys = [self._resolve_group_key(g) for g in stmt.group_by]
        groups: dict[tuple, list[dict]] = {}
        if not group_keys and not stmt.group_by:
            groups[()] = rows  # aggregate over everything (even empty)
        else:
            for r in rows:
                key = tuple(_hashable(r.get(k)) for k in group_keys)
                groups.setdefault(key, []).append(r)

        columns = self._output_columns(stmt)
        # validate bare column refs appear in GROUP BY
        if stmt.group_by:
            allowed = set(group_keys)
            for item in stmt.items:
                if isinstance(item.expr, ColumnRef):
                    name = item.expr.qualified
                    if name not in allowed and item.expr.name not in {
                        k.split(".")[-1] for k in allowed
                    }:
                        raise QueryError(
                            f"column {item.expr.qualified!r} must appear in "
                            f"GROUP BY or be used in an aggregate",
                            item.expr.pos,
                        )

        result = []
        for key, group in groups.items():
            row = {}
            for i, item in enumerate(stmt.items):
                row[columns[i]] = self._eval_aggregate(item.expr, group, stmt)
            if group_keys:
                # keep the group key values available for ORDER BY on source cols
                for k, v in zip(group_keys, key):
                    row.setdefault(k, v)
            result.append(row)
        return columns, result

    def _resolve_group_key(self, name: str) -> str:
        if "." in name:
            return name
        return name  # bare names resolve against the row dict directly

    # ------------------------------------------------------------- ORDER
    def _order_plain(self, stmt, rows):
        """ORDER BY over source rows (may reference output aliases,
        ordinals into the select list, or any source column)."""
        if not stmt.order_by:
            return rows
        columns = self._output_columns(stmt)

        def value_for(row, key: str):
            if key.isdigit():
                idx = int(key) - 1  # SQL ordinals are 1-based
                if 0 <= idx < len(columns):
                    return self._eval(stmt.items[idx].expr, row)
                raise QueryError(f"ORDER BY: ordinal {key} out of range")
            if key in columns:
                idx = columns.index(key)
                return self._eval(stmt.items[idx].expr, row)
            if key in row:
                return row[key]
            raise QueryError(f"ORDER BY: unknown column {key!r}")

        for item in reversed(stmt.order_by):
            rows.sort(
                key=lambda r, k=item.key: _sort_key(value_for(r, k)),
                reverse=item.descending,
            )
        return rows

    def _order(self, stmt, columns, result):
        if not stmt.order_by:
            return result
        for item in stmt.order_by:
            if not self._order_key_exists(item.key, columns, result):
                raise QueryError(f"ORDER BY: unknown column {item.key!r}")
        # stable sort from the least-significant key to the most
        for item in reversed(stmt.order_by):
            if item.key.isdigit():
                idx = int(item.key) - 1  # 1-based
                result.sort(
                    key=lambda r, i=idx: _sort_key(
                        r.get(columns[i]) if 0 <= i < len(columns) else None
                    ),
                    reverse=item.descending,
                )
            else:
                result.sort(
                    key=lambda r, k=item.key: _sort_key(r.get(k)),
                    reverse=item.descending,
                )
        return result

    def _order_key_exists(self, key: str, columns, result) -> bool:
        if key.isdigit():
            return 1 <= int(key) <= len(columns)
        if key in columns:
            return True
        if result and key in result[0]:
            return True
        return False

    # -------------------------------------------------------- evaluation
    def _find_aggregate(self, expr: Node):
        if isinstance(expr, Aggregate):
            return expr
        if isinstance(expr, BoolOp):
            return self._find_aggregate(expr.left) or self._find_aggregate(expr.right)
        if isinstance(expr, Not):
            return self._find_aggregate(expr.operand)
        if isinstance(expr, Compare):
            return self._find_aggregate(expr.left) or self._find_aggregate(expr.right)
        return None

    def _eval(self, expr: Node, row: dict):
        if isinstance(expr, Literal):
            return expr.value
        if isinstance(expr, ColumnRef):
            return self._lookup(expr, row)
        if isinstance(expr, Compare):
            return compare(expr.op, self._eval(expr.left, row), self._eval(expr.right, row))
        if isinstance(expr, BoolOp):
            left = self._eval(expr.left, row)
            if expr.op == "AND":
                if left is False:
                    return False
                right = self._eval(expr.right, row)
                if right is False:
                    return False
                if left is None or right is None:
                    return None
                return True
            if left is True:
                return True
            right = self._eval(expr.right, row)
            if right is True:
                return True
            if left is None or right is None:
                return None
            return False
        if isinstance(expr, Not):
            val = self._eval(expr.operand, row)
            if val is None:
                return None
            return not val
        if isinstance(expr, Aggregate):
            raise QueryError(
                "aggregate used outside the select list", expr.pos
            )
        if isinstance(expr, Star):
            raise QueryError("'*' is only valid as the whole select list or COUNT(*)", expr.pos)
        raise QueryError(f"cannot evaluate {expr!r}")

    def _lookup(self, ref: ColumnRef, row: dict):
        if ref.table:
            key = f"{ref.table}.{ref.name}"
            if key in row:
                return row[key]
            raise QueryError(f"unknown column {ref.qualified!r}", ref.pos)
        tables = row.get("__tables__", ())
        if len(tables) > 1:
            # ambiguous if both tables carry the column
            present = [t for t in tables if f"{t}.{ref.name}" in row]
            if len(present) > 1:
                raise QueryError(
                    f"ambiguous column {ref.name!r}; qualify it as "
                    f"{present[0]}.{ref.name} or {present[1]}.{ref.name}",
                    ref.pos,
                )
        if ref.name in row:
            return row[ref.name]
        raise QueryError(f"unknown column {ref.name!r}", ref.pos)

    def _eval_aggregate(self, expr: Node, group: list[dict], stmt):
        if isinstance(expr, Aggregate):
            return self._aggregate(expr, group)
        if isinstance(expr, Star):
            raise QueryError("'*' cannot be selected alongside aggregates")
        if isinstance(expr, ColumnRef):
            if not group:
                return None
            return self._eval(expr, group[0])
        if isinstance(expr, Literal):
            return expr.value
        raise QueryError(
            "only columns, literals and aggregates are allowed with GROUP BY",
            getattr(expr, "pos", None),
        )

    def _aggregate(self, agg: Aggregate, group: list[dict]):
        func = agg.func
        if agg.arg is None:  # COUNT(*)
            return len(group)
        values = [self._eval(agg.arg, r) for r in group]
        values = [v for v in values if v is not None]
        if func == "COUNT":
            return len(values)
        if func in ("SUM", "AVG"):
            if not values:
                return None
            bad = [v for v in values if not isinstance(v, (int, float))]
            if bad:
                raise QueryError(f"{func} requires numeric input, got {bad[0]!r}")
            total = sum(values)
            if func == "SUM":
                return total
            return total / len(values)
        if func in ("MIN", "MAX"):
            if not values:
                return None
            return min(values) if func == "MIN" else max(values)
        raise QueryError(f"unknown aggregate {func!r}", agg.pos)


def _agg_label(agg: Aggregate) -> str:
    if agg.arg is None:
        return "COUNT(*)"
    inner = agg.arg.qualified if isinstance(agg.arg, ColumnRef) else "?"
    return f"{agg.func}({inner})"


def _hashable(value):
    if isinstance(value, list):
        return tuple(value)
    return value


def _sort_key(value):
    """Total order for ORDER BY: NULL first, then numbers, then text."""
    if value is None:
        return (0, 0)
    if isinstance(value, bool):
        return (1, int(value))
    if isinstance(value, (int, float)):
        return (1, value)
    return (2, str(value))


def run_query(directory: str | Path, sql: str) -> tuple[list[str], list[dict]]:
    """Convenience: execute one SQL string against ``directory``."""
    return Engine(directory).execute(sql)
