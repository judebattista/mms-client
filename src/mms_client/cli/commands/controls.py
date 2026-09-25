"""operate, select, cancel, authority-probe, origin (CTL-1 … CTL-10, ORG-1 … ORG-5)."""

from __future__ import annotations

from typing import Any

from rich.text import Text

from mms_client import codes
from mms_client.core import controls
from mms_client.core.controls import ControlPlan, describe_state, describe_value
from mms_client.core.safety import ConfirmationDeclined

from ..context import CliContext
from ..errors import declined_text
from ..registry import command
from ..render import STYLE_ERROR, STYLE_NOTE, STYLE_OK, STYLE_REF, STYLE_WARN, Output, fmt, ms
from ..result import EXIT_REFUSED, CommandResult, failure
from ._common import orcat_override, step_error

CONTROL_FLAGS = "--orcat N, --test, --no-interlock, --no-synchrocheck (expert) and --or-ident TEXT apply"


def _plan(ctx: CliContext, ref: str, value: str | None) -> ControlPlan:
    s = ctx.require_session()
    return controls.plan_control(
        s,
        ref,
        value,
        or_cat=orcat_override(ctx),
        test=bool(ctx.opt("test")),
        interlock_check=not ctx.opt("no_interlock"),
        synchro_check=not ctx.opt("no_synchrocheck"),
    )


def render_plan(out: Output, plan: ControlPlan, *, title: str = "Pre-flight check (nothing sent yet)") -> None:
    """CTL-7: object, CDC, ctlModel, current/target, origin, check flags, test flag, Loc/LocSta/
    LocKey and Mod/Beh; warnings highlighted before anything is sent."""
    rows: list[tuple[str, Any]] = [
        ("object", Text.assemble((plan.ref.iec(), STYLE_REF), (f"   (confirm by typing {plan.confirm_name})", STYLE_NOTE))),
        ("device", plan.target_device),
        ("CDC", f"{plan.cdc} ({plan.cdc_source})"),
        ("ctlModel", f"{plan.ctl_model} {plan.ctl_model_name}"),
        ("current", describe_state(plan.cdc, plan.current)),
        ("target", describe_value(plan.cdc, plan.target)),
        ("origin", f"orCat={plan.or_cat} ({codes.OR_CATS.get(plan.or_cat)}), orIdent={plan.or_ident!r}"),
        (
            "checks",
            Text(
                f"interlock {'on' if plan.interlock_check else 'OFF'}, synchrocheck {'on' if plan.synchro_check else 'OFF'}",
                style="" if plan.interlock_check and plan.synchro_check else STYLE_WARN,
            ),
        ),
        ("test flag", Text("SET", style=STYLE_WARN) if plan.test else "not set"),
    ]
    if plan.sbo_timeout_ms is not None:
        rows.append(("sboTimeout", f"{plan.sbo_timeout_ms} ms"))
    if plan.prior_select_age_s is not None:
        rows.append(("selection", f"made {plan.prior_select_age_s:.1f} s ago with `select`: operate only, no new select (CTL-5)"))
    for name, v in plan.authority.values.items():
        meaning = v.get("meaning")
        rows.append((name, f"{fmt(v.get('value'))}" + (f" ({meaning})" if meaning else "") + f"   ({v.get('ref')})"))
    out.kv(rows, title=title)
    for w in plan.warnings:
        out.warn(w)


def _step_rows(steps: list[Any]) -> list[tuple[Any, ...]]:
    rows = []
    for st in steps:
        status = Text("ok", style=STYLE_OK) if st.ok else Text("REFUSED", style=STYLE_ERROR)
        detail = []
        if st.add_cause is not None and st.add_cause.name not in ("none",) and not st.ok:
            detail.append(f"AddCause {st.add_cause}")
        if st.ied_error is not None and st.ied_error.code:
            detail.append(f"{st.ied_error}")
        if st.ctl_error is not None and st.ctl_error.code:
            detail.append(f"LastApplError {st.ctl_error}")
        rows.append((st.service, status, ms(st.duration_s), "; ".join(detail)))
    return rows


def _control_args(p, oneshot: bool, *, value_help: str) -> None:
    p.add_argument("ref", help="controllable data object, e.g. CTRL/CSWI1.Pos (Oper/stVal suffixes are accepted)")
    p.add_argument("value", help=value_help)
    p.add_argument("--termination-timeout", type=float, metavar="S", help="how long to wait for CommandTermination")


def _operate_args(p, oneshot: bool) -> None:
    _control_args(p, oneshot, value_help="target: true/false (SPC), on/off or close/open (DPC), integer (INC)")


def _hint_ctx(plan: ControlPlan, service: str) -> dict[str, Any]:
    return {"service": service, "cdc": plan.cdc, "ctl_model": plan.ctl_model_name,
            "tags": {"test": "yes" if plan.test else "no"}}


@command(
    "operate",
    "Operate a control (SPC/DPC/INC) with the sequence its ctlModel needs; pre-flight and typed confirmation",
    area="Controls",
    configure=_operate_args,
    service="operate",
    description="Operate a control object. The sequence follows the device's ctlModel (CTL-2). The pre-flight check "
    "is shown before the confirmation (CTL-7); in the strict safety profile the object name must be typed "
    f"(SAF-2), and --yes never confirms a control (SAF-3). {CONTROL_FLAGS}.",
    examples=("operate CTRL/CSWI1.Pos close --orcat station", "operate GGIO1.SPCSO1 true --test"),
)
def operate(ctx: CliContext, args) -> CommandResult:
    s = ctx.require_session()
    plan = _plan(ctx, args.ref, args.value)
    if not ctx.json:
        render_plan(ctx.out, plan)
    summary = f"Operate {plan.ref.iec()} -> {describe_value(plan.cdc, plan.target)} on {plan.target_device}"
    try:
        s.policy.confirm_dangerous(s.ui, summary, plan.confirm_name, "control")  # SAF-2 / SAF-3 (CTL-8)
    except ConfirmationDeclined as e:
        r = failure(
            codes.tool("non-interactive-no-prompt") if not s.ui.interactive else codes.tool("not-confirmed"),
            declined_text(e),
            exit_code=EXIT_REFUSED,
            data={"plan": plan.to_json(), "sent": False},
            show_hint=not s.ui.interactive,
        )
        return r
    outcome = controls.execute(s, plan, confirm=False, termination_timeout_s=args.termination_timeout)
    data = {**outcome.to_json(), "sent": True}

    def text(out: Output) -> None:
        out.table(["step", "result", "time", "detail"], _step_rows(outcome.steps), title="Sequence (CTL-2, CTL-3)")
        if outcome.used_earlier_select:
            out.note("Operated on the selection made earlier with `select` (no new select was sent).")
        if outcome.termination is not None:
            t = outcome.termination
            detail = "positive" if t.positive else f"NEGATIVE, AddCause {t.add_cause}"
            out.line(f"CommandTermination: {detail} after {ms(outcome.termination_wait_s)}")
        elif plan.ctl_model in (3, 4) and outcome.ok is False and outcome.termination_wait_s is not None:
            out.line(f"CommandTermination: none within {ms(outcome.termination_wait_s)}", style=STYLE_WARN)
        out.line(f"Final state: stVal = {describe_state(plan.cdc, outcome.final)}")
        if outcome.ok:
            out.ok(f"Operated {plan.ref.iec()}.")

    r = CommandResult(data=data, text=text)
    if not outcome.ok:
        r.ok = False
        r.error = outcome.error or codes.tool("control-refused")
        r.message = f"{plan.ref.iec()}: {outcome.message or 'control refused'} ({r.error})"
        r.hint_context = _hint_ctx(plan, "operate")
    return r


def _select_args(p, oneshot: bool) -> None:
    _control_args(p, oneshot, value_help="the value a later operate would send (SelectWithValue carries it)")


@command(
    "select",
    "Select an SBO control without operating it (tests SBO timeout and cancel handling, CTL-5)",
    area="Controls",
    configure=_select_args,
    service="select",
)
def select(ctx: CliContext, args) -> CommandResult:
    s = ctx.require_session()
    plan = _plan(ctx, args.ref, args.value)
    if not ctx.json:
        render_plan(ctx.out, plan, title="Pre-flight check for SELECT only (nothing sent yet)")
    try:
        s.policy.confirm_dangerous(s.ui, f"SELECT {plan.ref.iec()} (no operate) on {plan.target_device}",
                                   plan.confirm_name, "select")
    except ConfirmationDeclined as e:
        return failure(
            codes.tool("non-interactive-no-prompt") if not s.ui.interactive else codes.tool("not-confirmed"),
            declined_text(e),
            exit_code=EXIT_REFUSED,
            data={"plan": plan.to_json(), "sent": False},
            show_hint=not s.ui.interactive,
        )
    step = controls.select_only(s, plan, confirm=False)
    data = {"plan": plan.to_json(), "step": step.to_json(), "selected": step.ok}

    def text(out: Output) -> None:
        out.table(["step", "result", "time", "detail"], _step_rows([step]))
        if step.ok:
            to = f" within sboTimeout ({plan.sbo_timeout_ms} ms)" if plan.sbo_timeout_ms else ""
            out.ok(f"Selected {plan.ref.iec()}. Operate or `cancel` it{to}; afterwards the device deselects it by itself.")

    r = CommandResult(data=data, text=text)
    if not step.ok:
        r.ok = False
        r.error = step_error(step)
        r.message = f"select of {plan.ref.iec()} refused ({r.error})"
        r.hint_context = _hint_ctx(plan, "select")
    return r


def _cancel_args(p, oneshot: bool) -> None:
    p.add_argument("ref", help="controllable data object")


@command("cancel", "Cancel a selection (CTL-5)", area="Controls", configure=_cancel_args, service="cancel")
def cancel(ctx: CliContext, args) -> CommandResult:
    s = ctx.require_session()
    step = controls.cancel(s, args.ref, or_cat=orcat_override(ctx))
    data = {"reference": args.ref, "step": step.to_json()}
    r = CommandResult(data=data, text=lambda out: out.table(["step", "result", "time", "detail"], _step_rows([step])))
    if not step.ok:
        r.ok = False
        r.error = step_error(step)
        r.message = f"cancel refused ({r.error})"
        r.hint_context = {"service": "cancel"}
    return r


# ---------------------------------------------------------------------------------- authority probe
def _probe_args(p, oneshot: bool) -> None:
    p.add_argument("ref", nargs="?", help="one control object (default: every controllable object)")
    p.add_argument("--orcats", metavar="LIST", help="orCat values to try, e.g. 1,2,3 (default 1-3; all in expert mode)")


def _cell_text(cell: dict[str, Any]) -> Text:
    if cell["accepted"] is None:
        return Text(cell.get("note") or "n/a", style=STYLE_NOTE)
    if cell["accepted"]:
        return Text("accepted", style=STYLE_OK)
    ac = cell.get("add_cause") or {}
    ied = cell.get("ied_error") or {}
    why = ac.get("name") if ac and ac.get("name") not in ("unknown", "none") else ied.get("name", "")
    return Text(f"REJECTED {why}".strip(), style=STYLE_ERROR)


@command(
    "authority-probe",
    "Select + Cancel with each orCat on every SBO object; matrix of accepted/rejected (ORG-5). Nothing is operated.",
    area="Controls",
    configure=_probe_args,
    service="select",
)
def authority_probe(ctx: CliContext, args) -> CommandResult:
    s = ctx.require_session()
    cats = None
    if args.orcats:
        cats = [controls.parse_or_cat(x) for x in args.orcats.split(",") if x.strip()]
    rows = controls.authority_probe(s, args.ref, or_cats=cats)
    data = {"rows": [r.to_json() for r in rows]}
    used = sorted({c.or_cat for r in rows for c in r.cells})

    def text(out: Output) -> None:
        cols = ["object", "ctlModel"] + [f"{c} {codes.OR_CATS.get(c)}" for c in used] + ["Loc", "LocSta", "LocKey", "note"]
        table_rows = []
        for r in data["rows"]:
            cells = {c["orCat"]: c for c in r["cells"]}
            auth = r["authority"]

            def av(name: str, auth=auth) -> str:
                v = auth.get(name) or auth.get(f"LLN0.{name}")
                return "" if v is None else fmt(v.get("value"))

            table_rows.append(
                [r["reference"], r["ctl_model"] or "?"]
                + [_cell_text(cells[c]) if c in cells else Text("") for c in used]
                + [av("Loc"), av("LocSta"), av("LocKey"), r.get("note", "")]
            )
        out.table(cols, table_rows, title=f"Authority probe on {s.device_name} (Select + Cancel; nothing operated)")
        out.note("Direct-control objects have no select step and are listed as not probeable.")

    return CommandResult(data=data, text=text)


# ---------------------------------------------------------------------------------- origin
def _origin_args(p, oneshot: bool) -> None:
    p.add_argument("ref", help="control object, e.g. CTRL/CSWI1.Pos")


@command(
    "origin",
    "What the device reports as the origin of the last command it received (ORG-4)",
    area="Controls",
    configure=_origin_args,
    service="read",
)
def origin(ctx: CliContext, args) -> CommandResult:
    s = ctx.require_session()
    d = controls.last_origin(s, args.ref)

    def text(out: Output) -> None:
        o = d.get("origin") if isinstance(d.get("origin"), dict) else {}
        oc = o.get("orCat") if o else None
        rows = [
            ("object", d["reference"]),
            ("orCat", "-" if oc is None else f"{oc} ({d.get('orCat_name')})"),
            ("orIdent", d.get("orIdent_text") if d.get("orIdent_text") is not None else fmt(o.get("orIdent") if o else None)),
            ("ctlNum", fmt(d.get("ctlNum"))),
            ("stVal", fmt(d.get("stVal"))),
            ("t", fmt(d.get("t"))),
        ]
        out.kv(rows, title="Origin of the last command (as reported by the device)")

    return CommandResult(data=d, text=text)
