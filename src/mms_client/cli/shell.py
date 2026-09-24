"""The interactive shell (CLI-1 … CLI-4): one association, reused by every command.

The read-eval loop takes its lines from prompt_toolkit on a terminal, or from any iterable of
strings (a script on stdin, or a test), so it can run without a TTY.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Iterator
from pathlib import Path

from mms_client import codes
from mms_client.core.safety import Mode

from . import runner
from .context import CliContext
from .render import STYLE_EXPERT, STYLE_NOTE
from .result import CommandResult

OR_CAT_SHORT = {1: "bay", 2: "station", 3: "remote"}

GOOSE_NOTE = (
    "This tool speaks MMS only: it cannot see GOOSE or Sampled Values, so it cannot check "
    "bay-level paths that use them."
)


def history_path() -> Path:
    state = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(state) / "mms-client" / "history"


def or_cat_label(value: int) -> str:
    return OR_CAT_SHORT.get(value) or codes.OR_CATS.get(value, str(value))


def prompt_fragments(
    device: str | None,
    cwd: tuple[str, ...],
    mode: Mode,
    or_cat: int | None,
    *,
    connected: bool = True,
) -> list[tuple[str, str]]:
    """The prompt as (style, text) pieces, e.g. ``[std] relay-F12:/CTRL/CSWI1 [orCat=remote]> ``.

    The mode is always shown (MOD-1): ``[std]`` in standard mode, a highlighted ``[EXPERT]`` in
    expert mode. The orCat appears once one is set (CLI-4).
    """
    frags: list[tuple[str, str]] = []
    if mode is Mode.EXPERT:
        frags.append(("bg:ansired fg:ansiwhite bold", "[EXPERT]"))
    else:
        frags.append(("fg:ansigray", "[std]"))
    frags.append(("", " "))
    if device is None:
        frags.append(("fg:ansiyellow", "(no device)"))
    else:
        frags.append(("bold", device))
        if not connected:
            frags.append(("fg:ansiyellow bold", " (not connected)"))
        frags.append(("", ":"))
        frags.append(("fg:ansicyan", "/" + "/".join(cwd)))
    if or_cat is not None:
        frags.append(("", f" [orCat={or_cat_label(or_cat)}]"))
    frags.append(("", "> "))
    return frags


def prompt_text(*args, **kwargs) -> str:
    return "".join(t for _, t in prompt_fragments(*args, **kwargs))


class Shell:
    def __init__(
        self,
        ctx: CliContext,
        *,
        lines: Iterable[str] | None = None,
        echo: bool | None = None,
        history_file: Path | None = None,
    ) -> None:
        self.ctx = ctx
        self._lines: Iterator[str] | None = iter(lines) if lines is not None else None
        self.echo = (lines is not None) if echo is None else echo
        self.history_file = history_file
        self.running = False
        self._pt = None
        self._lost_reported: object | None = None
        self.results: list[CommandResult] = []

    # ------------------------------------------------------------------ prompt
    def fragments(self) -> list[tuple[str, str]]:
        s = self.ctx.session
        if s is None:
            return prompt_fragments(None, (), self.ctx.mode, None)
        return prompt_fragments(s.device_name, s.cwd, s.policy.mode, s.or_cat, connected=s.connected)

    def prompt(self) -> str:
        return "".join(t for _, t in self.fragments())

    def _prompt_session(self):
        if self._pt is None:
            from prompt_toolkit import PromptSession
            from prompt_toolkit.history import FileHistory, InMemoryHistory

            from .completion import ShellCompleter
            from .errors import catalogue

            hist_path = self.history_file or history_path()
            try:
                hist_path.parent.mkdir(parents=True, exist_ok=True)
                history = FileHistory(str(hist_path))
            except OSError:
                history = InMemoryHistory()

            def vocabulary():
                cat = catalogue()
                return cat.vocabulary() if cat is not None else []

            completer = ShellCompleter(
                lambda: self.ctx.session.cached_model if self.ctx.session is not None else None,
                lambda: self.ctx.session.cwd if self.ctx.session is not None else (),
                vocabulary,
            )
            self._pt = PromptSession(history=history, completer=completer, complete_while_typing=False)
        return self._pt

    def read_line(self) -> str | None:
        """The next line, or None at end of input (Ctrl-D)."""
        if self._lines is not None:
            try:
                line = next(self._lines)
            except StopIteration:
                return None
            if self.echo:
                self.ctx.out.line(self.prompt() + line.rstrip("\n"), style=STYLE_NOTE)
            return line.rstrip("\n")
        from prompt_toolkit.formatted_text import FormattedText

        pt = self._prompt_session()
        while True:
            try:
                return pt.prompt(lambda: FormattedText(self.fragments()), refresh_interval=1.0)
            except KeyboardInterrupt:
                continue  # Ctrl-C at the prompt clears the line
            except EOFError:
                return None

    # ------------------------------------------------------------------ loop
    def banner(self) -> None:
        out = self.ctx.out
        out.line("mms-client shell. `help` lists the commands; Tab completes commands and object references.")
        out.line("Ctrl-C stops a running command; Ctrl-D or `exit` leaves (RCB changes are undone on the way out).")
        if self.ctx.expert:
            out.parts(("[EXPERT] ", STYLE_EXPERT), "Expert mode: guardrails are relaxed (a guardrail, not access control).")
        out.note(GOOSE_NOTE)

    def run(self, device: str | None = None) -> int:
        self.running = True
        if not self.ctx.json:
            self.banner()
        if device is not None:
            self.run_line("connect " + _quote(device))
        while self.running:
            self.check_connection()
            line = self.read_line()
            if line is None:
                break
            self.run_line(line)
        self.shutdown()
        return 0

    def run_line(self, line: str) -> CommandResult | None:
        try:
            result = runner.run_line(self.ctx, line)
        except KeyboardInterrupt:  # Ctrl-C while rendering: keep the shell
            self.ctx.out.note("Cancelled.")
            return None
        if result is not None:
            self.results.append(result)
            if result.exit_shell:
                self.running = False
        self.check_connection()
        return result

    def check_connection(self) -> None:
        """Tell the operator once when the association is lost; the shell stays (RPT-7)."""
        s = self.ctx.session
        if s is None or not s.connection_lost.is_set() or self._lost_reported is s.client:
            return
        if s.client is None:
            return
        self._lost_reported = s.client
        out = self.ctx.out
        out.warn(f"the association with {s.device_name} was lost. The shell stays open; `connect` reconnects.")
        for sub in list(s.subscriptions.values()):
            try:
                sub.stop()
            except Exception:  # the association is gone; the notes below say what lingers
                sub.active = False
                s.subscriptions.pop(sub.reference, None)
        for note in s.run_cleanups():
            out.note(f"Cleanup: {note}")

    def shutdown(self) -> None:
        s = self.ctx.session
        log_path = s.log.path if s is not None else None
        notes = self.ctx.close_session()
        out = self.ctx.out
        for n in notes:
            out.note(f"Cleanup: {n}")
        if log_path is not None and not self.ctx.json:
            out.note(f"Session log: {log_path}")


def _quote(text: str) -> str:
    import shlex

    return shlex.quote(text)
