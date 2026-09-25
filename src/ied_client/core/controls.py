"""Control services for SPC, DPC and INC (CTL-1 … CTL-10) and origin handling (ORG-1 … ORG-5).

The sequence always follows the device's own ``ctlModel`` (CTL-2):

=========================  =================================================
status-only                refused
direct-with-normal         Operate
sbo-with-normal            Select (read SBO), Operate
direct-with-enhanced       Operate, wait for CommandTermination
sbo-with-enhanced          SelectWithValue, Operate, wait for CommandTermination
=========================  =================================================

Every control is logged (CTL-10); ``restore`` never replays or reverses them.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from ied_client import codes
from ied_client.codes import ErrorInfo
from ied_client.protocol.errors import EncodeError, ServiceError
from ied_client.protocol.types import (
    AccessError,
    BitString,
    CommandTermination,
    ControlStepResult,
    Value,
    ValueKind,
    VarSpec,
    format_value,
    value_to_json,
)
from ied_client.protocol.values import parse_text

from .refs import ObjectRef, RefError, ln_class_of
from .safety import Policy, PolicyError
from .session import Session

SUPPORTED_CDCS = ("SPC", "DPC", "INC")
DBPOS = {0: "intermediate-state", 1: "off", 2: "on", 3: "bad-state"}
BEH = {1: "on", 2: "blocked", 3: "test", 4: "test/blocked", 5: "off"}


def check_or_cat_allowed(policy: Policy, value: int) -> None:
    """ORG-2: 1–3 always; 0 and 4–8 only in expert mode."""
    if value not in codes.OR_CATS:
        raise PolicyError(codes.tool("invalid-orcat"), f"orCat must be 0..8, got {value}")
    if value not in (1, 2, 3):
        policy.require_expert(f"orCat {value} ({codes.OR_CATS[value]})")


def parse_or_cat(text: str) -> int:
    t = text.strip().lower()
    if t.isdigit():
        return int(t)
    if t in codes.OR_CAT_ALIASES:
        return codes.OR_CAT_ALIASES[t]
    for n, name in codes.OR_CATS.items():
        if t == name:
            return n
    raise PolicyError(codes.tool("invalid-orcat"), f"unknown orCat {text!r} (use 1-3 or bay/station/remote)")


def ensure_or_cat(session: Session, override: int | None) -> int:
    """ORG-1: no silent default. The first control of a session asks; --orcat overrides."""
    if override is not None:
        check_or_cat_allowed(session.policy, override)
        return override
    if session.or_cat is not None:
        return session.or_cat
    if not session.ui.interactive:
        raise PolicyError(
            codes.tool("orcat-required"),
            "no originator category chosen: pass --orcat 1|2|3 (bay/station/remote). "
            "The tool never picks one silently.",
        )
    options = [("1", "bay-control"), ("2", "station-control"), ("3", "remote-control")]
    if session.policy.expert:
        options = [(str(k), v) for k, v in codes.OR_CATS.items()]
    ans = session.ui.choose(
        "Which originator category (orCat) should commands in this session carry?\n"
        "A device may accept or refuse a command depending on it (switching hierarchy: Loc/LocSta).",
        options,
        default=None,
    )
    if ans is None:
        raise PolicyError(codes.tool("orcat-required"), "no originator category chosen; nothing sent")
    session.set_or_cat(int(ans))
    return int(ans)


# ------------------------------------------------------------------------------ pre-flight
@dataclass(slots=True)
class AuthorityState:
    """Switching-hierarchy and behaviour values relevant to a control (CTL-6, CTL-7)."""

    values: dict[str, Any] = field(default_factory=dict)  # "Loc" -> {"ref":..., "value":...}

    def get(self, name: str) -> Any:
        v = self.values.get(name)
        return None if v is None else v.get("value")

    def to_json(self) -> dict:
        return self.values


@dataclass(slots=True)
class ControlPlan:
    ref: ObjectRef  # the data object
    cdc: str
    cdc_source: str
    ctl_model: int
    ctl_val_spec: VarSpec
    current: Value
    target: Value
    or_cat: int
    or_ident: str
    interlock_check: bool
    synchro_check: bool
    test: bool
    authority: AuthorityState
    sbo_timeout_ms: int | None = None
    oper_timeout_ms: int | None = None
    warnings: list[str] = field(default_factory=list)
    prior_select_age_s: float | None = None  # made earlier with `select` (CTL-5): operate uses it, no new select

    @property
    def ctl_model_name(self) -> str:
        return codes.CTL_MODELS.get(self.ctl_model, f"unknown-{self.ctl_model}")

    @property
    def confirm_name(self) -> str:
        """What the operator types to confirm (SAF-2): LN.DO."""
        return f"{self.ref.ln}.{'.'.join(self.ref.path)}"

    def summary(self) -> str:
        lines = [
            f"Control {self.ref.iec()} on {self.target_device}",
            f"  CDC:        {self.cdc} ({self.cdc_source})",
            f"  ctlModel:   {self.ctl_model} {self.ctl_model_name}",
            f"  current:    {describe_state(self.cdc, self.current)}",
            f"  target:     {describe_value(self.cdc, self.target)}",
            f"  origin:     orCat={self.or_cat} ({codes.OR_CATS.get(self.or_cat)}), orIdent={self.or_ident!r}",
            f"  check:      interlock={'on' if self.interlock_check else 'OFF'}, synchrocheck={'on' if self.synchro_check else 'OFF'}",
            f"  test flag:  {'SET' if self.test else 'not set'}",
        ]
        if self.prior_select_age_s is not None:
            lines.append(f"  selection:  made {self.prior_select_age_s:.1f} s ago with `select`; operate only (no new select)")
        for name, v in self.authority.values.items():
            lines.append(f"  {name + ':':11} {format_value(v.get('value'))}  ({v.get('ref')})")
        for w in self.warnings:
            lines.append(f"  WARNING: {w}")
        return "\n".join(lines)

    target_device: str = ""

    def to_json(self) -> dict:
        return {
            "reference": self.ref.iec(),
            "cdc": self.cdc,
            "cdc_source": self.cdc_source,
            "ctl_model": {"value": self.ctl_model, "name": self.ctl_model_name},
            "current": value_to_json(self.current),
            "target": value_to_json(self.target),
            "origin": {"orCat": self.or_cat, "orCat_name": codes.OR_CATS.get(self.or_cat), "orIdent": self.or_ident},
            "interlock_check": self.interlock_check,
            "synchro_check": self.synchro_check,
            "test": self.test,
            "authority": self.authority.to_json(),
            "sbo_timeout_ms": self.sbo_timeout_ms,
            "oper_timeout_ms": self.oper_timeout_ms,
            "prior_select_age_s": round(self.prior_select_age_s, 3) if self.prior_select_age_s is not None else None,
            "warnings": self.warnings,
        }


def describe_value(cdc: str, v: Value) -> str:
    if cdc == "DPC" and isinstance(v, bool):
        return "on (close)" if v else "off (open)"
    return format_value(v)


def describe_state(cdc: str, v: Value) -> str:
    if isinstance(v, BitString) and v.size == 2:
        return f"{DBPOS[v.as_int_msb0()]} ({v})"
    return format_value(v)


def _control_ref(session: Session, text: str) -> ObjectRef:
    model = session.model()
    ref = session.parse_ref(text)
    if ref.ln is None or not ref.path:
        raise RefError(f"{text!r}: give a controllable data object, e.g. LD/CSWI1.Pos")
    # Accept LD/LN.DO.Oper or .stVal and step back to the DO.
    for i in range(len(ref.path), 0, -1):
        candidate = ObjectRef(ref.ld, ref.ln, ref.path[:i])
        node = model.resolve(candidate).node
        if node is not None and "Oper" in node.children and "CO" in node.children["Oper"].specs:
            return candidate
    raise RefError(f"{ref.iec()} is not controllable (no Oper structure with FC=CO)")


def infer_cdc(session: Session, ref: ObjectRef, ctl_val: VarSpec, st_val: VarSpec | None) -> tuple[str, str]:
    """CDC from the SCL reference if available, else from the types in the device's model (CTL-1)."""
    ref_model = getattr(session.reference, "cdc_of", None)
    if callable(ref_model):
        cdc = ref_model(ref)
        if cdc:
            return cdc, "SCL reference"
    k = ctl_val.kind
    if k is ValueKind.BOOLEAN and st_val is not None and st_val.kind is ValueKind.BIT_STRING and abs(st_val.size or 0) == 2:
        return "DPC", "inferred from types"
    if k is ValueKind.BOOLEAN:
        return "SPC", "inferred from types"
    if k is ValueKind.INTEGER and st_val is not None and st_val.kind is ValueKind.INTEGER:
        return "INC", f"inferred from types (INC and ENC look the same over {session.protocol.display_name})"
    if k is ValueKind.BIT_STRING:
        return "BSC", "inferred from types"
    if k in (ValueKind.STRUCTURE, ValueKind.FLOAT):
        return "APC", "inferred from types"
    if k is ValueKind.INTEGER:
        return "ISC", "inferred from types"
    return "unknown", "inferred from types"


def _read_opt(session: Session, ref: ObjectRef, fc: str) -> tuple[Any, VarSpec | None]:
    model = session.model()
    try:
        r = model.resolve(ref)
    except RefError:
        return None, None
    if r.node is None or fc not in r.node.specs:
        return None, None
    spec = r.node.specs[fc]
    try:
        v = session.require_client().read(ref.with_fc(fc), spec)
    except ServiceError:
        return None, spec
    return (None if isinstance(v, AccessError) else v), spec


def read_ctl_model(session: Session, ref: ObjectRef) -> int:
    """The object's ctlModel [CF]. A failed read raises the device's own error (CLI-7); a ctlModel that is
    missing from the model or has an unexpected type is the tool's finding (``tool:ctlmodel-unusable``)."""
    cm = ref.child("ctlModel")
    try:
        node = session.model().resolve(cm).node
    except RefError:
        node = None
    if node is None or "CF" not in node.specs:
        raise PolicyError(codes.tool("ctlmodel-unusable"),
                          f"{cm.iec()} [CF] is not in the device's model: the control sequence cannot be chosen (CTL-2)")
    target = session.protocol.names.data(cm.with_fc("CF"))
    v = session.require_client().read(cm.with_fc("CF"), node.specs["CF"])  # ServiceError propagates as is
    if isinstance(v, AccessError):
        raise ServiceError("read", target, v.error, "ctlModel could not be read")
    if not isinstance(v, int) or isinstance(v, bool):
        raise PolicyError(codes.tool("ctlmodel-unusable"),
                          f"{cm.iec()} [CF] reads as {format_value(v)!r}, not an integer: the control sequence cannot be chosen")
    return v


def read_authority(session: Session, ref: ObjectRef) -> AuthorityState:
    """Loc / LocSta / LocKey of the LN and LLN0, and Mod/Beh of the LN (CTL-6, CTL-7)."""
    st = AuthorityState()
    model = session.model()
    lns = [ref.ln, "LLN0"] if ref.ln != "LLN0" else ["LLN0"]
    for name in ("Loc", "LocSta", "LocKey"):
        for ln in lns:
            r = ObjectRef(ref.ld, ln, (name, "stVal"))
            if model.has(r):
                v, _ = _read_opt(session, r, "ST")
                st.values[name if ln == ref.ln else f"LLN0.{name}"] = {"ref": r.iec(), "value": v}
    for name in ("Mod", "Beh"):
        r = ObjectRef(ref.ld, ref.ln, (name, "stVal"))
        if model.has(r):
            v, _ = _read_opt(session, r, "ST")
            st.values[name] = {"ref": r.iec(), "value": v, "meaning": BEH.get(v) if isinstance(v, int) else None}
    return st


def blocking_warnings(plan: ControlPlan) -> list[str]:
    """CTL-7: warn before sending when the values indicate the command will be blocked."""
    out = []
    a = plan.authority
    loc = a.get("Loc") if a.get("Loc") is not None else a.get("LLN0.Loc")
    loc_sta = a.get("LocSta") if a.get("LocSta") is not None else a.get("LLN0.LocSta")
    if loc is True and plan.or_cat in (2, 3, 5, 6):
        out.append(
            f"Loc is true (local control): a {codes.OR_CATS[plan.or_cat]} command will likely be refused "
            "(blocked-by-switching-hierarchy)"
        )
    if loc_sta is True and plan.or_cat in (3, 6):
        out.append("LocSta is true (station level has authority): remote-control commands will likely be refused")
    beh = a.get("Beh")
    if beh == 5:
        out.append("Beh is off: the function is switched off and will not execute commands (blocked-by-mode)")
    elif beh == 2:
        out.append("Beh is blocked: outputs are blocked (the command may be accepted but not executed)")
    elif beh in (3, 4) and not plan.test:
        out.append(f"Beh is {BEH[beh]}: commands without the test flag will likely be refused (blocked-by-mode)")
    elif beh == 1 and plan.test:
        out.append("the test flag is set but Beh is on: the device will likely refuse or ignore the test command")
    if plan.cdc == "DPC" and isinstance(plan.current, BitString):
        pos = plan.current.as_int_msb0()
        if (pos == 2 and plan.target is True) or (pos == 1 and plan.target is False):
            out.append("the switch is already in the target position (the device may answer position-reached)")
    if plan.cdc == "SPC" and plan.current == plan.target:
        out.append("stVal already equals the target value")
    return out


def plan_control(
    session: Session,
    ref_text: str,
    value_text: str | None,
    *,
    value: Value = None,
    or_cat: int | None = None,
    test: bool = False,
    interlock_check: bool = True,
    synchro_check: bool = True,
    allow_status_only: bool = False,
) -> ControlPlan:
    """Pre-flight (CTL-7): read everything relevant and build the plan; nothing is sent."""
    model = session.model()
    ref = _control_ref(session, ref_text)
    node = model.resolve(ref).node
    assert node is not None
    ctl_val_spec = node.children["Oper"].specs["CO"].child("ctlVal")
    if ctl_val_spec is None:
        raise RefError(f"{ref.iec()}.Oper has no ctlVal")
    st_node = node.children.get("stVal")
    st_spec = st_node.specs.get("ST") if st_node else None
    cdc, cdc_source = infer_cdc(session, ref, ctl_val_spec, st_spec)
    if cdc not in SUPPORTED_CDCS:
        raise PolicyError(
            codes.tool("cdc-not-supported"),
            f"{ref.iec()} looks like a {cdc} ({cdc_source}); v1 supports SPC, DPC and INC only",
        )
    if not interlock_check or not synchro_check:
        session.policy.require_expert("turning interlock/synchrocheck checks off (CTL-9)")
    ctl_model_v = read_ctl_model(session, ref)
    if ctl_model_v == 0 and not allow_status_only:
        raise PolicyError(codes.tool("status-only-control"), f"{ref.iec()} has ctlModel status-only: it cannot be controlled")
    oc = ensure_or_cat(session, or_cat)
    if value is None:
        if value_text is None:
            raise EncodeError("no target value given")
        try:
            value = parse_text(ctl_val_spec, _normalise_control_text(cdc, value_text), f"{ref.iec()}.ctlVal")
        except EncodeError as e:
            raise PolicyError(codes.tool("write-type-unsupported"), str(e)) from e
    current, _ = _read_opt(session, ref.child("stVal"), "ST")
    sbo_timeout, _ = _read_opt(session, ref.child("sboTimeout"), "CF")
    oper_timeout, _ = _read_opt(session, ref.child("operTimeout"), "CF")
    plan = ControlPlan(
        ref=ref,
        cdc=cdc,
        cdc_source=cdc_source,
        ctl_model=ctl_model_v,
        ctl_val_spec=ctl_val_spec,
        current=current,
        target=value,
        or_cat=oc,
        or_ident=session.or_ident,
        interlock_check=interlock_check,
        synchro_check=synchro_check,
        test=test,
        authority=read_authority(session, ref),
        sbo_timeout_ms=sbo_timeout if isinstance(sbo_timeout, int) else None,
        oper_timeout_ms=oper_timeout if isinstance(oper_timeout, int) else None,
        target_device=session.device_name,
    )
    plan.warnings = blocking_warnings(plan)
    prior: SelectState | None = session.selected.get(ref.iec()) if ctl_model_v in (2, 4) else None
    if prior is not None:
        plan.prior_select_age_s = time.monotonic() - prior.selected_at
        remaining = prior.remaining_s()
        if remaining is not None and remaining <= 0:
            plan.warnings.append(
                f"the selection made {plan.prior_select_age_s:.1f} s ago has probably expired (sboTimeout "
                f"{plan.sbo_timeout_ms} ms): the device is expected to refuse the operate (object-not-selected)"
            )
        if ctl_model_v == 4 and not _same_value(prior.plan.target, plan.target):
            plan.warnings.append(
                f"the selection was made with ctlVal {describe_value(cdc, prior.plan.target)}, this operate sends "
                f"{describe_value(cdc, plan.target)}: the device will likely refuse it"
            )
    if not interlock_check or not synchro_check:
        plan.warnings.append("interlock and/or synchrocheck checks are OFF (expert mode; this is logged)")
    return plan


def _same_value(a: Value, b: Value) -> bool:
    return value_to_json(a) == value_to_json(b)


def _normalise_control_text(cdc: str, text: str) -> str:
    t = text.strip().lower()
    if cdc == "DPC":
        return {"close": "true", "on": "true", "open": "false", "off": "false"}.get(t, t)
    return text


# ------------------------------------------------------------------------------ execution
@dataclass(slots=True)
class ControlOutcome:
    plan: ControlPlan
    steps: list[ControlStepResult] = field(default_factory=list)
    termination: CommandTermination | None = None
    termination_wait_s: float | None = None
    final: Value = None
    ok: bool = False
    error: ErrorInfo | None = None
    message: str = ""
    used_earlier_select: bool = False  # CTL-5: operated on the selection made with `select`

    def to_json(self) -> dict:
        return {
            "plan": self.plan.to_json(),
            "used_earlier_select": self.used_earlier_select,
            "steps": [s.to_json() for s in self.steps],
            "termination": self.termination.to_json() if self.termination else None,
            "termination_wait_s": round(self.termination_wait_s, 6) if self.termination_wait_s is not None else None,
            "final_stVal": value_to_json(self.final),
            "final_state": describe_state(self.plan.cdc, self.final),
            "ok": self.ok,
            "error": self.error.to_json() if self.error else None,
            "message": self.message,
        }


def step_error(step: ControlStepResult) -> ErrorInfo:
    """The most specific code explaining a refused control step (AddCause if present, else the IED error)."""
    if step.add_cause is not None and step.add_cause.name not in ("unknown", "none"):
        return step.add_cause
    if step.ied_error.code != 0:
        return step.ied_error
    return step.add_cause or codes.tool("control-refused")


def _prepare(session: Session, plan: ControlPlan):
    client = session.require_client()
    try:
        ctl = client.control(plan.ref)
    except ServiceError as e:
        session.remember_error(e.error, {"service": "operate", "cdc": plan.cdc}, str(e))
        raise
    ctl.configure(
        or_ident=plan.or_ident,
        or_cat=plan.or_cat,
        test=plan.test,
        interlock_check=plan.interlock_check,
        synchro_check=plan.synchro_check,
    )
    return ctl


def _log_control(session: Session, action: str, outcome: ControlOutcome | None = None, **extra: Any) -> None:
    session.log.write("control", action=action, **({"outcome": outcome.to_json()} if outcome else {}), **extra)


def execute(session: Session, plan: ControlPlan, *, confirm: bool = True, termination_timeout_s: float | None = None) -> ControlOutcome:
    """Run the ctlModel's sequence (CTL-2), time each step (CTL-3), wait for CommandTermination
    for enhanced security, and read back stVal."""
    if confirm:
        session.policy.confirm_dangerous(session.ui, plan.summary(), plan.confirm_name, "control")
    out = ControlOutcome(plan)
    ctl = _prepare(session, plan)
    m = plan.ctl_model
    ctx = {
        "service": "operate",
        "cdc": plan.cdc,
        "ctl_model": plan.ctl_model_name,
        "mode": session.mode.value,
        "ln_class": ln_class_of(plan.ref.ln) if plan.ref.ln else None,
        "tags": {"or_cat": codes.OR_CATS.get(plan.or_cat, str(plan.or_cat)), "test": "yes" if plan.test else "no"},
    }
    # CTL-5: a selection made earlier with `select` is used, not repeated (a second select of a selected
    # object is refused by the device). It is consumed by this operate whatever the outcome.
    prior = session.selected.pop(plan.ref.iec(), None) if m in (2, 4) else None
    out.used_earlier_select = prior is not None
    try:
        if m == 2 and prior is None:
            s = ctl.select()
            out.steps.append(s)
            if not s.ok:
                # An empty SBO value carries no reason: report the tool's own code, not an invented AddCause.
                out.error = step_error(s)
                out.message = "select refused (the device returned an empty SBO value)"
                return _finish(session, out, ctx | {"service": "select"})
        elif m == 4 and prior is None:
            s = ctl.select_with_value(plan.ctl_val_spec, plan.target)
            out.steps.append(s)
            if not s.ok:
                out.error = step_error(s)
                out.message = "select-with-value refused"
                return _finish(session, out, ctx | {"service": "select"})
        s = ctl.operate(plan.ctl_val_spec, plan.target)
        out.steps.append(s)
        if not s.ok:
            out.error = step_error(s)
            out.message = "operate refused"
            return _finish(session, out, ctx)
        if m in (3, 4):
            timeout = termination_timeout_s or max(5.0, (plan.oper_timeout_ms or 0) / 1000 + 2.0)
            t0 = time.perf_counter()
            term = ctl.wait_termination(timeout)
            out.termination_wait_s = time.perf_counter() - t0
            out.termination = term
            if term is None:
                out.error = codes.tool("command-termination-timeout")
                out.message = f"no CommandTermination within {timeout:.1f} s"
                return _finish(session, out, ctx | {"service": "command-termination"})
            if not term.positive:
                out.error = term.add_cause if term.add_cause and term.add_cause.code else codes.tool("command-termination-negative")
                out.message = "CommandTermination negative: the device accepted the command but could not complete it"
                return _finish(session, out, ctx | {"service": "command-termination"})
        out.ok = True
        return _finish(session, out, ctx)
    except ServiceError as e:
        out.error = e.error
        out.message = str(e)
        return _finish(session, out, ctx)


def _finish(session: Session, out: ControlOutcome, ctx: dict) -> ControlOutcome:
    # read back stVal (give the process a moment to follow)
    deadline = time.monotonic() + (1.0 if out.ok else 0.0)
    while True:
        final, _ = _read_opt(session, out.plan.ref.child("stVal"), "ST") if session.connected else (None, None)
        out.final = final
        if not out.ok or time.monotonic() >= deadline or _reached(out.plan, final):
            break
        time.sleep(0.1)
    if out.error is not None:
        session.remember_error(out.error, ctx, out.message)
    _log_control(session, "operate", out)
    return out


def _reached(plan: ControlPlan, final: Value) -> bool:
    if plan.cdc == "DPC" and isinstance(final, BitString):
        return final.as_int_msb0() == (2 if plan.target else 1)
    return final == plan.target


# ------------------------------------------------------------------------------ select / cancel (CTL-5)
@dataclass(slots=True)
class SelectState:
    plan: ControlPlan
    selected_at: float
    step: ControlStepResult

    def remaining_s(self) -> float | None:
        if self.plan.sbo_timeout_ms is None:
            return None
        return self.plan.sbo_timeout_ms / 1000 - (time.monotonic() - self.selected_at)


def select_only(session: Session, plan: ControlPlan, *, confirm: bool = True) -> ControlStepResult:
    """Select without operating (to test SBO timeout and cancel handling)."""
    if plan.ctl_model not in (2, 4):
        raise PolicyError(
            codes.tool("not-probeable-direct-control"),
            f"{plan.ref.iec()} uses {plan.ctl_model_name}: there is no select step",
        )
    if confirm:
        session.policy.confirm_dangerous(session.ui, "SELECT only (no operate)\n" + plan.summary(), plan.confirm_name, "select")
    ctl = _prepare(session, plan)
    step = ctl.select() if plan.ctl_model == 2 else ctl.select_with_value(plan.ctl_val_spec, plan.target)
    if step.ok:
        session.selected[plan.ref.iec()] = SelectState(plan, time.monotonic(), step)
    else:
        session.remember_error(step_error(step), {"service": "select", "cdc": plan.cdc}, "select refused")
    _log_control(session, "select", None, plan=plan.to_json(), step=step.to_json())
    return step


def cancel(session: Session, ref_text: str, *, or_cat: int | None = None) -> ControlStepResult:
    model = session.model()
    ref = _control_ref(session, ref_text)
    state: SelectState | None = session.selected.get(ref.iec())
    if state is None:
        # Cancel without our own select is allowed (tests how the device answers).
        client = session.require_client()
        ctl = client.control(ref)
        oc = ensure_or_cat(session, or_cat)
        ctl.configure(or_ident=session.or_ident, or_cat=oc)
    else:
        ctl = session.require_client().control(ref)
    del model
    step = ctl.cancel()
    session.selected.pop(ref.iec(), None)
    if not step.ok:
        session.remember_error(step_error(step), {"service": "cancel"}, "cancel refused")
    _log_control(session, "cancel", None, reference=ref.iec(), step=step.to_json())
    return step


# ------------------------------------------------------------------------------ authority probe (ORG-5)
@dataclass(slots=True)
class ProbeCell:
    or_cat: int
    accepted: bool | None  # None = not probeable
    add_cause: ErrorInfo | None = None
    ied_error: ErrorInfo | None = None
    note: str = ""

    def to_json(self) -> dict:
        return {
            "orCat": self.or_cat,
            "orCat_name": codes.OR_CATS.get(self.or_cat),
            "accepted": self.accepted,
            "add_cause": self.add_cause.to_json() if self.add_cause else None,
            "ied_error": self.ied_error.to_json() if self.ied_error else None,
            "note": self.note,
        }


@dataclass(slots=True)
class ProbeRow:
    ref: ObjectRef
    ctl_model: int | None
    cells: list[ProbeCell] = field(default_factory=list)
    authority: AuthorityState = field(default_factory=AuthorityState)
    note: str = ""

    def to_json(self) -> dict:
        return {
            "reference": self.ref.iec(),
            "ctl_model": codes.CTL_MODELS.get(self.ctl_model) if self.ctl_model is not None else None,
            "cells": [c.to_json() for c in self.cells],
            "authority": self.authority.to_json(),
            "note": self.note,
        }


def authority_probe(
    session: Session, ref_text: str | None = None, *, or_cats: list[int] | None = None, confirm: bool = True
) -> list[ProbeRow]:
    """Select + Cancel with each orCat on every SBO object; nothing is operated (ORG-5)."""
    model = session.model()
    targets = [_control_ref(session, ref_text)] if ref_text else model.controllable_objects()
    cats = or_cats or ([1, 2, 3] if not session.policy.expert else list(codes.OR_CATS))
    for c in cats:
        check_or_cat_allowed(session.policy, c)
    if confirm:
        session.policy.confirm_dangerous(
            session.ui,
            f"authority-probe on {session.device_name}: Select + Cancel with orCat {cats} on {len(targets)} object(s).\n"
            "Nothing is operated, but each select briefly reserves the object.",
            "authority-probe",
            "authority probe",
        )
    rows: list[ProbeRow] = []
    client = session.require_client()
    for ref in targets:
        row = ProbeRow(ref, None, authority=read_authority(session, ref))
        rows.append(row)
        try:
            row.ctl_model = read_ctl_model(session, ref)
        except (ServiceError, PolicyError) as e:
            row.note = f"ctlModel unreadable ({e.error})"
            continue
        if row.ctl_model not in (2, 4):
            row.note = "not probeable (no select step: " + codes.CTL_MODELS.get(row.ctl_model, f"ctlModel {row.ctl_model}") + ")"
            row.cells = [ProbeCell(c, None, note="not probeable") for c in cats]
            continue
        node = model.resolve(ref).node
        assert node is not None
        ctl_val_spec = node.children["Oper"].specs["CO"].child("ctlVal")
        current, _ = _read_opt(session, ref.child("stVal"), "ST")
        probe_value = _harmless_value(ctl_val_spec, current)
        try:
            ctl = client.control(ref)
        except ServiceError as e:
            row.note = f"control object unavailable: {e.error}"
            continue
        for c in cats:
            ctl.configure(or_ident=session.or_ident, or_cat=c)
            if row.ctl_model == 2:
                step = ctl.select()
                note = session.protocol.sbo_normal_select_note
            else:
                step = ctl.select_with_value(ctl_val_spec, probe_value) if ctl_val_spec else None
                note = ""
            if step is None:
                row.cells.append(ProbeCell(c, None, note="no ctlVal"))
                continue
            cell = ProbeCell(c, step.ok, step.add_cause, None if step.ok else step.ied_error, note)
            row.cells.append(cell)
            if step.ok:
                cancelled = ctl.cancel()
                if not cancelled.ok:
                    # The object stays selected until its sboTimeout: every later select would be refused with
                    # object-already-selected, which says nothing about authority. Stop probing this object.
                    why = step_error(cancelled)
                    cell.note = "; ".join(x for x in (cell.note, f"cancel refused ({why})") if x)
                    row.note = (f"probing stopped after orCat {c}: the cancel was refused ({why}), so the object stays "
                                "selected until its sboTimeout")
                    row.cells.extend(ProbeCell(rest, None, note="not probed (cancel refused)") for rest in cats[cats.index(c) + 1 :])
                    break
            time.sleep(0.05)
    session.log.write("control", action="authority-probe", rows=[r.to_json() for r in rows])
    return rows


def _harmless_value(spec: VarSpec | None, current: Value) -> Value:
    """A ctlVal for SelectWithValue that matches the current state where possible."""
    if spec is None:
        return None
    if spec.kind is ValueKind.BOOLEAN:
        if isinstance(current, BitString) and current.size == 2:
            return current.as_int_msb0() == 2
        return bool(current) if isinstance(current, bool) else False
    if spec.kind is ValueKind.INTEGER:
        return current if isinstance(current, int) and not isinstance(current, bool) else 0
    return None


# ------------------------------------------------------------------------------ origin (ORG-4)
def last_origin(session: Session, ref_text: str) -> dict[str, Any]:
    """What the device reports as the origin of the last command it received (ORG-4)."""
    ref = _control_ref(session, ref_text)
    out: dict[str, Any] = {"reference": ref.iec()}
    for name in ("origin", "ctlNum", "stVal", "t"):
        v, _ = _read_opt(session, ref.child(name), "ST")
        out[name] = v
    origin = out.get("origin")
    if isinstance(origin, dict):
        oc = origin.get("orCat")
        out["orCat_name"] = codes.OR_CATS.get(oc) if isinstance(oc, int) else None
        oi = origin.get("orIdent")
        if isinstance(oi, bytes):
            try:
                out["orIdent_text"] = oi.decode("utf-8")
            except UnicodeDecodeError:
                out["orIdent_text"] = None
    return out


_step_error = step_error  # backwards-compatible alias
