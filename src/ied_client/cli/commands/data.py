"""read, write, watch, setgroup (RW-1 … RW-8)."""

from __future__ import annotations

from typing import Any

from ied_client.core import readwrite, setgroup
from ied_client.core.readwrite import ReadResult, describe_quality
from ied_client.protocol.types import value_from_json

from ..context import CliContext
from ..registry import UsageError, command
from ..render import (
    STYLE_NOTE,
    STYLE_REF,
    STYLE_WARN,
    Output,
    clock,
    flatten,
    fmt,
    quality_text,
    ref_label,
    value_text,
)
from ..result import CommandResult


def _ref_args(p, oneshot: bool) -> None:
    p.add_argument("ref", help="object reference: LD/LN.DO.DA, relative to the current location, or with [FC]")
    p.add_argument("--fc", help="functional constraint, when the reference exists under several")


def _read_rows(res: ReadResult) -> list[tuple[str, str]]:
    """Components of a structured value as (relative path, text); quality decoded, time with flags."""
    rows = []
    for path, v in flatten(res.value):
        name = ".".join(path)
        if path and path[-1] == "q" and hasattr(v, "as_int_lsb0"):
            rows.append((name, quality_text(describe_quality(v))))
        else:
            rows.append((name, value_text((*res.ref.path, *path), v)))
    return rows


def render_read(out: Output, res: ReadResult, data: dict[str, Any], protocol_label: str) -> None:
    out.parts((ref_label(res.ref.iec(), res.ref.fc), STYLE_REF), (f"   {protocol_label} {res.native[1]}", STYLE_NOTE))
    if res.error is not None:
        return  # the failure block shows the code
    rows: list[tuple[str, Any]]
    if isinstance(res.value, dict):
        rows = [("type", data["type"]), ("FC", res.ref.fc), *_read_rows(res)]
    else:
        rows = [("value", value_text(res.ref.path, res.value)), ("type", data["type"]), ("FC", res.ref.fc)]
    if not isinstance(res.value, dict):  # a structure shows its q and t among the components
        if data.get("quality"):
            rows.append(("quality", quality_text(data["quality"])))
        if res.timestamp is not None:
            rows.append(("timestamp", str(res.timestamp)))
    rows.append(("took", f"{res.duration_s * 1000:.1f} ms"))
    out.kv(rows)


@command(
    "read",
    "Read a value: shows value, type, FC, quality and timestamp (RW-1)",
    area="Data",
    configure=_ref_args,
    service="read",
    examples=("read PROT/PTOC1.Str.general", "read Pos.stVal", "read GGIO1.AnIn1 [MX]"),
)
def read(ctx: CliContext, args) -> CommandResult:
    s = ctx.require_session()
    res = readwrite.read(s, args.ref, args.fc)
    data = res.to_json()
    result = CommandResult(data=data, text=lambda out: render_read(out, res, data, s.protocol.display_name))
    if res.error is not None:
        result.ok = False
        result.error = res.error
        result.message = f"read {ref_label(res.ref.iec(), res.ref.fc)} failed: the device answered {res.error}"
        result.hint_context = {"service": "read", "fc": res.ref.fc}
    return result


# ---------------------------------------------------------------------------------- write
def _write_args(p, oneshot: bool) -> None:
    p.add_argument("ref", help="object reference of a basic attribute")
    p.add_argument("value", help="new value; its type is taken from the device's model (RW-3)")
    p.add_argument("--fc", help="functional constraint, when the reference exists under several")
    p.add_argument("--no-verify", dest="verify", action="store_false", help="do not read the value back afterwards")


@command(
    "write",
    "Write a value (type from the model; shows current and new value, asks to confirm, reads back)",
    area="Data",
    configure=_write_args,
    service="write",
    examples=("write CTRL/GGIO1.Setp1.setVal 12", "write Cfg1.dcText 'bay 12' --yes"),
)
def write(ctx: CliContext, args) -> CommandResult:
    s = ctx.require_session()
    res = readwrite.write(s, args.ref, args.value, fc=args.fc, verify=args.verify)
    data = res.to_json()
    label = ref_label(res.ref.iec(), res.ref.fc)

    def text(out: Output) -> None:
        rows = [
            ("reference", label),
            ("type", data["type"]),
            ("before", fmt(res.before)),
            ("requested", fmt(res.requested)),
        ]
        if res.ok and args.verify:
            rows.append(("read back", fmt(res.after) + ("  (applied)" if res.applied else "  (NOT applied)")))
        out.kv(rows)
        if res.ok and res.applied is not False:
            out.ok(f"Wrote {label}." + ("" if args.verify else " (not read back)"))

    result = CommandResult(data=data, text=text, warnings=list(res.warnings))
    if res.error is not None:
        result.ok = False
        result.error = res.error
        result.hint_context = {"service": "write", "fc": res.ref.fc}
        if res.ok:  # accepted but not applied (RW-5)
            result.message = f"the device accepted the write to {label} but reads back {fmt(res.after)} (RW-5)"
        else:
            result.message = f"the device refused the write to {label}: {res.error} (response reported as received, RW-6)"
    return result


# ---------------------------------------------------------------------------------- watch
def _watch_args(p, oneshot: bool) -> None:
    _ref_args(p, oneshot)
    p.add_argument("--interval", type=float, default=1.0, metavar="S", help="seconds between reads (default 1)")
    p.add_argument("--count", type=int, metavar="N", help="stop after N reads")
    p.add_argument("--duration", type=float, metavar="S", help="stop after S seconds")
    p.add_argument("--all", dest="all_samples", action="store_true", help="show every sample, not only changes")


@command(
    "watch",
    "Poll a value and show changes until Ctrl-C (RW-2)",
    area="Data",
    configure=_watch_args,
    service="watch",
)
def watch(ctx: CliContext, args) -> CommandResult:
    s = ctx.require_session()
    if args.interval <= 0:
        raise UsageError("--interval must be positive")
    events: list[dict[str, Any]] = []
    interrupted = False
    out = ctx.out
    first = True
    try:
        for ev in readwrite.watch(
            s, args.ref, fc=args.fc, interval_s=args.interval, count=args.count, duration_s=args.duration,
            only_changes=not args.all_samples,
        ):
            j = ev.to_json()
            events.append(j)
            if ctx.json:
                continue
            res = ev.result
            if first:
                out.parts((f"Watching {ref_label(res.ref.iec(), res.ref.fc)}", STYLE_REF),
                          (f" every {args.interval:g} s; Ctrl-C stops.", STYLE_NOTE))
                first = False
            if res.error is not None:
                out.line(f"{clock(ev.at)}  <{res.error}>", style=STYLE_WARN)
                continue
            line = f"{clock(ev.at)}  {value_text(res.ref.path, res.value, 120)}"
            if ev.changed and ev.previous is not None:
                line += f"   (was {fmt(ev.previous, 60)})"
            if res.quality is not None:
                q = describe_quality(res.quality)
                if q["validity"] != "good" or q["detail"] or q["test"]:
                    line += f"   q={quality_text(q)}"
            out.line(line)
    except KeyboardInterrupt:
        interrupted = True
    data = {"reference": args.ref, "interval_s": args.interval, "events": events, "changes": sum(1 for e in events if e["changed"])}
    r = CommandResult(data=data, interrupted=interrupted and not ctx.in_shell)
    if interrupted and ctx.in_shell:
        r.notes.append("Stopped.")
    return r


# ---------------------------------------------------------------------------------- setgroup
def _setgroup_args(p, oneshot: bool) -> None:
    sub = p.add_subparsers(dest="action", metavar="{show,activate,edit}")
    sh = sub.add_parser("show", help="setting group control blocks: NumOfSG, ActSG, EditSG, …")
    sh.add_argument("--ld", help="only this logical device")
    ac = sub.add_parser("activate", help="make setting group N the active one")
    ac.add_argument("group", type=int)
    ac.add_argument("--ld", help="logical device of the SGCB (when there are several)")
    ed = sub.add_parser("edit", help="edit values of group N: select, write SE values, confirm (RW-7)")
    ed.add_argument("group", type=int)
    ed.add_argument("pairs", nargs="+", metavar="REF VALUE", help="reference and new value, repeated")
    ed.add_argument("--ld", help="logical device of the SGCB (when there are several)")


def _sgcb_rows(states) -> list[tuple[Any, ...]]:
    rows = []
    for st in states:
        v = st.values
        err = str(st.error) if st.error else ""
        rows.append((f"{st.cb.ld}/{st.cb.ln}.SGCB", v.get("NumOfSG", ""), v.get("ActSG", ""), v.get("EditSG", ""),
                     fmt(v.get("CnfEdit")) if "CnfEdit" in v else "", fmt(v.get("LActTm")) if "LActTm" in v else "",
                     v.get("ResvTms", ""), err))
    return rows


SGCB_COLUMNS = ["SGCB", "NumOfSG", "ActSG", "EditSG", "CnfEdit", "LActTm", "ResvTms", "error"]


@command(
    "setgroup",
    "Setting groups: `show`, `activate <n>`, `edit <n> <ref> <value> …` (RW-7)",
    area="Data",
    configure=_setgroup_args,
    service="setgroup",
    examples=("setgroup show", "setgroup activate 2", "setgroup edit 2 PROT/PTOC1.StrVal.setMag.f 1.2"),
)
def setgroup_cmd(ctx: CliContext, args) -> CommandResult:
    s = ctx.require_session()
    action = args.action or "show"
    if action == "show":
        states = setgroup.show(s, getattr(args, "ld", None))
        return CommandResult(
            data={"sgcbs": [st.to_json() for st in states]},
            text=lambda out: out.table(SGCB_COLUMNS, _sgcb_rows(states), title="Setting group control blocks"),
        )
    if action == "activate":
        before = {st.cb.ld: st.values.get("ActSG") for st in setgroup.show(s, args.ld)}
        after = setgroup.activate(s, args.group, args.ld)
        data = {"before": before.get(after.cb.ld), "sgcb": after.to_json()}
        return CommandResult(
            data=data,
            text=lambda out: out.ok(
                f"Active setting group of {after.cb.ld}: {data['before']} -> {after.values.get('ActSG')}"
            ),
        )
    if len(args.pairs) % 2:
        raise UsageError("give reference/value pairs: setgroup edit <group> <ref> <value> [<ref> <value> …]")
    pairs = list(zip(args.pairs[::2], args.pairs[1::2], strict=True))
    res = setgroup.edit(s, args.group, pairs, ld=args.ld)
    data = res.to_json()

    def text(out: Output) -> None:
        rows = []
        for it in res.items:
            after = it.get("after")
            applied = it.get("applied")
            rows.append((it["reference"] + " [SE]", fmt(value_from_json(it.get("before"))), fmt(value_from_json(it.get("requested"))),
                         "" if after is None else fmt(value_from_json(after)),
                         "yes" if applied else ("no" if applied is False else "-"), it.get("error", {}).get("name", "") if it.get("error") else ""))
        out.table(["setting", "before", "requested", "read back", "applied", "error"], rows,
                  title=f"Setting group {res.group} of {res.ld}")
        if res.confirmed and res.verified:
            out.ok(f"Setting group {res.group} edited and confirmed.")

    result = CommandResult(data=data, text=text)
    if res.error is not None:
        result.ok = False
        result.error = res.error
        result.message = res.message or "setting group edit failed"
        result.hint_context = {"service": "setgroup", "fc": "SE"}
    return result
