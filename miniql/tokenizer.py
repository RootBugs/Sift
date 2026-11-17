"""Tokenizer for the miniql SQL subset.

Produces a list of :class:`Token` objects, each carrying its type, the
semantic value, and the 0-based character position where the token
starts — parse errors report that position verbatim.

Token types
-----------
``KEYWORD``  one of the reserved words (case-insensitive; the value is
             stored upper-cased)
``IDENT``    an identifier (table/column name), value lower-cased so
             queries are case-insensitive for names
``NUMBER``   an integer or floating-point literal
``STRING``   a single-quoted string literal; ``''`` escapes a quote
``OP``       one of ``= != <> < <= > >=`` or ``*`` (``*`` is punctuation
             when used as the COUNT argument / select star)
``PUNCT``    ``, ( ) ;``
``EOF``      end of input (always the final token)
"""
from __future__ import annotations

from dataclasses import dataclass

from .errors import ParseError

KEYWORDS = {
    "SELECT", "FROM", "JOIN", "INNER", "ON", "WHERE", "GROUP", "BY",
    "ORDER", "ASC", "DESC", "LIMIT", "AND", "OR", "NOT", "AS", "COUNT",
    "SUM", "AVG", "MIN", "MAX", "NULL",
}

AGGREGATES = {"COUNT", "SUM", "AVG", "MIN", "MAX"}

# two-character operators must be tried before one-character ones
_OPERATORS = ["!=", "<>", "<=", ">=", "=", "<", ">"]
_PUNCT = set(",();.")


@dataclass(frozen=True)
class Token:
    type: str
    value: object
    pos: int

    def __repr__(self) -> str:  # compact, useful in test failures
        return f"Token({self.type}, {self.value!r}, {self.pos})"


def tokenize(text: str) -> list[Token]:
    """Convert ``text`` into a token list ending with an EOF token."""
    tokens: list[Token] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch.isspace():
            i += 1
            continue
        if ch == "-" and i + 1 < n and text[i + 1].isdigit():
            # negative number literal
            start = i
            i += 1
            while i < n and (text[i].isdigit() or text[i] == "."):
                i += 1
            tokens.append(_number(text[start:i], start))
            continue
        if ch.isdigit() or (ch == "." and i + 1 < n and text[i + 1].isdigit()):
            start = i
            while i < n and (text[i].isdigit() or text[i] in ".eE"):
                if text[i] in "eE" and i + 1 < n and text[i + 1] in "+-":
                    i += 1
                i += 1
            tokens.append(_number(text[start:i], start))
            continue
        if ch.isalpha() or ch == "_":
            start = i
            while i < n and (text[i].isalnum() or text[i] == "_"):
                i += 1
            word = text[start:i]
            upper = word.upper()
            if upper in KEYWORDS:
                value = None if upper == "NULL" else upper
                tokens.append(Token("KEYWORD", value, start))
            else:
                tokens.append(Token("IDENT", word.lower(), start))
            continue
        if ch == "'":
            start = i
            i += 1
            parts: list[str] = []
            closed = False
            while i < n:
                if text[i] == "'":
                    if i + 1 < n and text[i + 1] == "'":
                        parts.append("'")
                        i += 2
                        continue
                    i += 1
                    closed = True
                    break
                parts.append(text[i])
                i += 1
            if not closed:
                raise ParseError("unterminated string literal", start)
            tokens.append(Token("STRING", "".join(parts), start))
            continue
        matched = False
        for op in _OPERATORS:
            if text.startswith(op, i):
                tokens.append(Token("OP", "!=" if op == "<>" else op, i))
                i += len(op)
                matched = True
                break
        if matched:
            continue
        if ch == "*":
            tokens.append(Token("OP", "*", i))
            i += 1
            continue
        if ch in _PUNCT:
            tokens.append(Token("PUNCT", ch, i))
            i += 1
            continue
        raise ParseError(f"unexpected character {ch!r}", i)
    tokens.append(Token("EOF", None, n))
    return tokens


def _number(literal: str, pos: int) -> Token:
    if any(c in literal for c in ".eE"):
        try:
            return Token("NUMBER", float(literal), pos)
        except ValueError:
            raise ParseError(f"invalid number {literal!r}", pos) from None
    try:
        return Token("NUMBER", int(literal), pos)
    except ValueError:
        raise ParseError(f"invalid number {literal!r}", pos) from None
