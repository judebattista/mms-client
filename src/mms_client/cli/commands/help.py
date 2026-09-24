"""explain (EXP-2), help, exit/quit."""

from __future__ import annotations

from typing import Any

from mms_client import codes

from .. import errors, registry
from ..context import CliContext
from ..registry import Needs, UsageError, command
from ..render import STYLE_NOTE, Output
from ..result import CommandResult
from ._common import last_error_in, load_log, session_logs

GOOSE_NOTE = (
    "This tool speaks MMS only. It cannot see GOOSE or Sampled Values, so paths that use them "
    "(for example bay-level interlocking between devices) cannot be checked with it."
)


def _explain_args(p, oneshot: bool) -> None:
    p.add_argument("query", nargs="*", help="`last`, an error code (data-access:object-access-denied, add-cause:2, "
                   "IED_ERROR_ACCESS_DENIED), or a term (XCBR, DPC, ST, ctlModel, ResvTms, …)")


def _last_error(ctx: CliContext) -> dict[str, Any] | None:
    s = ctx.session
    if s is not None and s.last_error is not None:
        le = s.last_error
        return {"error": le.error.to_json(), "key": le.error.key, "context": le.context, "message": le.message,
                "source": "this session"}
    if ctx.in_shell:
        return None
    # one-shot: the newest error recorded in the recent session logs
    for path in session_logs(ctx)[:20]:
        try:
            e = last_error_in(load_log(path))
        except (OSError, ValueError):
            continue
        if e is None:
            continue
        err = e["error"]
        return {"error": err, "key": f"{err['domain']}:{err['name']}", "context": e.get("context") or {},
                "message": e.get("message", ""), "source": str(path), "ts": e.get("ts")}
    return None


def render_explanation(out: Output, exp: Any) -> None:
    try:
        from mms_client.explain.render import render_explanation as plain
    except Exception:  # pragma: no cover
        out.line(f"{exp.title}\n{exp.summary}")
        return
    text = plain(exp, width=max(60, min(100, out.console.width - 2)))
    lines = text.splitlines()
    if lines:
        out.line(lines[0], style="bold")
    for ln in lines[1:]:
        out.line(ln, style="bold" if ln in ("Summary", "Details", "Next checks") else None)


@command(
    "explain",
    "Explain the last error, an error code, or a term, with the next checks to try (EXP-2)",
    area="Help",
    needs=Needs.NOTHING,
    configure=_explain_args,
    examples=("explain last", "explain add-cause:blocked-by-switching-hierarchy", "explain XCBR", "explain ResvTms"),
)
def explain(ctx: CliContext, args) -> CommandResult:
    query = " ".join(args.query).strip()
    if not query:
        raise UsageError("what should be explained? e.g. `explain last`, `explain object-access-denied`, `explain XCBR`")
    cat = errors.catalogue()
    if cat is None:
        return CommandResult(ok=False, error=codes.tool("internal-error"), message="the explanation catalogue could not be loaded",
                             show_hint=False)
    if query.lower() == "last":
        le = _last_error(ctx)
        if le is None:
            return CommandResult(data={"last_error": None},
                                 text=lambda out: out.line("No error recorded yet" + (" in this session." if ctx.in_shell else ".")))
        hctx = errors.hint_context({**le["context"], "mode": le["context"].get("mode") or ctx.mode.value})
        exp = cat.explain(le["key"], hctx)
        data = {"last_error": le, "explanation": exp.to_json() if exp else None}

        def text_last(out: Output) -> None:
            e = le["error"]
            out.line(f"Last error: {le['message'] or le['key']}", style="bold")
            out.line(f"  code: {e['domain']}:{e['name']}" + (f" ({e['code']})" if e.get("code") is not None else ""), style=STYLE_NOTE)
            if le["context"]:
                out.line("  context: " + ", ".join(f"{k}={v}" for k, v in le["context"].items()), style=STYLE_NOTE)
            if le["source"] != "this session":
                out.line(f"  from: {le['source']}", style=STYLE_NOTE)
            out.line("")
            if exp is not None:
                render_explanation(out, exp)
            else:
                out.line("The catalogue has no entry for this code yet.")

        return CommandResult(data=data, text=text_last)
    exp = cat.explain(query)
    if exp is not None:
        return CommandResult(data={"query": query, "explanation": exp.to_json()}, text=lambda out: render_explanation(out, exp))
    matches = cat.search(query, 10)
    data = {"query": query, "explanation": None, "matches": [{"id": m.id, "title": m.title} for m in matches]}

    def text(out: Output) -> None:
        try:
            from mms_client.explain.render import render_search

            out.line(render_search(matches, query))
        except Exception:  # pragma: no cover
            for m in matches:
                out.line(f"  {m.id}  {m.title}")

    return CommandResult(data=data, text=text)


# ---------------------------------------------------------------------------------- help
def _help_args(p, oneshot: bool) -> None:
    p.add_argument("topic", nargs="*", help="a command")


SHELL_KEYS = [
    "Tab completes commands, options and object references (after /, . and [ for the FC).",
    "Ctrl-C stops the running command (watch, subscribe, …) but not the shell.",
    "Ctrl-D or `exit` leaves; RCBs the tool enabled are disabled first (RPT-7).",
    "Any line takes --json, --terse, --yes, --orcat N, --or-ident TEXT, --test (and --no-interlock/--no-synchrocheck in expert mode).",
]


@command("help", "List the commands, or show one command's arguments", area="Help", needs=Needs.NOTHING, configure=_help_args)
def help_cmd(ctx: CliContext, args) -> CommandResult:
    if args.topic:
        cmd, n = registry.resolve(args.topic)
        if cmd is None:
            raise UsageError(f"no command {' '.join(args.topic)!r} (`help` lists them)")
        text_help = cmd.parser(oneshot=not ctx.in_shell).format_help()
        data = {"command": cmd.name, "summary": cmd.summary, "area": cmd.area, "needs": cmd.needs.value,
                "shell_only": cmd.shell_only, "help": text_help}
        return CommandResult(data=data, text=lambda out: out.line(text_help.rstrip()))
    cmds = [c for c in registry.all_commands() if ctx.in_shell or not c.shell_only]
    data = {"commands": [{"name": c.name, "summary": c.summary, "area": c.area, "needs": c.needs.value, "shell_only": c.shell_only}
                         for c in cmds]}

    def text(out: Output) -> None:
        rows = [(c.area, c.name, c.summary) for c in cmds]
        out.table(["area", "command", "what it does"], rows, title="Commands" + (" (shell)" if ctx.in_shell else ""))
        if ctx.in_shell:
            for k in SHELL_KEYS:
                out.note(k)
        else:
            out.note("One-shot: mms-client [options] <command> <device> [args]. `mms-client --help` lists the options.")
        out.note("`help <command>` shows its arguments.")
        out.note("Standard and expert modes are a guardrail against mistakes, not access control.")
        out.note(GOOSE_NOTE)

    return CommandResult(data=data, text=text)


@command("exit", "Leave the shell (undoes the tool's RCB changes first)", area="Shell", needs=Needs.NOTHING,
         shell_only=True, aliases=("quit",))
def exit_cmd(ctx: CliContext, args) -> CommandResult:
    return CommandResult(data={"exit": True}, exit_shell=True)
