"""Engine tests: each clause alone + combined, join fixtures, aggregates,
NULL semantics, precedence behavior, error cases."""
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from miniql import Engine, QueryError  # noqa: E402

CITIES = """city,population,region
london,8900000,uk
paris,2100000,fr
berlin,3700000,de
madrid,3200000,es
"""

# join fixture: known pairs by matching region codes
REGIONS = """code,name
uk,United Kingdom
fr,France
de,Germany
es,Spain
"""

PEOPLE = """id,name,country
1,alice,uk
2,bob,fr
3,carol,de
4,dave,es
5,eve,it
"""

# score has an empty cell → NULL
SCORES = """name,score
ann,10
bob,
carol,30
"""


class EngineTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="miniql-")
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        (self.dir / "cities.csv").write_text(CITIES, encoding="utf-8")
        (self.dir / "regions.csv").write_text(REGIONS, encoding="utf-8")
        (self.dir / "people.csv").write_text(PEOPLE, encoding="utf-8")
        (self.dir / "scores.csv").write_text(SCORES, encoding="utf-8")
        (self.dir / "empty.csv").write_text("a,b\n", encoding="utf-8")
        self.engine = Engine(self.dir)

    def q(self, sql):
        return self.engine.execute(sql)


class TestClausesAlone(EngineTestCase):
    def test_select_star_and_projection(self):
        cols, rows = self.q("SELECT * FROM regions")
        self.assertEqual(cols, ["code", "name"])
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0], {"code": "uk", "name": "United Kingdom"})

        cols, rows = self.q("SELECT name FROM regions WHERE code = 'fr'")
        self.assertEqual(cols, ["name"])
        self.assertEqual(rows, [{"name": "France"}])

    def test_where_all_comparison_operators(self):
        cases = [
            ("population = 8900000", ["london"]),
            ("population != 8900000", ["paris", "berlin", "madrid"]),
            ("population < 3000000", ["paris"]),
            ("population <= 2100000", ["paris"]),
            ("population > 3000000", ["london", "berlin", "madrid"]),
            ("population >= 3200000", ["london", "berlin", "madrid"]),
            ("city = 'london'", ["london"]),
            ("city != 'london'", ["paris", "berlin", "madrid"]),
        ]
        for where, expected in cases:
            with self.subTest(where=where):
                _c, rows = self.q(f"SELECT city FROM cities WHERE {where}")
                self.assertEqual([r["city"] for r in rows], expected)

    def test_order_by_alone(self):
        _c, rows = self.q("SELECT city FROM cities ORDER BY population DESC")
        self.assertEqual(
            [r["city"] for r in rows], ["london", "berlin", "madrid", "paris"]
        )
        _c, rows = self.q("SELECT city FROM cities ORDER BY city")
        self.assertEqual(
            [r["city"] for r in rows], ["berlin", "london", "madrid", "paris"]
        )

    def test_limit_alone(self):
        _c, rows = self.q("SELECT city FROM cities ORDER BY city LIMIT 2")
        self.assertEqual([r["city"] for r in rows], ["berlin", "london"])
        _c, rows = self.q("SELECT city FROM cities LIMIT 0")
        self.assertEqual(rows, [])

    def test_group_by_alone(self):
        _c, rows = self.q(
            "SELECT region, COUNT(*) AS n FROM cities GROUP BY region ORDER BY region"
        )
        self.assertEqual(
            rows,
            [
                {"region": "de", "n": 1},
                {"region": "es", "n": 1},
                {"region": "fr", "n": 1},
                {"region": "uk", "n": 1},
            ],
        )

    def test_join_alone(self):
        _c, rows = self.q(
            "SELECT cities.city, regions.name "
            "FROM cities JOIN regions ON cities.region = regions.code "
            "ORDER BY cities.city"
        )
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0], {"city": "berlin", "name": "Germany"})


class TestCombinedAndJoin(EngineTestCase):
    def test_join_correctness_on_known_fixture(self):
        _c, rows = self.q(
            "SELECT people.name, regions.name AS country "
            "FROM people JOIN regions ON people.country = regions.code "
            "ORDER BY people.name"
        )
        # eve (it) has no region row → inner join drops her
        self.assertEqual(
            rows,
            [
                {"name": "alice", "country": "United Kingdom"},
                {"name": "bob", "country": "France"},
                {"name": "carol", "country": "Germany"},
                {"name": "dave", "country": "Spain"},
            ],
        )

    def test_join_with_where_and_limit_combined(self):
        _c, rows = self.q(
            "SELECT people.name, regions.name AS country "
            "FROM people JOIN regions ON people.country = regions.code "
            "WHERE people.id > 1 ORDER BY people.id LIMIT 2"
        )
        self.assertEqual(
            rows,
            [
                {"name": "bob", "country": "France"},
                {"name": "carol", "country": "Germany"},
            ],
        )

    def test_join_ambiguous_column_requires_qualification(self):
        (self.dir / "codes.csv").write_text("region,x\nuk,1\n", encoding="utf-8")
        (self.dir / "codes2.csv").write_text("region,y\nuk,2\n", encoding="utf-8")
        engine = Engine(self.dir)
        with self.assertRaises(QueryError) as ctx:
            engine.execute(
                "SELECT region FROM codes JOIN codes2 ON codes.region = codes2.region"
            )
        self.assertIn("ambiguous", str(ctx.exception))
        # qualified reference resolves it
        _c, rows = engine.execute(
            "SELECT codes.region FROM codes JOIN codes2 "
            "ON codes.region = codes2.region"
        )
        self.assertEqual(rows, [{"region": "uk"}])

    def test_combined_full_pipeline(self):
        _c, rows = self.q(
            "SELECT region, COUNT(*) AS n, SUM(population) AS total "
            "FROM cities "
            "WHERE population >= 3000000 "
            "GROUP BY region "
            "ORDER BY total DESC "
            "LIMIT 2"
        )
        self.assertEqual(
            rows,
            [
                {"region": "uk", "n": 1, "total": 8900000},
                {"region": "de", "n": 1, "total": 3700000},
            ],
        )


class TestPrecedenceBehavior(EngineTestCase):
    def test_and_binds_tighter_than_or(self):
        # (country='uk') OR (country='fr' AND id=2) → alice + bob
        _c, rows = self.q(
            "SELECT name FROM people "
            "WHERE country = 'uk' OR country = 'fr' AND id = 2 "
            "ORDER BY name"
        )
        self.assertEqual([r["name"] for r in rows], ["alice", "bob"])
        # with parens: (uk OR fr) AND id=2 → bob only
        _c, rows = self.q(
            "SELECT name FROM people "
            "WHERE (country = 'uk' OR country = 'fr') AND id = 2 "
            "ORDER BY name"
        )
        self.assertEqual([r["name"] for r in rows], ["bob"])

    def test_not_parenthesized_semantics(self):
        _c, rows = self.q(
            "SELECT name FROM people WHERE NOT (country = 'uk') ORDER BY name"
        )
        self.assertEqual(
            [r["name"] for r in rows], ["bob", "carol", "dave", "eve"]
        )


class TestAggregates(EngineTestCase):
    def test_group_aggregates_match_hand_computed_values(self):
        _c, rows = self.q(
            "SELECT city, COUNT(*) AS n, SUM(population) AS s, "
            "MIN(population) AS lo, MAX(population) AS hi "
            "FROM cities WHERE population < 8000000 "
            "GROUP BY city ORDER BY city"
        )
        self.assertEqual(
            rows,
            [
                {"city": "berlin", "n": 1, "s": 3700000, "lo": 3700000, "hi": 3700000},
                {"city": "madrid", "n": 1, "s": 3200000, "lo": 3200000, "hi": 3200000},
                {"city": "paris", "n": 1, "s": 2100000, "lo": 2100000, "hi": 2100000},
            ],
        )
        # AVG over the whole table: (8900000+2100000+3700000+3200000)/4
        _c, rows = self.q("SELECT AVG(population) AS a FROM cities")
        self.assertEqual(rows, [{"a": 4475000.0}])

    def test_count_star_vs_count_column_ignores_nulls(self):
        _c, rows = self.q("SELECT COUNT(*) AS all_rows, COUNT(score) AS scored FROM scores")
        self.assertEqual(rows, [{"all_rows": 3, "scored": 2}])
        _c, rows = self.q("SELECT SUM(score) AS s, AVG(score) AS a, "
                          "MIN(score) AS lo, MAX(score) AS hi FROM scores")
        self.assertEqual(rows, [{"s": 40, "a": 20.0, "lo": 10, "hi": 30}])

    def test_empty_input_null_semantics(self):
        """Aggregate over zero rows: COUNT=0, SUM/AVG/MIN/MAX = NULL;
        a non-aggregate query returns zero rows."""
        _c, rows = self.q(
            "SELECT COUNT(*) AS n, COUNT(a) AS nc, SUM(a) AS s, AVG(a) AS av, "
            "MIN(a) AS lo, MAX(a) AS hi FROM empty"
        )
        self.assertEqual(
            rows, [{"n": 0, "nc": 0, "s": None, "av": None, "lo": None, "hi": None}]
        )
        _c, rows = self.q("SELECT a FROM empty")
        self.assertEqual(rows, [])
        # WHERE filtering everything away still yields the single agg row
        _c, rows = self.q("SELECT COUNT(*) AS n, SUM(population) AS s "
                          "FROM cities WHERE population > 1000000000")
        self.assertEqual(rows, [{"n": 0, "s": None}])

    def test_null_comparisons_filter_rows(self):
        # bob's score is NULL → every comparison is UNKNOWN → excluded
        _c, rows = self.q("SELECT name FROM scores WHERE score > 0 ORDER BY name")
        self.assertEqual([r["name"] for r in rows], ["ann", "carol"])
        _c, rows = self.q("SELECT name FROM scores WHERE NOT score > 0")
        self.assertEqual(rows, [])  # NOT UNKNOWN is still UNKNOWN

    def test_group_by_column_must_be_grouped_or_aggregated(self):
        with self.assertRaises(QueryError) as ctx:
            self.q("SELECT city, population FROM cities GROUP BY city")
        self.assertIn("GROUP BY", str(ctx.exception))


class TestErrorsAndTypes(EngineTestCase):
    def test_unknown_table_and_column_errors(self):
        with self.assertRaises(QueryError) as ctx:
            self.q("SELECT a FROM nope")
        self.assertIn("unknown table", str(ctx.exception))
        with self.assertRaises(QueryError) as ctx2:
            self.q("SELECT nope FROM cities")
        self.assertIn("unknown column", str(ctx2.exception))
        self.assertIsNotNone(ctx2.exception.pos)

    def test_order_by_unknown_column_errors(self):
        with self.assertRaises(QueryError) as ctx:
            self.q("SELECT city FROM cities ORDER BY nosuch")
        self.assertIn("ORDER BY", str(ctx.exception))

    def test_csv_type_inference(self):
        (self.dir / "typed.csv").write_text(
            "i,f,s,blank\n7,2.5,hello,\n", encoding="utf-8"
        )
        _c, rows = self.q("SELECT * FROM typed")
        row = rows[0]
        self.assertEqual(row["i"], 7)
        self.assertIsInstance(row["i"], int)
        self.assertEqual(row["f"], 2.5)
        self.assertIsInstance(row["f"], float)
        self.assertEqual(row["s"], "hello")
        self.assertIsNone(row["blank"])

    def test_malformed_csv_reported_with_line_number(self):
        (self.dir / "bad.csv").write_text("a,b\n1\n", encoding="utf-8")
        with self.assertRaises(QueryError) as ctx:
            self.q("SELECT * FROM bad")
        self.assertIn("line 2", str(ctx.exception))

    def test_order_by_non_selected_column_and_ordinal(self):
        _c, rows = self.q("SELECT city FROM cities ORDER BY population DESC")
        self.assertEqual(
            [r["city"] for r in rows], ["london", "berlin", "madrid", "paris"]
        )
        _c, rows = self.q("SELECT city, population FROM cities ORDER BY 2 DESC LIMIT 1")
        self.assertEqual(rows[0]["city"], "london")
        _c, rows = self.q("SELECT city, population FROM cities ORDER BY 1 DESC")
        self.assertEqual(rows[0]["city"], "paris")


if __name__ == "__main__":
    unittest.main()
