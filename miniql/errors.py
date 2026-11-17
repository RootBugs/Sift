"""Error types for miniql."""
from __future__ import annotations


class MiniQLError(Exception):
    """Base class for all miniql errors."""


class QueryError(MiniQLError):
    """A runtime error (unknown table/column, bad aggregate usage).

    Carries the source position of the offending token when known.
    """

    def __init__(self, message: str, pos: int | None = None) -> None:
        self.message = message
        self.pos = pos
        super().__init__(self.__str__())

    def __str__(self) -> str:
        if self.pos is None:
            return self.message
        return f"{self.message} (at position {self.pos})"


class ParseError(MiniQLError):
    """A syntax error; always carries the position of the bad token."""

    def __init__(self, message: str, pos: int) -> None:
        self.message = message
        self.pos = pos
        super().__init__(self.__str__())

    def __str__(self) -> str:
        return f"Parse error at position {self.pos}: {self.message}"
