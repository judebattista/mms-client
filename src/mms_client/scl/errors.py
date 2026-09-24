"""Exceptions raised by the SCL package."""

from __future__ import annotations


class SclError(Exception):
    """An SCL file is unusable, or a reference inside it cannot be resolved.

    ``source`` is the file name (or ``"<bytes>"``) and ``line`` the 1-based line number of the
    offending element when known. ``str(err)`` renders as ``source:line: message``.
    """

    def __init__(self, message: str, *, source: str | None = None, line: int | None = None) -> None:
        self.message = message
        self.source = source
        self.line = line
        super().__init__(self.__str__())

    def __str__(self) -> str:
        return f"{location(self.source, self.line)}{self.message}"


def location(source: str | None, line: int | None) -> str:
    """``"file:line: "`` prefix for messages (empty when nothing is known)."""
    if source and line:
        return f"{source}:{line}: "
    if source:
        return f"{source}: "
    if line:
        return f"line {line}: "
    return ""
