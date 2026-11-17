# Sift

A small **SQL query engine over CSV files** — tokenizer + recursive-descent
parser to an AST, then a real execution engine: `SELECT` with `JOIN`,
`WHERE`, `GROUP BY`, `ORDER BY`, `LIMIT`, comparison operators,
`AND`/`OR`/`NOT` with correct precedence, the aggregates
`COUNT SUM AVG MIN MAX`, NULL semantics, parse errors that point at the
exact character position, and a CLI with an interactive mode that prints
result tables.

**Zero dependencies** — Python standard library only (`csv`, `argparse`,
`subprocess`-free tests, …).

## SQL subset

```
SELECT * | expr [AS alias] , ...
FROM table
[ [INNER] JOIN table ON expr ]
[ WHERE expr ]                 -- = != < <= > >= AND OR NOT, ( )
[ GROUP BY column, ... ]
[ ORDER BY column|ordinal [ASC|DESC], ... ]
[ LIMIT n ] ;
```

Precedence (tested): `OR` < `AND` < `NOT` < comparison; parentheses
override.  Column references may be bare (`city`) or qualified
(`cities.city`); across a JOIN a bare name that exists in both tables is
**ambiguous** and must be qualified.  `ORDER BY` accepts output
aliases, 1-based ordinals (`ORDER BY 2 DESC`), or any source column —
even one not in the select list.  Statements in the REPL end with `;`
and may span multiple lines.

## Documented semantics

* **CSV typing:** cells are auto-typed `int` → `float` → empty cell is
  `NULL` → `str`.  Blank lines are skipped; a row with the wrong column
  count is reported with its line number.
* **NULL:** any comparison with `NULL` yields `NULL` (SQL's UNKNOWN);
  `WHERE` keeps a row only when the predicate is exactly **True** — so
  `NOT (x = 1)` still drops NULL rows.  Comparing a number with text
  yields `NULL`, never a crash.
* **Aggregates on empty input:** `COUNT(*)` = 0, `COUNT(col)` counts
  non-NULL values, `SUM`/`AVG`/`MIN`/`MAX` = `NULL`.  Without `GROUP BY`
  an aggregate query returns exactly **one** row even over zero input
  rows; a plain query returns zero rows.  `AVG` returns a float;
  `MIN`/`MAX` ignore NULLs and order numbers before text.
* **GROUP BY:** bare (non-aggregated) select columns must appear in
  `GROUP BY` — otherwise a `QueryError` names the column and position.
* **Errors:** `ParseError`/`QueryError` both carry `pos`; the message
  ends with `(at position N)`.

## Runnable example

The repository ships a `fixtures/` directory (`cities.csv`,
`regions.csv`, `people.csv`).

```console
> python -m miniql fixtures -c "SELECT city, population FROM cities WHERE population > 3000000 ORDER BY population DESC;"
+--------+------------+
| city   | population |
| london | 8900000    |
| berlin | 3700000    |
| madrid | 3200000    |
+--------+------------+
> python -m miniql fixtures -c "SELECT people.name, regions.name AS country FROM people JOIN regions ON people.country = regions.code ORDER BY people.name;"
+-------+----------------+
| name  | country        |
| alice | United Kingdom |
| bob   | France         |
| carol | Germany        |
| dave  | Spain          |
| eve   | Italy          |
+-------+----------------+
> python -m miniql fixtures -c "SELECT COUNT(*) AS n, AVG(population) AS avg_pop FROM cities;"
+---+---------+
| n | avg_pop |
| 4 | 4475000 |
+---+---------+
```

Interactive mode (statements end with `;`; `.help`, `.tables`,
`.schema <t>`, `.exit`):

```console
> python -m miniql fixtures
miniql — type SQL, or .help for commands
miniql> SELECT city FROM cities ORDER BY city;
+--------+
| city   |
| berlin |
| london |
| madrid |
| paris  |
+--------+
miniql> .tables
cities
people
regions
miniql> exit
```

Library use:

```python
from miniql import Engine

engine = Engine("fixtures")
columns, rows = engine.execute(
    "SELECT region, COUNT(*) AS n FROM cities GROUP BY region ORDER BY region"
)
print(columns)  # ['region', 'n']
print(rows[0])  # {'region': 'de', 'n': 1}
```

## Test command

```
python -m unittest discover -s tests -v
```

53 tests: tokenizer positions, AST shape for every clause alone and the
full pipeline, `AND`-tighter-than-`OR` (AST **and** behavior), join
correctness on known fixtures (inner-join drop, ambiguity error),
aggregate hand-computed values, empty-input NULL semantics, NULL
filtering, parse errors with exact positions, error line numbers for
malformed CSVs, table formatting, and the CLI end to end (one-shot +
interactive REPL, multi-line statements, dot-commands, exit codes).
