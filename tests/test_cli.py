"""CLI tests: table formatting, one-shot mode, interactive REPL."""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from miniql.cli import format_table, main  # noqa: E402

CITIES = """city,population,region
london,8900000,uk
paris,2100000,fr
berlin,3700000,de
"""


def run_cli(*args, stdin=None):
    return subprocess.run(
        [sys.executable, "-m", "miniql", *args],
        cwd=PROJECT_ROOT,
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )


class TestFormatTable(unittest.TestCase):
    def test_alignment_and_null_rendering(self):
        out = format_table(
            ["name", "score"],
            [{"name": "ann", "score": 10}, {"name": "bob", "score": None}],
        )
        lines = out.splitlines()
        self.assertEqual(lines[0], "+------+-------+")
        self.assertIn("| name | score |", lines[1])
        self.assertIn("| ann  | 10    |", lines)
        self.assertIn("| bob  | NULL  |", lines)
        self.assertEqual(lines[-1], lines[0])
        # every data row has the same outer width
        widths = {len(ln) for ln in lines}
        self.assertEqual(len(widths), 1, out)

    def test_empty_result_still_prints_header(self):
        out = format_table(["a"], [])
        lines = out.splitlines()
        self.assertEqual(len(lines), 3)  # rule, header, rule
        self.assertIn("| a ", lines[1])


class TestOneShotMode(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="miniql-cli-")
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        (self.dir / "cities.csv").write_text(CITIES, encoding="utf-8")

    def test_one_shot_prints_table(self):
        out = run_cli(str(self.dir), "-c", "SELECT city FROM cities ORDER BY city")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("| city   |", out.stdout)
        self.assertIn("| berlin |", out.stdout)
        self.assertIn("| paris  |", out.stdout)

    def test_one_shot_parse_error_exits_nonzero(self):
        out = run_cli(str(self.dir), "-c", "SELECT FROM cities")
        self.assertEqual(out.returncode, 1)
        self.assertIn("Parse error at position", out.stderr)

    def test_one_shot_query_error_exits_nonzero(self):
        out = run_cli(str(self.dir), "-c", "SELECT a FROM nosuch")
        self.assertEqual(out.returncode, 1)
        self.assertIn("unknown table", out.stderr)

    def test_missing_directory_exits_2(self):
        out = run_cli(str(self.dir / "nope"), "-c", "SELECT 1")
        self.assertEqual(out.returncode, 2)

    def test_main_returns_cleanly_for_valid_query(self):
        # direct call (no subprocess) — stdout is captured by unittest
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = main([str(self.dir), "-c", "SELECT COUNT(*) AS n FROM cities"])
        self.assertEqual(code, 0)
        self.assertIn("3", buf.getvalue())


class TestInteractiveRepl(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="miniql-repl-")
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        (self.dir / "cities.csv").write_text(CITIES, encoding="utf-8")

    def test_interactive_query_and_exit(self):
        out = run_cli(
            str(self.dir),
            stdin="SELECT city FROM cities ORDER BY city;\nexit\n",
        )
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("miniql>", out.stdout)
        self.assertIn("| berlin |", out.stdout)
        self.assertIn("| london |", out.stdout)

    def test_multiline_statement_joined_until_semicolon(self):
        out = run_cli(
            str(self.dir),
            stdin="SELECT city\nFROM cities\nWHERE population > 5000000;\nquit\n",
        )
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("| london |", out.stdout)

    def test_dot_commands(self):
        out = run_cli(
            str(self.dir),
            stdin=".tables\n.schema cities\n.help\n.exit\n",
        )
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("cities", out.stdout)
        self.assertIn("cities(city, population, region)", out.stdout)
        self.assertIn("commands:", out.stdout)

    def test_error_reported_and_repl_continues(self):
        out = run_cli(
            str(self.dir),
            stdin="SELECT FROM cities;\nSELECT city FROM cities LIMIT 1;\nexit\n",
        )
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("error: Parse error at position", out.stdout)
        self.assertIn("| city   |", out.stdout)  # second statement still ran

    def test_eof_exits_zero(self):
        out = run_cli(str(self.dir), stdin="")
        self.assertEqual(out.returncode, 0)


if __name__ == "__main__":
    unittest.main()
