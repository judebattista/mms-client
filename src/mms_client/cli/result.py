"""What a command handler returns: data for the JSON envelope plus a text renderer."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from mms_client.codes import ErrorInfo

if TYPE_CHECKING:
    from .render import Output

# Exit codes of one-shot mode.
EXIT_OK = 0
EXIT_FAILED = 1  # the command failed or the device refused
EXIT_USAGE = 2  # bad command line
EXIT_CONNECT = 3  # the association could not be opened (or is gone)
EXIT_REFUSED = 4  # refused by the tool's policy, or the operator did not confirm
EXIT_INTERRUPTED = 130  # Ctrl-C

Renderer = Callable[["Output"], None]


@dataclass
class CommandResult:
    """The outcome of one command.

    ``data`` becomes ``result`` in the JSON envelope (ARC-3); ``text`` renders the same data for a
    person. A failed command sets ``ok=False`` and ``error`` (the raw code, CLI-7); the runner then
    prints the message, the code and the catalogue hint (EXP-1) after the renderer's output.
    """

    data: Any = None
    text: Renderer | None = None
    ok: bool = True
    error: ErrorInfo | None = None
    message: str | None = None
    exit_code: int | None = None
    hint_context: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # e.g. cleanup notes (RPT-7)
    show_hint: bool = True
    interrupted: bool = False
    exit_shell: bool = False
    hint: Any = None  # explain.Hint, filled in by the runner

    @property
    def code(self) -> int:
        if self.exit_code is not None:
            return self.exit_code
        return EXIT_OK if self.ok else EXIT_FAILED


def failure(
    error: ErrorInfo,
    message: str,
    *,
    exit_code: int = EXIT_FAILED,
    data: Any = None,
    text: Renderer | None = None,
    hint_context: dict[str, Any] | None = None,
    show_hint: bool = True,
) -> CommandResult:
    return CommandResult(
        data=data,
        text=text,
        ok=False,
        error=error,
        message=message,
        exit_code=exit_code,
        hint_context=dict(hint_context or {}),
        show_hint=show_hint,
    )
