"""Global options (accepted before or after the command) and per-line options in the shell."""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass, fields

from ied_client import ENV_PREFIX, TOOL_NAME

from .registry import CliArgumentParser

# Options that apply to a whole session (association, log, mode). In the shell they are set when
# the shell starts, or with `connect` / `set`; they are not accepted on individual lines.
SESSION_OPTIONS = ("inventory", "expert", "as_client", "bind", "port", "protocol", "log_dir", "no_log", "timeout")
# Options that apply to one command and may be given on any shell line as well.
LINE_OPTIONS = ("json", "terse", "yes", "orcat", "or_ident", "test", "no_interlock", "no_synchrocheck")

SHELL_ALTERNATIVES = {
    "--expert": "use `set mode expert` in the shell",
    "--inventory": f"the inventory is chosen when the shell starts ({TOOL_NAME} -i FILE shell <device>)",
    "-i": f"the inventory is chosen when the shell starts ({TOOL_NAME} -i FILE shell <device>)",
    "--protocol": f"the protocol is chosen when the shell starts ({TOOL_NAME} --protocol NAME shell <device>)",
    "--as": "use `connect <device> --as CLIENT`",
    "--bind": "use `connect <device> --bind IP`",
    "--port": "use `connect <device> --port N`",
    "--log-dir": "the log directory is chosen when the shell starts",
    "--no-log": "logging is chosen when the shell starts",
    "--timeout": "the timeout is chosen when the shell starts",
}


@dataclass
class Options:
    """Session-wide options. ``None`` means "not given" (line options fall back to these)."""

    inventory: str | None = None
    expert: bool = False
    terse: bool = False
    json: bool = False
    yes: bool = False
    as_client: str | None = None
    bind: str | None = None
    port: int | None = None
    protocol: str | None = None
    orcat: str | None = None
    or_ident: str | None = None
    log_dir: str | None = None
    no_log: bool = False
    timeout: int | None = None
    test: bool = False
    no_interlock: bool = False
    no_synchrocheck: bool = False
    version: bool = False

    @classmethod
    def from_namespace(cls, ns: argparse.Namespace) -> Options:
        o = cls()
        for f in fields(cls):
            v = getattr(ns, f.name, None)
            if v is not None:
                setattr(o, f.name, v)
        if o.inventory is None:
            o.inventory = os.environ.get(f"{ENV_PREFIX}_INVENTORY") or None
        return o


@dataclass
class LineOptions:
    """Per-command overrides given on a shell line (all ``None`` = use the session's options)."""

    json: bool | None = None
    terse: bool | None = None
    yes: bool | None = None
    orcat: str | None = None
    or_ident: str | None = None
    test: bool | None = None
    no_interlock: bool | None = None
    no_synchrocheck: bool | None = None

    @classmethod
    def from_namespace(cls, ns: argparse.Namespace) -> LineOptions:
        return cls(**{f.name: getattr(ns, f.name, None) for f in fields(cls)})


def _add_line_options(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("output and confirmation")
    g.add_argument("--json", action="store_true", default=None, help="print one versioned JSON document (ARC-3); never prompts")
    g.add_argument("--terse", action="store_true", default=None, help="no explanatory hints (CLI-8)")
    g.add_argument("--yes", action="store_true", default=None, help="confirm writes; never confirms controls or takeovers (SAF-3)")
    c = p.add_argument_group("controls")
    c.add_argument("--orcat", metavar="N|NAME", default=None, help="originator category: 1-3 or bay/station/remote (0, 4-8 need --expert)")
    c.add_argument("--or-ident", dest="or_ident", metavar="TEXT", default=None, help=f"originator identification (default {TOOL_NAME}/<user>@<host>)")
    c.add_argument("--test", action="store_true", default=None, help="set the Test flag on controls (CTL-6)")
    c.add_argument("--no-interlock", dest="no_interlock", action="store_true", default=None, help="interlock check off (expert only, logged; CTL-9)")
    c.add_argument("--no-synchrocheck", dest="no_synchrocheck", action="store_true", default=None, help="synchrocheck off (expert only, logged; CTL-9)")


def global_parser() -> CliArgumentParser:
    """All global options. Parsed with ``parse_known_args`` so they may appear anywhere."""
    p = CliArgumentParser(prog=TOOL_NAME, add_help=False)
    s = p.add_argument_group("session")
    s.add_argument("-i", "--inventory", metavar="FILE", default=None, help=f"experiment inventory (default: ${ENV_PREFIX}_INVENTORY)")
    s.add_argument("--expert", action="store_true", default=None, help="expert mode (MOD-3); a guardrail, not access control")
    s.add_argument("--as", dest="as_client", metavar="CLIENT", default=None, help="act as this client from the inventory: its source IP and RCBs (NBR-1)")
    s.add_argument("--bind", metavar="IP", default=None, help="bind the association to this local IP (NBR-2)")
    s.add_argument("--port", type=int, metavar="N", default=None, help="port (default: the inventory's, or the protocol's)")
    s.add_argument("--protocol", metavar="NAME", default=None,
                   help="protocol module for a device given by address, and for discover (default: the inventory's, or mms)")
    s.add_argument("--log-dir", dest="log_dir", metavar="DIR", default=None, help=f"session log directory (default ~/.local/state/{TOOL_NAME}/sessions)")
    s.add_argument("--no-log", dest="no_log", action="store_true", default=None, help="do not write a session log")
    s.add_argument("--timeout", type=int, metavar="MS", default=None, help="connect and request timeout in milliseconds")
    s.add_argument("--version", action="store_true", default=None, help="show the version and exit")
    _add_line_options(p)
    return p


def line_parser() -> CliArgumentParser:
    """The options accepted on any shell line."""
    p = CliArgumentParser(prog="", add_help=False)
    _add_line_options(p)
    return p


def split_options(parser: CliArgumentParser, tokens: list[str]) -> tuple[argparse.Namespace, list[str]]:
    """Take the parser's options out of ``tokens`` wherever they are; keep the rest in order.

    Everything after ``--`` is left alone (so a value such as ``--json`` can still be written).
    """
    if "--" in tokens:
        i = tokens.index("--")
        head, tail = tokens[:i], tokens[i:]
    else:
        head, tail = tokens, []
    ns, rest = parser.parse_known_args(head)
    return ns, rest + tail


def global_options_help() -> str:
    text = global_parser().format_help()
    # drop argparse's own usage line: main() prints its own
    lines = text.splitlines()
    while lines and not lines[0].strip().endswith(":"):
        lines.pop(0)
    return "\n".join(lines)
