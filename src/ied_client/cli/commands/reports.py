"""rcb, subscribe, gi (RPT-1 … RPT-8)."""

from __future__ import annotations

from typing import Any

from rich.text import Text

from ied_client import codes
from ied_client.core import reports
from ied_client.core.reports import NoFreeRcbError, RcbStatus, Subscription, free_reason

from ..context import CliContext
from ..registry import UsageError, command
from ..render import STYLE_NOTE, STYLE_OK, STYLE_REF, STYLE_WARN, Output, yes_no
from ..result import EXIT_CONNECT, EXIT_REFUSED, CommandResult, failure

STATE_STYLE = {"free": STYLE_OK, "enabled": STYLE_WARN, "reserved": STYLE_WARN, "owned": STYLE_WARN, "unreadable": "red"}


def _owner(st: dict[str, Any]) -> str:
    if st.get("owner_name"):
        return f"{st['owner_name']} ({st['owner_ip']})" if st.get("owner_ip") and st["owner_name"] != st["owner_ip"] else st["owner_name"]
    return st.get("owner_ip") or ""


def rcb_rows(items: list[dict[str, Any]]) -> list[list[Any]]:
    rows = []
    for st in items:
        v = st.get("values") or {}
        if st["type"] == "BR":
            resv = "ResvTms=" + ("n/a" if v.get("resv_tms") is None else str(v.get("resv_tms")))
            opt = st.get("opt_flds") or []
            if st.get("last_buf_ovfl") is not None:
                buf_ovfl = Text("TRUE (last report)", style=STYLE_WARN) if st["last_buf_ovfl"] else "false (last report)"
            else:
                buf_ovfl = "in reports" if "bufOvfl" in opt else "not reported"
        else:
            resv = "Resv=" + yes_no(v.get("resv"))
            buf_ovfl = ""
        conf = "" if v.get("conf_rev") is None else str(v.get("conf_rev"))
        if st.get("conf_rev_mismatch"):
            conf = Text(f"{conf} (expected {st.get('expected_conf_rev')})", style=STYLE_WARN)
        err = st.get("error")
        rows.append(
            [
                st["reference"],
                st["type"],
                Text(st["state"], style=STATE_STYLE.get(st["state"], "")),
                v.get("dataset") or "",
                yes_no(v.get("rpt_ena")),
                resv,
                _owner(st),
                ",".join(st.get("trg_ops") or []),
                "" if v.get("intg_pd") is None else str(v.get("intg_pd")),
                conf,
                buf_ovfl,
                ", ".join(st.get("assigned_to") or []),
                f"{err['domain']}:{err['name']}" if err else "",
            ]
        )
    return rows


RCB_COLUMNS = ["RCB", "type", "state", "DatSet", "RptEna", "Resv/ResvTms", "Owner", "TrgOps", "IntgPd", "ConfRev",
               "BufOvfl", "assigned to", "error"]


def _rcb_args(p, oneshot: bool) -> None:
    p.add_argument("ln", nargs="?", help="only RCBs of this logical node or logical device (e.g. CTRL/LLN0)")


@command(
    "rcb",
    "List report control blocks: type, dataset, state, reservation, owner, TrgOps, IntgPd, ConfRev (RPT-1)",
    area="Reports",
    configure=_rcb_args,
    service="get-rcb",
)
def rcb(ctx: CliContext, args) -> CommandResult:
    s = ctx.require_session()
    items = [st.to_json() for st in reports.list_rcbs(s, args.ln)]
    mismatches = [st for st in items if st.get("conf_rev_mismatch")]

    def text(out: Output) -> None:
        out.table(RCB_COLUMNS, rcb_rows(items), title=f"Report control blocks on {s.device_name}")
        out.note("Owner is resolved to an inventory name where possible; `this tool` is this association.")

    r = CommandResult(data={"rcbs": items}, text=text)
    for st in mismatches:
        r.warnings.append(f"{st['reference']}: ConfRev {st['values']['conf_rev']} differs from the reference ({st['expected_conf_rev']}) (RPT-6)")
    return r


def instances_text(out: Output, instances: list[RcbStatus]) -> None:
    """RPT-3: every instance with its owner or reservation state."""
    out.table(
        ["instance", "state", "why not free"],
        [(st.cb.reference, Text(st.state, style=STATE_STYLE.get(st.state, "")), free_reason(st, me=None) or "free")
         for st in instances],
        title="Instances",
    )


# ---------------------------------------------------------------------------------- subscribe
def _sub_args(p, oneshot: bool) -> None:
    p.add_argument("rcb", nargs="?", help="RCB reference, name, or base name of indexed instances (urcbEvents)")
    p.add_argument("--takeover", action="store_true", help="take over an RCB another client uses (expert, typed confirmation; RPT-4)")
    p.add_argument("--trgops", metavar="LIST", help="trigger options, e.g. dchg,qchg,gi (restored on stop)")
    p.add_argument("--intgpd", type=int, metavar="MS", help="integrity period (restored on stop)")
    p.add_argument("--buftm", type=int, metavar="MS", help="buffer time (restored on stop)")
    p.add_argument("--no-gi", dest="gi", action="store_false", help="no general interrogation after enabling")
    p.add_argument("--purge-buf", action="store_true", help="purge the BRCB buffer first")
    p.add_argument("--count", type=int, metavar="N", help="stop after N reports")
    p.add_argument("--duration", type=float, metavar="S", help="stop after S seconds")
    if not oneshot:
        p.add_argument("--keep", action="store_true", help="keep the RCB enabled after the display stops (for `gi`); "
                       "it is disabled on `subscribe --stop`, `disconnect` or exit")
        p.add_argument("--stop", action="store_true", help="stop a kept subscription (all, or the one named)")


def _find_sub(ctx: CliContext, text: str | None) -> Subscription | None:
    s = ctx.session
    if s is None or not s.subscriptions:
        return None
    subs = list(s.subscriptions.values())
    if text is None:
        return subs[0] if len(subs) == 1 else None
    for sub in subs:
        cb = sub.cb
        native = s.protocol.names.control_block(cb.ld, cb.ln, cb.fc, cb.name)
        if text in (cb.reference, native, cb.name, f"{cb.ln}.{cb.name}") or cb.name.startswith(text):
            return sub
    return None


def report_lines(out: Output, view) -> None:
    lines = view.lines()
    head = lines[0]
    style = STYLE_WARN if (view.gap or view.report.buf_ovfl) else "bold"
    out.line(head, style=style)
    if view.gap:
        out.warn(f"SqNum gap: {view.gap} report(s) missing before this one (RPT-2)")
    for ln in lines[1:]:
        out.line(ln)


def show_reports(ctx: CliContext, sub: Subscription, *, count: int | None, duration: float | None) -> tuple[list[dict], bool]:
    """Print reports as they arrive until Ctrl-C, ``count`` or ``duration``. Returns (reports, interrupted)."""
    views: list[dict] = []
    interrupted = False
    try:
        for view in sub.reports(duration_s=duration, count=count):
            if len(views) < 10000:
                views.append(view.to_json())
            if not ctx.json:
                report_lines(ctx.out, view)
    except KeyboardInterrupt:
        interrupted = True
    return views, interrupted


@command(
    "subscribe",
    "Enable a free RCB and show its reports until Ctrl-C (RPT-2); cleanup on stop (RPT-7)",
    area="Reports",
    configure=_sub_args,
    service="subscribe",
    examples=("subscribe urcbEvents", "subscribe CTRL/LLN0.BR.brcbMeas01 --count 10", "subscribe brcbA --takeover --expert"),
)
def subscribe(ctx: CliContext, args) -> CommandResult:
    s = ctx.require_session()
    keep = bool(getattr(args, "keep", False))
    if getattr(args, "stop", False):
        subs = [sub for sub in list(s.subscriptions.values()) if args.rcb is None or _find_sub(ctx, args.rcb) is sub]
        notes: list[str] = []
        for sub in subs:
            notes += sub.stop()
        return CommandResult(
            data={"stopped": [sub.reference for sub in subs], "cleanup": notes},
            text=lambda out: out.line(f"Stopped {len(subs)} subscription(s)." if subs else "No active subscription."),
            notes=[f"Cleanup: {n}" for n in notes],
        )
    sub = _find_sub(ctx, args.rcb)
    resumed = sub is not None and args.rcb is not None
    if not resumed:
        if not args.rcb:
            raise UsageError("give the RCB: subscribe <rcb> (see `rcb` for the list)")
        try:
            sub = reports.subscribe(
                s,
                args.rcb,
                takeover=args.takeover,
                trg_ops=reports.parse_trg_ops(args.trgops) if args.trgops else None,
                intg_pd=args.intgpd,
                buf_tm=args.buftm,
                gi=args.gi,
                purge_buf=args.purge_buf,
            )
        except NoFreeRcbError as e:
            insts = e.instances
            return failure(
                e.error,
                f"no free instance of {args.rcb!r}: every instance is in use, reserved or assigned to another client (RPT-3)",
                exit_code=EXIT_REFUSED,
                data={"instances": [st.to_json() for st in insts]},
                text=lambda out: instances_text(out, insts),
            )
    assert sub is not None
    if not ctx.json:
        v = sub.initial
        out = ctx.out
        out.parts(("Subscribed to ", STYLE_OK), (sub.reference, STYLE_REF),
                  f" ({sub.cb.kind}, dataset {v.dataset}, RptID {v.rpt_id!r})" + (" (resumed)" if resumed else ""))
        if sub.takeover_from:
            out.warn(f"taken over from {sub.takeover_from}: its reports stop until it re-enables the RCB")
        if sub.changed_params:
            out.note(f"Changed parameters (restored on stop): {sub.changed_params.get('new')}")
        out.note("Reports appear as they arrive; Ctrl-C stops" + (" the display (the RCB stays enabled: --keep)." if keep else " and disables the RCB."))
    views, interrupted = show_reports(ctx, sub, count=args.count, duration=args.duration)
    lost = s.connection_lost.is_set()
    notes = [] if (keep and not lost) else sub.stop()
    data = {"subscription": sub.to_json(), "reports": views, "received": len(views), "sqnum_gaps": sub.gaps,
            "kept": keep and not lost, "cleanup": notes}

    def text(out: Output) -> None:
        out.line(f"{len(views)} report(s) shown, {sub.gaps} missing by SqNum." + (" The RCB stays enabled (`subscribe --stop`)." if data["kept"] else ""),
                 style=STYLE_NOTE)

    r = CommandResult(data=data, text=text, notes=[f"Cleanup: {n}" for n in notes])
    r.interrupted = interrupted and not ctx.in_shell
    if lost:
        r.ok = False
        r.error = codes.tool("connection-lost")
        r.message = f"the association with {s.device_name} was lost during the subscription"
        r.exit_code = EXIT_CONNECT
    return r


# ---------------------------------------------------------------------------------- gi
def _gi_args(p, oneshot: bool) -> None:
    p.add_argument("rcb", nargs="?", help="the RCB (default: the one kept with `subscribe --keep`)")
    p.add_argument("--wait", type=float, default=3.0, metavar="S", help="how long to show the reports that follow (default 3 s)")


@command(
    "gi",
    "General interrogation on the subscribed RCB and show the reports it causes (RPT-5)",
    area="Reports",
    configure=_gi_args,
    service="gi",
)
def gi(ctx: CliContext, args) -> CommandResult:
    s = ctx.require_session()
    sub = _find_sub(ctx, args.rcb)
    temporary = sub is None
    if sub is None:
        if not args.rcb:
            raise UsageError("no kept subscription: give the RCB (`gi <rcb>`), or `subscribe <rcb> --keep` first")
        try:
            sub = reports.subscribe(s, args.rcb, gi=False)
        except NoFreeRcbError as e:
            insts = e.instances
            return failure(e.error, f"no free instance of {args.rcb!r} to interrogate (RPT-3)", exit_code=EXIT_REFUSED,
                           data={"instances": [st.to_json() for st in insts]}, text=lambda out: instances_text(out, insts))
    try:
        sub.gi()
        if not ctx.json:
            ctx.out.parts(("GI sent to ", STYLE_OK), (sub.reference, STYLE_REF), f"; showing reports for {args.wait:g} s.")
        views, interrupted = show_reports(ctx, sub, count=None, duration=args.wait)
    finally:
        notes = sub.stop() if temporary else []
    data = {"rcb": sub.reference, "reports": views, "temporary_subscription": temporary, "cleanup": notes}
    r = CommandResult(data=data, text=lambda out: out.note(f"{len(views)} report(s)."), notes=[f"Cleanup: {n}" for n in notes])
    r.interrupted = interrupted and not ctx.in_shell
    return r
