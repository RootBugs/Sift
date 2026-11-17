"""Recursive-descent parser producing a SELECT statement AST.

Grammar (precedence loosest → tightest)::

    statement   := SELECT select_list FROM table
                   [ [INNER] JOIN table ON expr ]
                   [ WHERE expr ]
                   [ GROUP BY name_list ]
                   [ ORDER BY order_item (, order_item)* ]
                   [ LIMIT number ] [ ';' ] EOF
    select_list := select_item (, select_item)*
    select_item := '*' | expr [ AS name ]
    expr        := or_expr
    or_expr     := and_expr ( OR and_expr )*
    and_expr    := not_expr ( AND not_expr )*
    not_expr    := NOT not_expr | comparison
    comparison  := primary ( ( = != < <= > >= ) primary )?
    primary     := number | string | NULL | name [ '(' arg ')' ]
                   | '(' expr ')' | aggregate '(' ( '*' | expr ) ')'

``AND`` binds tighter than ``OR`` (tested); ``NOT`` binds tighter than
``AND``.  Aggregate functions may only appear in the select list; column
references in WHERE/JOIN/ORDER/GROUP are plain names or ``table.column``.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .errors import ParseError
from .tokenizer import AGGREGATES, Token, tokenize

COMPARISON_OPS = {"=", "!=", "<", "<=", ">", ">="}


# --------------------------------------------------------------------- AST
@dataclass(frozen=True)
class Literal:
    value: object
    pos: int


@dataclass(frozen=True)
class ColumnRef:
    name: str
    pos: int
    table: str | None = None

    @property
    def qualified(self) -> str:
        return f"{self.table}.{self.name}" if self.table else self.name


@dataclass(frozen=True)
class Compare:
    op: str
    left: "Node"
    right: "Node"
    pos: int


@dataclass(frozen=True)
class BoolOp:
    op: str  # AND | OR
    left: "Node"
    right: "Node"
    pos: int


@dataclass(frozen=True)
class Not:
    operand: "Node"
    pos: int


@dataclass(frozen=True)
class Aggregate:
    func: str  # COUNT | SUM | AVG | MIN | MAX
    arg: "Node | None"  # None means COUNT(*)
    pos: int


@dataclass(frozen=True)
class Star:
    pos: int


Node = Literal | ColumnRef | Compare | BoolOp | Not | Aggregate | Star


@dataclass(frozen=True)
class SelectItem:
    expr: Node
    alias: str | None = None


@dataclass(frozen=True)
class OrderItem:
    key: str  # output name, ordinal as string of digits, or source column
    descending: bool = False


@dataclass(frozen=True)
class JoinClause:
    table: str
    on: Node
    pos: int


@dataclass(frozen=True)
class SelectStatement:
    items: list[SelectItem]
    table: str
    join: JoinClause | None = None
    where: Node | None = None
    group_by: list[str] = field(default_factory=list)
    order_by: list[OrderItem] = field(default_factory=list)
    limit: int | None = None


# ------------------------------------------------------------------ parser
class Parser:
    def __init__(self, tokens: list[Token]) -> None:
        self.tokens = tokens
        self.i = 0

    # -- token helpers -------------------------------------------------
    @property
    def cur(self) -> Token:
        return self.tokens[self.i]

    def advance(self) -> Token:
        tok = self.tokens[self.i]
        if tok.type != "EOF":
            self.i += 1
        return tok

    def expect(self, type_: str, value=None, what: str | None = None) -> Token:
        tok = self.cur
        if tok.type != type_ or (value is not None and tok.value != value):
            wanted = what or (value if value is not None else type_.lower())
            found = "end of input" if tok.type == "EOF" else repr(tok.value)
            raise ParseError(f"expected {wanted}, found {found}", tok.pos)
        return self.advance()

    def accept_keyword(self, *words: str) -> Token | None:
        tok = self.cur
        if tok.type == "KEYWORD" and tok.value in words:
            return self.advance()
        return None

    # -- entry point ---------------------------------------------------
    def parse(self) -> SelectStatement:
        stmt = self.parse_select()
        if self.cur.type == "PUNCT" and self.cur.value == ";":
            self.advance()
        if self.cur.type != "EOF":
            raise ParseError(
                f"unexpected token {self.cur.value!r}", self.cur.pos
            )
        return stmt

    def parse_select(self) -> SelectStatement:
        self.expect("KEYWORD", "SELECT", "SELECT")
        items = self.parse_select_list()
        self.expect("KEYWORD", "FROM", "FROM")
        table_tok = self.expect("IDENT", what="table name")
        join = None
        if self.accept_keyword("INNER"):
            join = self.parse_join()
        elif self.cur.type == "KEYWORD" and self.cur.value == "JOIN":
            join = self.parse_join()
        where = None
        if self.accept_keyword("WHERE"):
            where = self.parse_expr()
        group_by: list[str] = []
        if self.accept_keyword("GROUP"):
            self.expect("KEYWORD", "BY", "BY")
            group_by = self.parse_name_list()
        order_by: list[OrderItem] = []
        if self.accept_keyword("ORDER"):
            self.expect("KEYWORD", "BY", "BY")
            order_by = self.parse_order_list()
        limit = None
        if self.accept_keyword("LIMIT"):
            tok = self.cur
            if tok.type != "NUMBER" or not isinstance(tok.value, int):
                raise ParseError("LIMIT requires a non-negative integer", tok.pos)
            if tok.value < 0:
                raise ParseError("LIMIT requires a non-negative integer", tok.pos)
            self.advance()
            limit = tok.value
        return SelectStatement(
            items=items,
            table=table_tok.value,
            join=join,
            where=where,
            group_by=group_by,
            order_by=order_by,
            limit=limit,
        )

    def parse_join(self) -> JoinClause:
        if self.cur.type == "KEYWORD" and self.cur.value == "JOIN":
            pos = self.cur.pos
            self.advance()
        else:
            self.expect("KEYWORD", "JOIN", "JOIN")
            pos = self.tokens[self.i - 1].pos
        table_tok = self.expect("IDENT", what="table name after JOIN")
        self.expect("KEYWORD", "ON", "ON")
        on = self.parse_expr()
        return JoinClause(table=table_tok.value, on=on, pos=pos)

    def parse_select_list(self) -> list[SelectItem]:
        items = [self.parse_select_item()]
        while self.cur.type == "PUNCT" and self.cur.value == ",":
            self.advance()
            items.append(self.parse_select_item())
        return items

    def parse_select_item(self) -> SelectItem:
        tok = self.cur
        if tok.type == "OP" and tok.value == "*":
            self.advance()
            return SelectItem(Star(tok.pos))
        expr = self.parse_expr()
        alias = None
        if self.accept_keyword("AS"):
            alias = self.expect("IDENT", what="alias after AS").value
        elif self.cur.type == "IDENT":
            # bare alias without AS (e.g. SELECT COUNT(*) n FROM t)
            alias = self.advance().value
        return SelectItem(expr, alias)

    def parse_name_list(self) -> list[str]:
        names = [self.parse_qualified_name()]
        while self.cur.type == "PUNCT" and self.cur.value == ",":
            self.advance()
            names.append(self.parse_qualified_name())
        return names

    def parse_order_list(self) -> list[OrderItem]:
        items = [self.parse_order_item()]
        while self.cur.type == "PUNCT" and self.cur.value == ",":
            self.advance()
            items.append(self.parse_order_item())
        return items

    def parse_order_item(self) -> OrderItem:
        tok = self.cur
        if tok.type == "NUMBER" and isinstance(tok.value, int):
            key = str(tok.value)
            self.advance()
        elif tok.type == "IDENT":
            key = self.parse_qualified_name()
        else:
            raise ParseError("expected a column name or ordinal in ORDER BY", tok.pos)
        descending = False
        if self.accept_keyword("DESC"):
            descending = True
        else:
            self.accept_keyword("ASC")
        return OrderItem(key, descending)

    def parse_qualified_name(self) -> str:
        first = self.expect("IDENT", what="column name")
        if self.cur.type == "PUNCT" and self.cur.value == ".":
            self.advance()
            second = self.expect("IDENT", what="column name after '.'")
            return f"{first.value}.{second.value}"
        return first.value

    # -- expressions ---------------------------------------------------
    def parse_expr(self) -> Node:
        return self.parse_or()

    def parse_or(self) -> Node:
        left = self.parse_and()
        while self.accept_keyword("OR"):
            pos = self.tokens[self.i - 1].pos
            right = self.parse_and()
            left = BoolOp("OR", left, right, pos)
        return left

    def parse_and(self) -> Node:
        left = self.parse_not()
        while self.accept_keyword("AND"):
            pos = self.tokens[self.i - 1].pos
            right = self.parse_not()
            left = BoolOp("AND", left, right, pos)
        return left

    def parse_not(self) -> Node:
        tok = self.accept_keyword("NOT")
        if tok is not None:
            return Not(self.parse_not(), tok.pos)
        return self.parse_comparison()

    def parse_comparison(self) -> Node:
        left = self.parse_primary()
        tok = self.cur
        if tok.type == "OP" and tok.value in COMPARISON_OPS:
            self.advance()
            right = self.parse_primary()
            return Compare(tok.value, left, right, tok.pos)
        return left

    def parse_primary(self) -> Node:
        tok = self.cur
        if tok.type == "NUMBER":
            self.advance()
            return Literal(tok.value, tok.pos)
        if tok.type == "STRING":
            self.advance()
            return Literal(tok.value, tok.pos)
        if tok.type == "KEYWORD" and tok.value == "NULL":
            self.advance()
            return Literal(None, tok.pos)
        if tok.type == "PUNCT" and tok.value == "(":
            self.advance()
            inner = self.parse_expr()
            self.expect("PUNCT", ")", ")")
            return inner
        if tok.type == "KEYWORD" and tok.value in AGGREGATES:
            return self.parse_aggregate()
        if tok.type == "IDENT":
            name = self.parse_qualified_name()
            if "." in name:
                table, col = name.split(".", 1)
                return ColumnRef(col, tok.pos, table)
            return ColumnRef(name, tok.pos)
        found = "end of input" if tok.type == "EOF" else repr(tok.value)
        raise ParseError(f"expected an expression, found {found}", tok.pos)

    def parse_aggregate(self) -> Aggregate:
        func_tok = self.advance()
        self.expect("PUNCT", "(", f"'(' after {func_tok.value}")
        arg: Node | None
        if func_tok.value == "COUNT" and self.cur.type == "OP" and self.cur.value == "*":
            self.advance()
            arg = None
        else:
            arg = self.parse_expr()
        self.expect("PUNCT", ")", ")")
        if func_tok.value == "COUNT" and isinstance(arg, Star):
            arg = None
        return Aggregate(func_tok.value, arg, func_tok.pos)


def parse(sql: str) -> SelectStatement:
    """Tokenize and parse ``sql`` into a :class:`SelectStatement`."""
    return Parser(tokenize(sql)).parse()
