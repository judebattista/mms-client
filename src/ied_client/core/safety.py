"""Operating modes (MOD-1..4), safety profile (SAF-1..3) and operator interaction.

Modes are guardrails against mistakes, not access control (MOD-4): anyone can pass --expert.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from ied_client import codes
from ied_client.codes import ErrorInfo


class Mode(StrEnum):
    STANDARD = "standard"
    EXPERT = "expert"


class SafetyProfile(StrEnum):
    LAB = "lab"
    STRICT = "strict"


class PolicyError(Exception):
    """An action was refused by the tool's own guardrails (not by the device)."""

    def __init__(self, error: ErrorInfo, message: str) -> None:
        self.error = error
        super().__init__(message)


class ConfirmationDeclined(Exception):
    """The operator did not confirm; nothing was sent."""


class Interaction(Protocol):
    """How the core asks the operator things. The CLI provides implementations."""

    interactive: bool

    def confirm(self, prompt: str, *, default: bool = False) -> bool: ...

    def confirm_typed(self, prompt: str, expected: str) -> bool: ...

    def choose(self, prompt: str, options: list[tuple[str, str]], *, default: str | None = None) -> str | None: ...

    def notify(self, message: str) -> None: ...


class NonInteractive:
    """Used for one-shot --json runs and the future pytest phase: never blocks (IDN-5)."""

    interactive = False

    def __init__(self, *, assume_yes_for_writes: bool = False) -> None:
        self.assume_yes_for_writes = assume_yes_for_writes
        self.messages: list[str] = []

    def confirm(self, prompt: str, *, default: bool = False) -> bool:
        return False

    def confirm_typed(self, prompt: str, expected: str) -> bool:
        return False

    def choose(self, prompt: str, options: list[tuple[str, str]], *, default: str | None = None) -> str | None:
        return default

    def notify(self, message: str) -> None:
        self.messages.append(message)


class Scripted:
    """Deterministic answers for tests: a queue of responses consumed in order."""

    interactive = True

    def __init__(self, answers: list[object] | None = None) -> None:
        self.answers = list(answers or [])
        self.prompts: list[str] = []
        self.options: list[list[tuple[str, str]]] = []
        self.messages: list[str] = []

    def _next(self, prompt: str) -> object:
        self.prompts.append(prompt)
        if not self.answers:
            raise AssertionError(f"unexpected prompt: {prompt}")
        return self.answers.pop(0)

    def confirm(self, prompt: str, *, default: bool = False) -> bool:
        return bool(self._next(prompt))

    def confirm_typed(self, prompt: str, expected: str) -> bool:
        return self._next(prompt) == expected

    def choose(self, prompt: str, options: list[tuple[str, str]], *, default: str | None = None) -> str | None:
        self.options.append(list(options))
        ans = self._next(prompt)
        return None if ans is None else str(ans)

    def notify(self, message: str) -> None:
        self.messages.append(message)


@dataclass
class Policy:
    """Mode + safety profile + --yes, and the confirmation rules derived from them."""

    mode: Mode = Mode.STANDARD
    safety: SafetyProfile = SafetyProfile.STRICT
    yes: bool = False

    @property
    def expert(self) -> bool:
        return self.mode is Mode.EXPERT

    def require_expert(self, what: str) -> None:
        if not self.expert:
            raise PolicyError(
                codes.tool("refused-standard-mode"),
                f"{what} needs expert mode (--expert or `set mode expert`). "
                "Modes are a guardrail against mistakes, not access control.",
            )

    def confirm_write(self, ui: Interaction, summary: str) -> None:
        """RW-4 / SAF-3: writes need a y/N confirmation unless --yes."""
        if self.yes:
            ui.notify(summary + "\n(confirmed by --yes)")
            return
        if not ui.interactive:
            raise ConfirmationDeclined(
                "write not confirmed: non-interactive run; pass --yes to confirm writes explicitly"
            )
        if not ui.confirm(summary + "\nWrite this value?", default=False):
            raise ConfirmationDeclined("write cancelled by the operator")

    def confirm_dangerous(self, ui: Interaction, summary: str, object_name: str, action: str = "control") -> None:
        """SAF-2 / SAF-3: controls, takeovers and similar need typed confirmation in strict
        mode, y/N in lab mode; --yes never skips them."""
        if not ui.interactive:
            raise ConfirmationDeclined(
                f"{action} not sent: it needs an operator confirmation, which a non-interactive run cannot give "
                "(--yes never confirms controls or takeovers)"
            )
        if self.safety is SafetyProfile.STRICT:
            ok = ui.confirm_typed(summary + f"\nType the object name ({object_name}) to confirm:", object_name)
        else:
            ok = ui.confirm(summary + f"\nSend this {action}?", default=False)
        if not ok:
            raise ConfirmationDeclined(f"{action} cancelled by the operator")
