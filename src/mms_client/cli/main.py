"""Entry point: ``mms-client [options] <command> <device> [args]`` or ``mms-client shell <device>``."""

from __future__ import annotations

import sys
from collections.abc import Sequence

from mms_client import __version__

from . import registry, runner
from .context import CliContext, stdin_is_tty
from .options import Options, global_options_help, global_parser, split_options
from .registry import UsageError
from .result import EXIT_INTERRUPTED, EXIT_USAGE
from .shell import GOOSE_NOTE, Shell

USAGE = """\
usage: mms-client [options] <command> <device> [arguments] [options]
       mms-client [options] shell [<device>]
       mms-client help [<command>]"""

ABOUT = """\
An IEC 61850 MMS client for the test rack: check that an IED's MMS server is configured and
communicating correctly, read and write values, operate controls, and troubleshoot associations.
Global options may be given before or after the command."""

EXIT_CODES = """\
exit codes:
  0    ok
  1    the command failed, or the device refused it
  2    usage error (bad command line)
  3    the association could not be opened, or was lost
  4    refused by the tool (mode/safety guardrail) or not confirmed
  130  interrupted (Ctrl-C)"""


def overall_help() -> str:
    lines = [USAGE, "", ABOUT, "", "commands:"]
    by_area: dict[str, list[registry.Command]] = {}
    for c in registry.all_commands():
        by_area.setdefault(c.area, []).append(c)
    for area in registry.AREAS:
        cmds = by_area.get(area, [])
        if not cmds and area != "Shell":
            continue
        lines.append(f"  {area}")
        if area == "Shell":
            lines.append(f"    {'shell':<20} Open one association and an interactive shell (CLI-1)")
        for c in cmds:
            tag = "  (shell only)" if c.shell_only else ""
            name = c.name + "".join(f", {a}" for a in c.aliases)
            lines.append(f"    {name:<20} {c.summary}{tag}")
    lines += ["", global_options_help(), "", EXIT_CODES, "", "notes:"]
    lines.append("  " + GOOSE_NOTE)
    lines.append("  Standard and expert modes are a guardrail against mistakes, not access control.")
    lines.append("  `mms-client help <command>` or `mms-client <command> -h` shows a command's arguments.")
    return "\n".join(lines)


def version_text() -> str:
    try:
        from mms_client.adapter import libiec61850_version, pyiec61850_version

        return f"mms-client {__version__} (libiec61850 {libiec61850_version()}, pyiec61850-ng {pyiec61850_version()})"
    except Exception:  # pragma: no cover
        return f"mms-client {__version__}"


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        return _main(args)
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return EXIT_INTERRUPTED


def _main(argv: list[str]) -> int:
    try:
        ns, rest = split_options(global_parser(), argv)
    except UsageError as e:
        print(f"mms-client: {e}", file=sys.stderr)
        print(USAGE, file=sys.stderr)
        return EXIT_USAGE
    options = Options.from_namespace(ns)
    if options.version:
        print(version_text())
        return 0
    if not rest or rest[0] in ("-h", "--help"):
        print(overall_help())
        return 0 if rest else EXIT_USAGE
    if rest[0] == "shell":
        return run_shell(options, rest[1:], argv)
    ctx = CliContext(options)
    ctx.argv = ["mms-client", *argv]
    return runner.run_oneshot(ctx, rest)


def run_shell(options: Options, rest: list[str], argv: list[str]) -> int:
    if any(a in ("-h", "--help") for a in rest):
        print("usage: mms-client [options] shell [<device>]\n\nOpen one association with <device> and an interactive shell.")
        return 0
    if len(rest) > 1:
        print(f"mms-client shell: unexpected arguments {rest[1:]}\nusage: mms-client [options] shell [<device>]", file=sys.stderr)
        return EXIT_USAGE
    interactive = stdin_is_tty()
    ctx = CliContext(options, in_shell=True, interactive=interactive)
    ctx.argv = ["mms-client", *argv]
    lines = None if interactive else sys.stdin
    shell = Shell(ctx, lines=lines, echo=not interactive and not options.json)
    return shell.run(rest[0] if rest else None)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
