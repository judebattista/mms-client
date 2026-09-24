"""The command registry shared by the shell and one-shot mode (CLI-6).

Every command is registered once, with its argparse definition and a handler
``(ctx, args) -> CommandResult``. The shell and ``mms-client <command> <device> …`` both look
commands up here, build the same parser (one-shot mode adds the ``device`` positional) and call the
same handler; the runner renders the result as text or as the JSON envelope (ARC-3).

Adding a command
----------------
Drop a module into ``mms_client/cli/commands/`` (every module there is imported automatically) and
decorate a handler::

    from mms_client.cli.registry import Needs, command
    from mms_client.cli.result import CommandResult

    def _args(p, oneshot):
        p.add_argument("ref", help="object reference")

    @command("frob", "One-line summary shown in `help`", area="Data", configure=_args, service="read")
    def frob(ctx, args) -> CommandResult:
        value = some_core_function(ctx.session, args.ref)      # all logic lives in the core (ARC-1)
        return CommandResult(data=value, text=lambda out: out.line(str(value)))

``needs`` says what the runner prepares before the handler runs: nothing, a device (a
:class:`~mms_client.core.session.Session` that is not yet associated), or an open association.
"""

from __future__ import annotations

import argparse
import importlib
import pkgutil
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .context import CliContext
    from .result import CommandResult

Handler = Callable[["CliContext", argparse.Namespace], "CommandResult"]
Configure = Callable[[argparse.ArgumentParser, bool], None]

# Areas in the order `help` shows them (SPEC §6.2).
AREAS = (
    "Connection",
    "Diagnosis",
    "Model",
    "Files",
    "Data",
    "Controls",
    "Reports",
    "Verification",
    "Help",
    "Session",
    "Inventory",
    "Shell",
)


class Needs(StrEnum):
    NOTHING = "nothing"  # no device at all (explain, help, set, log, inventory …)
    TARGET = "target"  # a device, but no association is opened by the runner (diagnose, export …)
    SESSION = "session"  # an open association (read, write, operate …)


class UsageError(Exception):
    """The command line could not be parsed (exit code 2)."""

    def __init__(self, message: str, usage: str | None = None) -> None:
        self.usage = usage
        super().__init__(message)


class ParserExit(Exception):
    """argparse wanted to exit (after printing ``--help``)."""

    def __init__(self, status: int = 0, message: str | None = None) -> None:
        self.status = status
        self.message = message
        super().__init__(message or "")


class CliArgumentParser(argparse.ArgumentParser):
    """An ArgumentParser that never calls ``sys.exit`` (the shell must survive a typo)."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("allow_abbrev", False)
        kwargs.setdefault("exit_on_error", False)
        super().__init__(*args, **kwargs)

    def error(self, message: str):  # type: ignore[override]
        raise UsageError(message, self.format_usage())

    def exit(self, status: int = 0, message: str | None = None):  # type: ignore[override]
        raise ParserExit(status, message)

    def parse_args(self, args=None, namespace=None):  # type: ignore[override]
        try:
            return super().parse_args(args, namespace)
        except argparse.ArgumentError as e:  # e.g. "unrecognized arguments" with exit_on_error=False
            raise UsageError(str(e), self.format_usage()) from e

    def parse_known_args(self, args=None, namespace=None):  # type: ignore[override]
        try:
            return super().parse_known_args(args, namespace)
        except argparse.ArgumentError as e:  # exit_on_error=False raises these directly
            raise UsageError(str(e), self.format_usage()) from e


@dataclass
class Command:
    name: str  # "read", or a two-word name such as "files ls"
    summary: str
    handler: Handler
    area: str
    needs: Needs = Needs.SESSION
    configure: Configure | None = None
    description: str | None = None
    shell_only: bool = False
    aliases: tuple[str, ...] = ()
    needs_model: bool = True  # browse the model before the handler runs (MDL-4 cache)
    service: str | None = None  # HintContext.service for failures of this command (EXP-3)
    examples: tuple[str, ...] = ()

    @property
    def takes_device(self) -> bool:
        """One-shot mode takes ``<device>`` right after the command name."""
        return self.needs is not Needs.NOTHING

    @property
    def words(self) -> tuple[str, ...]:
        return tuple(self.name.split())

    def parser(self, *, oneshot: bool) -> CliArgumentParser:
        prog = f"mms-client {self.name}" if oneshot else self.name
        epilog = None
        if self.examples:
            epilog = "examples:\n" + "\n".join(f"  {e}" for e in self.examples)
        p = CliArgumentParser(
            prog=prog,
            description=self.description or self.summary,
            epilog=epilog,
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        if oneshot and self.takes_device:
            p.add_argument("device", help="device: inventory name, or IP address / host[:port] (INV-5)")
        if self.configure is not None:
            self.configure(p, oneshot)
        return p


_REGISTRY: dict[str, Command] = {}
_ALIASES: dict[str, str] = {}
_loaded = False


def register(cmd: Command) -> Command:
    if cmd.area not in AREAS:
        raise ValueError(f"{cmd.name}: unknown area {cmd.area!r}")
    if cmd.name in _REGISTRY or cmd.name in _ALIASES:
        raise ValueError(f"command {cmd.name!r} registered twice")
    _REGISTRY[cmd.name] = cmd
    for a in cmd.aliases:
        _ALIASES[a] = cmd.name
    return cmd


def unregister(name: str) -> None:
    cmd = _REGISTRY.pop(name, None)
    if cmd is not None:
        for a in cmd.aliases:
            _ALIASES.pop(a, None)


def command(name: str, summary: str, *, area: str, **kwargs: Any) -> Callable[[Handler], Handler]:
    """Decorator: register ``handler`` as command ``name``."""

    def deco(handler: Handler) -> Handler:
        register(Command(name=name, summary=summary, handler=handler, area=area, **kwargs))
        return handler

    return deco


def load_commands() -> None:
    """Import every module of :mod:`mms_client.cli.commands` (each registers its commands)."""
    global _loaded
    if _loaded:
        return
    _loaded = True
    from . import commands

    for mod in sorted(pkgutil.iter_modules(commands.__path__), key=lambda m: m.name):
        if not mod.name.startswith("_"):
            importlib.import_module(f"{commands.__name__}.{mod.name}")


def all_commands() -> list[Command]:
    load_commands()
    return sorted(_REGISTRY.values(), key=lambda c: (AREAS.index(c.area), c.name))


def get(name: str) -> Command | None:
    load_commands()
    name = _ALIASES.get(name, name)
    return _REGISTRY.get(name)


def group_words() -> dict[str, list[str]]:
    """First words of two-word commands → their second words (``files`` → ``ls``, ``get``)."""
    load_commands()
    out: dict[str, list[str]] = {}
    for c in _REGISTRY.values():
        if len(c.words) == 2:
            out.setdefault(c.words[0], []).append(c.words[1])
    return {k: sorted(v) for k, v in out.items()}


def resolve(tokens: list[str]) -> tuple[Command | None, int]:
    """The command named by the first one or two tokens, and how many tokens it used."""
    load_commands()
    if not tokens:
        return None, 0
    if len(tokens) >= 2:
        two = f"{tokens[0]} {tokens[1]}"
        if two in _REGISTRY or two in _ALIASES:
            return get(two), 2
    cmd = get(tokens[0])
    return (cmd, 1) if cmd is not None else (None, 0)


def top_level_words() -> list[str]:
    """Words that can start a command line (for completion and suggestions)."""
    load_commands()
    words = {c.words[0] for c in _REGISTRY.values()} | set(_ALIASES)
    return sorted(words)
