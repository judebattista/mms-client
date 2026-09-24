"""Operator interaction for the core's confirmations and questions (``core.safety.Interaction``).

* :class:`PromptInteraction` asks at the terminal with prompt_toolkit (y/N, typed confirmation,
  choose from a list with a "don't know"-style default).
* :class:`TextNonInteractive` never blocks (IDN-5): it answers "no" to every confirmation and takes
  the default of every question, and shows notifications as text unless JSON output is on.
"""

from __future__ import annotations

from collections.abc import Callable

from mms_client.core.safety import NonInteractive

from .render import Output

PromptFn = Callable[[str], str]


def _pt_prompt(message: str) -> str:
    from prompt_toolkit import prompt

    return prompt(message)


class TextNonInteractive(NonInteractive):
    """Non-interactive runs (``--json``, stdin not a terminal): never blocks, never confirms."""

    def __init__(self, out: Output | None = None, *, assume_yes_for_writes: bool = False) -> None:
        super().__init__(assume_yes_for_writes=assume_yes_for_writes)
        self.out = out

    def notify(self, message: str) -> None:
        super().notify(message)
        if self.out is not None:
            self.out.block(message)


class PromptInteraction:
    """Asks the operator at the terminal. ``prompt_fn`` is replaceable for tests."""

    interactive = True

    def __init__(self, out: Output, prompt_fn: PromptFn | None = None) -> None:
        self.out = out
        self._prompt = prompt_fn or _pt_prompt
        self.prompts: list[str] = []

    def _ask(self, prompt_text: str, question: str) -> str | None:
        self.prompts.append(prompt_text)
        self.out.block(prompt_text)
        try:
            return self._prompt(question)
        except EOFError:
            return None

    def confirm(self, prompt: str, *, default: bool = False) -> bool:
        ans = self._ask(prompt, "[Y/n] " if default else "[y/N] ")
        if ans is None:
            return False
        a = ans.strip().lower()
        if not a:
            return default
        return a in ("y", "yes")

    def confirm_typed(self, prompt: str, expected: str) -> bool:
        ans = self._ask(prompt, "> ")
        if ans is None:
            return False
        ok = ans.strip() == expected
        if not ok:
            self.out.warn(f"{ans.strip()!r} does not match {expected!r}; nothing was sent.")
        return ok

    def choose(self, prompt: str, options: list[tuple[str, str]], *, default: str | None = None) -> str | None:
        labels = dict(options)
        lines = [prompt]
        for i, (_key, label) in enumerate(options, 1):
            lines.append(f"  {i}) {label}")
        if default is not None:
            question = f"Choose 1-{len(options)} [Enter = {labels.get(default, default)}]: "
        else:
            question = f"Choose 1-{len(options)} [Enter = cancel]: "
        text = "\n".join(lines)
        for attempt in range(3):
            ans = self._ask(text if attempt == 0 else "", question)
            if ans is None:
                return default
            a = ans.strip()
            if not a:
                return default
            if a.isdigit() and 1 <= int(a) <= len(options):
                return options[int(a) - 1][0]
            for key, label in options:
                if a.lower() in (key.lower(), label.lower()):
                    return key
            self.out.warn(f"{a!r} is not one of the options.")
        return default

    def notify(self, message: str) -> None:
        self.out.block(message)
