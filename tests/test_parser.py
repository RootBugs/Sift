"""Tokenizer and parser tests: AST shape, precedence, error positions."""
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from miniql import ParseError, parse, tokenize  # noqa: E402
from miniql.parser import (  # noqa: E402
    Aggregate,
    BoolOp,
    ColumnRef,
    Compare,
    JoinClause,
    Not,
    Star,
)


class TestTokenizer(unittest.TestCase):
    def test_tokenize_covers_token_types_with_positions(self):
        tokens = tokenize("SELECT name FROM t WHERE n >= 3 AND x != 'a''b'")
        types = [t.type for t in tokens]
        self.assertEqual(types[0], "KEYWORD")
        self.assertEqual(tokens[0].value, "SELECT")
        self.assertEqual(tokens[0].pos, 0)
        self.assertIn("IDENT", types)
        self.assertIn("OP", types)
        self.assertIn("STRING", types)
        self.assertEqual(tokens[-1].type, "EOF")
        # doubled quote escapes a literal quote
        string_tok = next(t for t in tokens if t.type == "STRING")
        self.assertEqual(string_tok.value, "a'b")
        # keyword recognition is case-insensitive; names normalize to lower
        tokens2 = tokenize("select NAME from T")
        self.assertEqual(tokens2[0].value, "SELECT")
        self.assertEqual(tokens2[1].value, "name")
        self.assertEqual(tokens2[1].pos, 7)

    def test_tokenize_numbers_and_negative_literals(self):
        tokens = tokenize("SELECT x FROM t WHERE n > -12 AND f <= 3.5")
        numbers = [t.value for t in tokens if t.type == "NUMBER"]
        self.assertEqual(numbers, [-12, 3.5])
        self.assertIsInstance(numbers[0], int)
        self.assertIsInstance(numbers[1], float)

    def test_unterminated_string_reports_start_position(self):
        with self.assertRaises(ParseError) as ctx:
            tokenize("SELECT 'abc")
        self.assertEqual(ctx.exception.pos, 7)
        self.assertIn("unterminated", str(ctx.exception))

    def test_unexpected_character_reports_position(self):
        with self.assertRaises(ParseError) as ctx:
            tokenize("SELECT a FROM t # oops")
        self.assertEqual(ctx.exception.pos, 16)
        self.assertIn("unexpected character", str(ctx.exception))


class TestParser(unittest.TestCase):
    def test_select_star(self):
        stmt = parse("SELECT * FROM cities;")
        self.assertIsInstance(stmt.items[0].expr, Star)
        self.assertEqual(stmt.table, "cities")
        self.assertIsNone(stmt.where)
        # trailing semicolon consumed, no trailing junk
        self.assertEqual(stmt.limit, None)

    def test_clause_alone_where(self):
        stmt = parse("SELECT name FROM t WHERE age >= 18")
        self.assertIsInstance(stmt.where, Compare)
        self.assertEqual(stmt.where.op, ">=")
        self.assertEqual(stmt.group_by, [])
        self.assertEqual(stmt.order_by, [])
        self.assertIsNone(stmt.join)

    def test_clause_alone_group_by(self):
        stmt = parse("SELECT city, COUNT(*) FROM t GROUP BY city")
        self.assertEqual(stmt.group_by, ["city"])
        self.assertIsInstance(stmt.items[1].expr, Aggregate)
        self.assertIsNone(stmt.items[1].expr.arg)  # COUNT(*)
        self.assertIsNone(stmt.where)

    def test_clause_alone_order_by_and_limit(self):
        stmt = parse("SELECT name FROM t ORDER BY name DESC, name ASC LIMIT 5")
        self.assertEqual(len(stmt.order_by), 2)
        self.assertTrue(stmt.order_by[0].descending)
        self.assertFalse(stmt.order_by[1].descending)
        self.assertEqual(stmt.limit, 5)
        self.assertIsNone(stmt.where)

    def test_clause_alone_join(self):
        stmt = parse("SELECT a.x FROM a JOIN b ON a.id = b.a_id")
        self.assertIsInstance(stmt.join, JoinClause)
        self.assertEqual(stmt.join.table, "b")
        self.assertIsInstance(stmt.join.on, Compare)
        self.assertEqual(stmt.join.on.op, "=")
        self.assertIsNone(stmt.where)

    def test_combined_statement_ast(self):
        stmt = parse(
            "SELECT city, COUNT(*) AS n FROM cities "
            "JOIN regions ON cities.region = regions.code "
            "WHERE population > 1000 "
            "GROUP BY city "
            "ORDER BY n DESC "
            "LIMIT 3"
        )
        self.assertEqual(stmt.table, "cities")
        self.assertIsNotNone(stmt.join)
        self.assertIsNotNone(stmt.where)
        self.assertEqual(stmt.group_by, ["city"])
        self.assertEqual(stmt.order_by[0].key, "n")
        self.assertTrue(stmt.order_by[0].descending)
        self.assertEqual(stmt.limit, 3)
        self.assertEqual(stmt.items[1].alias, "n")

    def test_and_binds_tighter_than_or(self):
        stmt = parse("SELECT x FROM t WHERE a = 1 OR b = 2 AND c = 3")
        where = stmt.where
        self.assertIsInstance(where, BoolOp)
        self.assertEqual(where.op, "OR")  # top level is OR
        self.assertIsInstance(where.right, BoolOp)
        self.assertEqual(where.right.op, "AND")  # AND grouped on the right
        self.assertIsInstance(where.right.left, Compare)
        self.assertIsInstance(where.right.right, Compare)

    def test_not_binds_tighter_than_and(self):
        stmt = parse("SELECT x FROM t WHERE NOT a = 1 AND b = 2")
        where = stmt.where
        self.assertIsInstance(where, BoolOp)
        self.assertEqual(where.op, "AND")
        self.assertIsInstance(where.left, Not)
        self.assertIsInstance(where.left.operand, Compare)

    def test_parentheses_override_precedence(self):
        stmt = parse("SELECT x FROM t WHERE (a = 1 OR b = 2) AND c = 3")
        where = stmt.where
        self.assertIsInstance(where, BoolOp)
        self.assertEqual(where.op, "AND")
        self.assertIsInstance(where.left, BoolOp)
        self.assertEqual(where.left.op, "OR")

    def test_aggregate_with_argument_and_alias_forms(self):
        stmt = parse("SELECT SUM(price) total, AVG(price) AS avg_price FROM t")
        first = stmt.items[0].expr
        self.assertIsInstance(first, Aggregate)
        self.assertEqual(first.func, "SUM")
        self.assertIsInstance(first.arg, ColumnRef)
        self.assertEqual(stmt.items[0].alias, "total")  # bare alias
        self.assertEqual(stmt.items[1].alias, "avg_price")  # AS alias

    def test_qualified_column_reference(self):
        stmt = parse("SELECT t2.col FROM t1 JOIN t2 ON t1.id = t2.id WHERE t2.col > 1")
        item = stmt.items[0].expr
        self.assertIsInstance(item, ColumnRef)
        self.assertEqual(item.table, "t2")
        self.assertEqual(item.name, "col")
        self.assertEqual(item.qualified, "t2.col")

    def test_parse_error_missing_from_has_position(self):
        with self.assertRaises(ParseError) as ctx:
            parse("SELECT name")
        self.assertEqual(ctx.exception.pos, 11)  # end of input position
        self.assertIn("FROM", str(ctx.exception))

    def test_parse_error_positions_point_at_bad_token(self):
        with self.assertRaises(ParseError) as ctx:
            parse("SELECT name FROM t WHERE ==")
        self.assertEqual(ctx.exception.pos, 25)
        with self.assertRaises(ParseError) as ctx2:
            parse("SELECT name FROM t GROUP city")
        self.assertEqual(ctx2.exception.pos, 25)
        self.assertIn("position 25", str(ctx2.exception))

    def test_limit_must_be_non_negative_integer(self):
        with self.assertRaises(ParseError) as ctx:
            parse("SELECT a FROM t LIMIT -1")
        self.assertIn("LIMIT", str(ctx.exception))
        self.assertGreaterEqual(ctx.exception.pos, 20)
        with self.assertRaises(ParseError):
            parse("SELECT a FROM t LIMIT x")

    def test_junk_after_statement_rejected(self):
        with self.assertRaises(ParseError) as ctx:
            parse("SELECT a FROM t; DROP TABLE t")
        self.assertIn("position 17", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
