"""set, log, restore, export (§6.2 Session; MOD-1, ORG-2, ORG-3, CLI-8, LOG-1 … LOG-3)."""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path
from typing import Any

from ied_client import TOOL_NAME, codes
from ied_client.core import restore as core_restore
from ied_client.core.controls import check_or_cat_allowed, parse_or_cat
from ied_client.core.results import default_or_ident
from ied_client.core.safety import Mode, Policy

from ..context import CliContext
from ..registry import Needs, UsageError, command
from ..render import STYLE_EXPERT, STYLE_NOTE, Output, code_label, fmt_json_value
from ..result import CommandResult
from ._common import last_error_in, latest_log_for, load_log, session_logs

# ---------------------------------------------------------------------------------- set
SET_KEYS = ("mode", "orcat", "terse", "oridentity")
ONESHOT_EQUIVALENT = {"mode": "--expert", "orcat": "--orcat N", "terse": "--terse", "oridentity": "--or-ident TEXT"}


def _set_args(p, oneshot: bool) -> None:
    p.add_argument("key", nargs="?", choices=SET_KEYS, help="what to set (no argument: show the settings)")
    p.add_argument("value", nargs="*", help="mode: standard|expert; orcat: 1-3|bay|station|remote; terse: on|off; oridentity: text")


def _settings(ctx: CliContext) -> dict[str, Any]:
    s = ctx.session
    return {
        "mode": ctx.mode.value,
        "orcat": s.or_cat if s is not None else (ctx.options.orcat or None),
        "terse": ctx.terse,
        "oridentity": s.or_ident if s is not None else (ctx.options.or_ident or default_or_ident()),
        "safety": s.policy.safety.value if s is not None else None,
    }


@command(
    "set",
    "Session settings: `set mode standard|expert`, `set orcat <1-3|bay|station|remote>`, `set terse on|off`, `set oridentity <text>`",
    area="Session",
    needs=Needs.NOTHING,
    configure=_set_args,
)
def set_cmd(ctx: CliContext, args) -> CommandResult:
    if args.key is None:
        st = _settings(ctx)
        return CommandResult(data=st, text=lambda out: out.kv([(k, "-" if v is None else str(v)) for k, v in st.items()], title="Settings"))
    if not ctx.in_shell:
        eq = ONESHOT_EQUIVALENT[args.key]
        return CommandResult(
            data={"key": args.key, "applied": False, "equivalent_option": eq},
            text=lambda out: out.line(f"In one-shot mode settings last for one command: use {eq} with the command."),
        )
    value = " ".join(args.value).strip()
    if not value:
        raise UsageError(f"give a value: set {args.key} <value>")
    s = ctx.session
    notes: list[str] = []
    if args.key == "mode":
        if value not in ("standard", "expert"):
            raise UsageError("set mode standard|expert")
        mode = Mode(value)
        ctx.options.expert = mode is Mode.EXPERT
        if s is not None:
            s.policy.mode = mode
            if mode is Mode.STANDARD and s.or_cat is not None and s.or_cat not in (1, 2, 3):
                notes.append(f"orCat {s.or_cat} needs expert mode; it was cleared (the next control asks again)")
                s.or_cat = None
            s.log.write("note", setting="mode", value=value)

        def text(out: Output) -> None:
            if mode is Mode.EXPERT:
                out.parts(("[EXPERT] ", STYLE_EXPERT), "Expert mode is on: RCB takeover, interlock/synchrocheck off, orCat 0 and 4-8, "
                          "and writes to FCs that are not normally writable are now allowed.")
                out.note("Modes are a guardrail against mistakes, not access control (MOD-4).")
            else:
                out.line("Standard mode.")

        return CommandResult(data={"mode": value}, text=text, notes=notes)
    if args.key == "orcat":
        v = parse_or_cat(value)
        if s is not None:
            s.set_or_cat(v)
        else:
            check_or_cat_allowed(Policy(mode=ctx.mode), v)
            ctx.options.orcat = str(v)
        name = codes.OR_CATS.get(v)
        return CommandResult(data={"orcat": v, "name": name},
                             text=lambda out: out.line(f"orCat for the following controls: {v} ({name})."))
    if args.key == "terse":
        if value not in ("on", "off"):
            raise UsageError("set terse on|off")
        on = value == "on"
        ctx.options.terse = on
        if s is not None:
            s.terse = on
            s.log.write("note", setting="terse", value=on)
        return CommandResult(data={"terse": on}, text=lambda out: out.line("Hints off." if on else "Hints on."))
    # oridentity (ORG-3)
    ctx.options.or_ident = value
    if s is not None:
        s.or_ident = value
        s.log.write("note", setting="oridentity", value=value)
    return CommandResult(data={"oridentity": value}, text=lambda out: out.line(f"orIdent for the following controls: {value!r}"))


# ---------------------------------------------------------------------------------- log
def _log_args(p, oneshot: bool) -> None:
    p.add_argument("file", nargs="?", help="a session log to show (default: this session's, or the list of logs)")
    p.add_argument("-n", "--lines", type=int, default=20, help="entries to show (default 20)")


def entry_summary(e: dict[str, Any]) -> str:
    k = e.get("kind")
    if k == "command":
        return str(e.get("line", ""))
    if k == "write":
        s = f"{e.get('ref')} [{e.get('fc')}] {fmt_json_value(e.get('before'), 40)} -> {fmt_json_value(e.get('requested', e.get('after')), 40)}"
        return s + ("" if e.get("ok") else f"  FAILED {code_label(e.get('error'))}")
    if k == "restore":
        s = f"{e.get('ref')} [{e.get('fc')}] restored to {fmt_json_value(e.get('after'), 40)} (undoing #{e.get('restored_from')})"
        return s + ("" if e.get("ok") else f"  FAILED {code_label(e.get('error'))}")
    if k == "control":
        out = e.get("outcome") or {}
        ref = (out.get("plan") or {}).get("reference") or e.get("reference") or (e.get("plan") or {}).get("reference") or ""
        ok = out.get("ok") if out else (e.get("step") or {}).get("ok")
        return f"{e.get('action')} {ref} " + ("ok" if ok else "refused" if ok is False else "")
    if k == "error":
        return f"{code_label(e.get('error'))}  {e.get('message', '')}"
    if k == "request":
        return f"{e.get('service')} {e.get('target') or ''} " + ("ok" if e.get("ok") else f"FAILED {code_label(e.get('error'))}")
    if k == "rcb":
        return f"{e.get('action')} {e.get('rcb') or e.get('key') or ''}"
    if k == "command-result":
        return f"{e.get('command')}: " + ("ok" if e.get("ok") else f"failed ({code_label(e.get('error'))})")
    if k == "session-start":
        return f"{e.get('device')} mode={e.get('mode')} safety={e.get('safety')}"
    if k == "note":
        return ", ".join(f"{a}={b}" for a, b in e.items() if a not in ("seq", "ts", "kind"))
    return ", ".join(f"{a}" for a in e if a not in ("seq", "ts", "kind"))


def _entry_rows(entries: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
    return [(e.get("seq"), _dt.datetime.fromtimestamp(e.get("ts", 0)).strftime("%H:%M:%S"), e.get("kind"), entry_summary(e))
            for e in entries]


@command("log", "Show the session log's path and its recent entries, or list earlier logs (LOG-1)", area="Session",
         needs=Needs.NOTHING, configure=_log_args)
def log_cmd(ctx: CliContext, args) -> CommandResult:
    if args.file:
        entries = load_log(Path(args.file))
        path: str | None = args.file
    elif ctx.session is not None:
        entries = ctx.session.log.entries
        path = str(ctx.session.log.path) if ctx.session.log.path else None
    else:
        logs = session_logs(ctx)[: max(args.lines, 1)]
        items = [{"path": str(p), "size": p.stat().st_size,
                  "modified": _dt.datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds")} for p in logs]
        return CommandResult(
            data={"log_dir": str(ctx.log_dir), "logs": items},
            text=lambda out: (out.line(f"Session logs in {ctx.log_dir}:"),
                              out.table(["log", "size", "modified"], [(Path(i["path"]).name, i["size"], i["modified"]) for i in items])),
        )
    shown = entries[-args.lines:] if args.lines > 0 else entries
    data = {"path": path, "entries": len(entries), "recent": shown}

    def text(out: Output) -> None:
        out.line(f"Session log: {path or '(not written: --no-log)'}", style="bold")
        out.table(["#", "time", "kind", "what"], _entry_rows(shown), title=f"Last {len(shown)} of {len(entries)} entries")

    return CommandResult(data=data, text=text)


# ---------------------------------------------------------------------------------- restore
def _restore_args(p, oneshot: bool) -> None:
    g = p.add_mutually_exclusive_group()
    g.add_argument("--from", dest="from_log", metavar="LOG", help="undo the writes recorded in an earlier session log")
    g.add_argument("--last", action="store_true", help="use the newest earlier session log of this device")


@command(
    "restore",
    "Put back every value written in the session, newest first, after confirmation; controls are never replayed (LOG-2)",
    area="Session",
    configure=_restore_args,
    service="write",
)
def restore(ctx: CliContext, args) -> CommandResult:
    s = ctx.require_session()
    log_file: Path | None = None
    if args.from_log:
        log_file = Path(args.from_log)
    elif args.last:
        log_file = latest_log_for(ctx, s.device_name, exclude=s.log.path)
        if log_file is None:
            raise UsageError(f"no earlier session log of {s.device_name} in {ctx.log_dir}")
    elif not ctx.in_shell:
        raise UsageError("one-shot restore needs the log to undo: --from LOG or --last")
    plan = core_restore.plan_restore(s, log_file)
    source = str(log_file) if log_file else "this session"
    if not plan.items:
        return CommandResult(
            data={"source": source, **plan.to_json()},
            text=lambda out: (out.line(f"Nothing to restore: no successful writes in {source}."),
                              out.note("Controls are never replayed or reversed (CTL-10).") if plan.excluded_controls else None),
        )
    core_restore.run_restore(s, plan)  # confirmation through the core's policy (shows the plan)
    data = {"source": source, **plan.to_json()}
    failed = [i for i in plan.items if not i.ok]

    def text(out: Output) -> None:
        rows = [(i.write.ref, i.write.fc, fmt_json_value(i.write.before, 60), "ok" if i.ok else f"FAILED {i.skipped or code_label(i.error)}")
                for i in plan.items]
        out.table(["reference", "FC", "restored to", "result"], rows, title=f"Restore from {source}")
        if plan.excluded_controls:
            out.note(f"{plan.excluded_controls} control(s) were not replayed: controls are never restored (CTL-10).")

    r = CommandResult(data=data, text=text)
    if failed:
        r.ok = False
        r.error = failed[0].error or codes.tool("write-readback-mismatch")
        r.message = f"{len(failed)} of {len(plan.items)} value(s) could not be restored"
        r.hint_context = {"service": "write"}
    return r


# ---------------------------------------------------------------------------------- export
def _export_args(p, oneshot: bool) -> None:
    p.add_argument("--incident", metavar="FILE", required=True, help="write an incident file (JSON) for the failure record (LOG-3)")
    p.add_argument("--note", help="what was being done and what went wrong, in your words")
    p.add_argument("--log", metavar="LOG", help="take the log excerpt from this session log instead")
    p.add_argument("--overwrite", action="store_true", help="replace an existing file")


@command(
    "export",
    "Save an incident file: device, experiment, inventory, last diagnose/check results, log excerpt (LOG-3)",
    area="Session",
    needs=Needs.TARGET,
    configure=_export_args,
    examples=("export --incident incidents/2026-09-24-f12.json --note 'reports stop after 10 min'",),
)
def export(ctx: CliContext, args) -> CommandResult:
    from ied_client.core.incident import build_incident, write_incident

    s = ctx.session
    if s is None:
        raise UsageError(f"no device: `connect <device>` first, or run `{TOOL_NAME} export <device> --incident FILE`")
    path = Path(args.incident)
    if path.exists() and not args.overwrite:
        raise FileExistsError(f"{path} exists (use --overwrite)")
    # In one-shot mode this session has just started: take the excerpt from an earlier log.
    source: Path | None = Path(args.log) if args.log else None
    if source is None and not ctx.in_shell:
        source = latest_log_for(ctx, s.device_name, exclude=s.log.path)
    if source is None:
        write_incident(s, path, note=args.note)
        body = json.loads(path.read_text(encoding="utf-8"))["result"]
    else:
        doc = build_incident(s, note=args.note)
        body = doc["result"]
        entries = load_log(source)
        body["log_file"] = str(source)
        body["log_excerpt"] = entries[-300:]
        if body.get("last_error") is None:
            le = last_error_in(entries)
            if le is not None:
                body["last_error"] = {"error": le["error"], "context": le.get("context") or {}, "message": le.get("message", "")}
        results = {e["kind"]: e for e in entries if e.get("kind") in ("diagnose", "check")}
        if not body.get("results") and results:
            body["results"] = {k: v.get("report") or v for k, v in results.items()}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc, indent=1, sort_keys=True, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
        s.log.write("note", action="export-incident", path=str(path))
    data = {"file": str(path.resolve()), "log_entries": len(body.get("log_excerpt") or []), "log_file": body.get("log_file"),
            "results": sorted((body.get("results") or {}).keys())}

    def text(out: Output) -> None:
        out.ok(f"Incident file written: {data['file']}")
        out.line(f"  log excerpt: {data['log_entries']} entries from {data['log_file'] or 'this session'}", style=STYLE_NOTE)
        if data["results"]:
            out.line(f"  results included: {', '.join(data['results'])}", style=STYLE_NOTE)
        if body.get("last_error"):
            out.line(f"  last error: {code_label(body['last_error']['error'])}", style=STYLE_NOTE)

    return CommandResult(data=data, text=text)
