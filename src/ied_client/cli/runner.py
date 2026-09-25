"""Run one command: parse its arguments, prepare the session, call the handler, render the result.

The same path serves one-shot mode and every shell line (CLI-6); only the preparation differs
(one-shot creates the session for ``<device>``; the shell reuses its session).
"""

from __future__ import annotations

import argparse
import contextlib
import difflib
import json
import shlex
from collections.abc import Iterator
from typing import Any

from ied_client import TOOL_NAME, codes
from ied_client.core.results import envelope, jsonable

from . import errors
from .context import CliContext
from .interact import TextNonInteractive
from .options import SHELL_ALTERNATIVES, LineOptions, line_parser, split_options
from .registry import Command, Needs, ParserExit, UsageError, group_words, resolve, top_level_words
from .render import STYLE_NOTE, code_label
from .result import EXIT_INTERRUPTED, EXIT_USAGE, CommandResult, failure

# ---------------------------------------------------------------------------------- lookup


def lookup(tokens: list[str], *, in_shell: bool) -> tuple[Command, list[str]]:
    """The command named by ``tokens`` and its remaining arguments; UsageError if none."""
    cmd, n = resolve(tokens)
    if cmd is None:
        word = tokens[0] if tokens else ""
        groups = group_words()
        if word in groups:
            raise UsageError(f"`{word}` needs a subcommand: " + ", ".join(f"{word} {w}" for w in groups[word]))
        close = difflib.get_close_matches(word, top_level_words(), n=3, cutoff=0.6)
        msg = f"unknown command {word!r}"
        if close:
            msg += "; did you mean " + " or ".join(f"`{c}`" for c in close) + "?"
        raise UsageError(msg + " (`help` lists the commands)")
    if cmd.shell_only and not in_shell:
        raise UsageError(f"`{cmd.name}` only makes sense inside the shell ({TOOL_NAME} shell <device>)")
    return cmd, tokens[n:]


def parse(cmd: Command, tokens: list[str], *, oneshot: bool) -> argparse.Namespace:
    try:
        return cmd.parser(oneshot=oneshot).parse_args(tokens)
    except UsageError as e:
        for tok in tokens:
            alt = SHELL_ALTERNATIVES.get(tok.split("=", 1)[0])
            if alt and not oneshot:
                raise UsageError(f"{tok} applies to the whole session: {alt}", e.usage) from None
        raise


def args_json(args: argparse.Namespace) -> dict[str, Any]:
    return {k: jsonable(v) for k, v in vars(args).items()}


# ---------------------------------------------------------------------------------- execution
@contextlib.contextmanager
def _command_overrides(ctx: CliContext) -> Iterator[None]:
    """Apply per-command options (shell line options, --yes, --or-ident) to the session."""
    s = ctx.session
    saved_ident = None
    ui = ctx.ui
    if isinstance(ui, TextNonInteractive):
        ui.messages.clear()  # the envelope carries this command's notifications only
    if s is not None:
        s.ui = ui
        s.policy.yes = bool(ctx.opt("yes"))
        if ctx.line.or_ident:
            saved_ident = s.or_ident
            s.or_ident = ctx.line.or_ident
    try:
        yield
    finally:
        if s is not None:
            s.policy.yes = bool(ctx.options.yes)
            if saved_ident is not None:
                s.or_ident = saved_ident


def call(ctx: CliContext, cmd: Command, args: argparse.Namespace) -> CommandResult:
    """Prepare what the command needs, then run its handler. Never raises (except SystemExit)."""
    before = ctx.session.last_error if ctx.session is not None else None
    try:
        with _command_overrides(ctx):
            if cmd.needs is Needs.SESSION:
                if ctx.in_shell:
                    ctx.require_session()
                else:
                    ctx.connect()
                if cmd.needs_model:
                    ctx.ensure_model()
            result = cmd.handler(ctx, args)
    except KeyboardInterrupt:
        result = CommandResult(ok=False, message="interrupted (Ctrl-C)", error=codes.tool("interrupted"),
                               exit_code=EXIT_INTERRUPTED, interrupted=True, show_hint=False)
    except Exception as e:  # mapped to exit codes; internal errors are reported, not raised
        result = errors.from_exception(ctx, cmd, e)
        if result.error is not None and result.error.name == "internal-error" and ctx.session is not None:
            ctx.session.log.write("error", error=result.error, message=result.message, traceback=(result.data or {}).get("traceback"))
    finalize(ctx, cmd, result, before)
    return result


def finalize(ctx: CliContext, cmd: Command | None, result: CommandResult, before: Any = None) -> None:
    """Hint (EXP-1) and bookkeeping for `explain last` and the log."""
    s = ctx.session
    if not result.ok and result.error is not None:
        values = errors.context_for(ctx, cmd, result)
        if s is not None and s.last_error is before and not result.interrupted:
            # the core did not record this failure (e.g. a policy refusal): record it for `explain last`
            s.remember_error(result.error, {k: v for k, v in values.items() if k in errors.HINT_FIELDS}, result.message or "")
        if result.show_hint:
            result.hint = errors.hint_for(result.error, values)
    if s is not None:
        s.log.write(
            "command-result",
            command=cmd.name if cmd else None,
            ok=result.ok,
            exit_code=result.code,
            error=result.error,
            message=result.message,
        )


# ---------------------------------------------------------------------------------- output
def emit(ctx: CliContext, cmd_name: str, result: CommandResult) -> None:
    if ctx.json:
        print(json.dumps(to_envelope(ctx, cmd_name, result), indent=2, ensure_ascii=False, default=str), file=ctx.stdout)
        ctx.stdout.flush()
        return
    out = ctx.out
    if result.text is not None:
        try:
            result.text(out)
        except Exception as e:  # a rendering bug must not lose the result or the shell
            out.error(f"internal error while showing the result ({type(e).__name__}: {e}); `--json` shows the data")
    for w in result.warnings:
        out.warn(w)
    if not result.ok:
        render_failure(ctx, result)
    for n in result.notes:
        out.note(n)


def render_failure(ctx: CliContext, result: CommandResult) -> None:
    out = ctx.out
    if result.interrupted:
        out.note("Interrupted." if not ctx.in_shell else "Cancelled.")
        return
    out.error(result.message or "the command failed")
    if result.error is not None and result.error.name != "usage-error":
        out.line(f"  code: {code_label(result.error)}", style=STYLE_NOTE)  # CLI-7: always the raw code
    if result.error is not None and result.error.name == "usage-error":
        usage = (result.data or {}).get("usage")
        if usage:
            out.line(usage)
    hint = result.hint
    if hint is not None and not ctx.terse:
        out.parts(("  Hint: ", "bold"), hint.render())
        out.line(f"  More: explain {hint.entry_id}", style=STYLE_NOTE)


def to_envelope(ctx: CliContext, cmd_name: str, result: CommandResult) -> dict[str, Any]:
    extra: dict[str, Any] = {}
    if not result.ok:
        err = result.error.to_json() if result.error is not None else None
        if err is not None:
            err["key"] = result.error.key
        extra["error"] = {**(err or {}), "message": result.message}
        extra["hint"] = result.hint.to_json() if result.hint is not None and hasattr(result.hint, "to_json") else None
    if result.warnings:
        extra["warnings"] = result.warnings
    if result.notes:
        extra["notes"] = result.notes
    if result.interrupted:
        extra["interrupted"] = True
    msgs = getattr(ctx.ui, "messages", None)
    if msgs:
        extra["messages"] = list(msgs)
    extra["exit_code"] = result.code
    return envelope(cmd_name, result.data, device=ctx.device_label, ok=result.ok, protocol=ctx.protocol_module(), **extra)


# ---------------------------------------------------------------------------------- one-shot
def run_oneshot(ctx: CliContext, tokens: list[str]) -> int:
    """``mms-client [globals] <command> [<device>] [args]`` (global options already removed)."""
    try:
        cmd, rest = lookup(tokens, in_shell=False)
        args = parse(cmd, rest, oneshot=True)
    except ParserExit as e:
        return e.status
    except UsageError as e:
        name = " ".join(tokens[:1])
        result = errors.from_exception(ctx, None, e)
        emit(ctx, name, result)
        return EXIT_USAGE
    result = CommandResult()
    try:
        if cmd.takes_device:
            try:
                s = ctx.make_session(args.device)
            except Exception as e:
                result = errors.from_exception(ctx, cmd, e)
                finalize(ctx, cmd, result)
                emit(ctx, cmd.name, result)
                return result.code
            s.log.write("command", line=shlex.join(ctx.argv), command=cmd.name, args=args_json(args))
        result = call(ctx, cmd, args)
    finally:
        try:
            notes = ctx.close_session()
        except KeyboardInterrupt:
            notes = ["cleanup interrupted by Ctrl-C: some changes may not have been undone"]
            result.exit_code = EXIT_INTERRUPTED
        result.notes.extend(f"Cleanup: {n}" for n in notes)
    if result.interrupted:
        result.exit_code = EXIT_INTERRUPTED
    emit(ctx, cmd.name, result)
    return result.code


# ---------------------------------------------------------------------------------- shell lines
def run_line(ctx: CliContext, line: str) -> CommandResult | None:
    """Run one shell line (same commands and handlers as one-shot mode)."""
    try:
        tokens = shlex.split(line, comments=True)
    except ValueError as e:
        result = failure(codes.tool("usage-error"), f"cannot parse the line: {e}", exit_code=EXIT_USAGE, show_hint=False)
        emit(ctx, "", result)
        return result
    if not tokens:
        return None
    ctx.line = LineOptions()
    try:
        ns, rest = split_options(line_parser(), tokens)
        ctx.line = LineOptions.from_namespace(ns)
        cmd, rest = lookup(rest, in_shell=True)
        args = parse(cmd, rest, oneshot=False)
    except ParserExit as e:
        ctx.line = LineOptions()
        return CommandResult(ok=e.status == 0, exit_code=e.status)
    except UsageError as e:
        result = errors.from_exception(ctx, None, e)
        emit(ctx, tokens[0], result)
        ctx.line = LineOptions()
        return result
    try:
        before = ctx.session
        if before is not None:
            before.log.write("command", line=line, command=cmd.name, args=args_json(args))
        result = call(ctx, cmd, args)
        if ctx.session is not None and ctx.session is not before:
            # `connect` opened a new session (and log): record the line that did it
            ctx.session.log.write("command", line=line, command=cmd.name, args=args_json(args))
        emit(ctx, cmd.name, result)
    finally:
        ctx.line = LineOptions()
    return result
