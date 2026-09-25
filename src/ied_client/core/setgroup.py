"""Setting groups (RW-7): the SGCB select-edit-confirm sequence.

Editing a group: EditSG := n (the device copies group n into the SE values), write the SE values,
CnfEdit := true (the device stores them in group n), EditSG := 0 (release). Only the active group
is visible under FC=SG; snapshots therefore capture only the active group (VER-9).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ied_client import codes
from ied_client.codes import ErrorInfo
from ied_client.protocol.errors import NotConnectedError, ServiceError
from ied_client.protocol.types import AccessError, VarSpec, format_value, value_to_json
from ied_client.protocol.values import parse_text

from .model import ControlBlockInfo
from .readwrite import values_equal
from .refs import ObjectRef, RefError
from .safety import PolicyError
from .session import CleanupAction, Session

SGCB_FIELDS = ("NumOfSG", "ActSG", "EditSG", "CnfEdit", "LActTm", "ResvTms")


@dataclass(slots=True)
class SgcbState:
    cb: ControlBlockInfo
    values: dict[str, Any] = field(default_factory=dict)
    error: ErrorInfo | None = None

    @property
    def ld(self) -> str:
        return self.cb.ld

    def to_json(self) -> dict:
        return {
            "reference": f"{self.cb.ld}/{self.cb.ln}.SGCB",
            "values": {k: value_to_json(v) for k, v in self.values.items()},
            "error": self.error.to_json() if self.error else None,
        }


def sgcbs(session: Session) -> list[ControlBlockInfo]:
    return session.model().control_blocks({"SGCB"})


def _sgcb_for(session: Session, ld: str | None) -> ControlBlockInfo:
    cbs = sgcbs(session)
    if not cbs:
        raise PolicyError(codes.tool("no-setting-groups"), f"{session.device_name} has no setting group control block (SGCB)")
    if ld is None:
        if len(cbs) > 1:
            raise PolicyError(
                codes.tool("ambiguous-sgcb"),
                "several logical devices have an SGCB; name one: " + ", ".join(cb.ld for cb in cbs),
            )
        return cbs[0]
    for cb in cbs:
        if cb.ld == ld:
            return cb
    raise PolicyError(codes.tool("no-setting-groups"), f"logical device {ld} has no SGCB")


def show(session: Session, ld: str | None = None) -> list[SgcbState]:
    client = session.require_client()
    out = []
    for cb in sgcbs(session):
        if ld and cb.ld != ld:
            continue
        st = SgcbState(cb)
        try:
            val = client.read(ObjectRef(cb.ld, cb.ln, ("SGCB",), "SP"), cb.spec)
            if isinstance(val, dict):
                st.values = val
            elif isinstance(val, AccessError):
                st.error = val.error
        except ServiceError as e:
            st.error = e.error
        out.append(st)
    return out


def _field_spec(cb: ControlBlockInfo, name: str) -> VarSpec:
    s = cb.spec.child(name)
    if s is None:
        raise PolicyError(codes.tool("sgcb-attribute-missing"), f"the SGCB of {cb.ld} has no {name}")
    return s


def _write_field(session: Session, cb: ControlBlockInfo, name: str, value: Any) -> None:
    client = session.require_client()
    client.write(ObjectRef(cb.ld, cb.ln, ("SGCB", name), "SP"), _field_spec(cb, name), value)


def activate(
    session: Session, group: int, ld: str | None = None, *, confirm: bool = True, log_extra: dict[str, Any] | None = None
) -> SgcbState:
    """Change the active setting group (writes ActSG; logged, restorable)."""
    cb = _sgcb_for(session, ld)
    before = show(session, cb.ld)[0]
    num = before.values.get("NumOfSG")
    if isinstance(num, int) and not 1 <= group <= num:
        raise PolicyError(codes.tool("invalid-setting-group"), f"group must be 1..{num}")
    if confirm:
        session.policy.confirm_write(
            session.ui,
            f"Activate setting group {group} on {session.device_name} ({cb.ld}); currently active: {before.values.get('ActSG')}\n"
            "Protection functions switch to the settings of that group immediately.",
        )
    ref = f"{cb.ld}/{cb.ln}.SGCB.ActSG"
    try:
        _write_field(session, cb, "ActSG", group)
        ok, err = True, None
    except ServiceError as e:
        ok, err = False, e.error
        session.remember_error(e.error, {"service": "setgroup"}, str(e))
    after = show(session, cb.ld)[0]
    session.log.write(
        "write",
        ref=ref,
        fc="SP",
        write_kind="sgcb-actsg",
        before=before.values.get("ActSG"),
        requested=group,
        after=after.values.get("ActSG"),
        ok=ok,
        error=err,
        **(log_extra or {}),
    )
    if not ok:
        raise ServiceError("setgroup", ref, err)  # type: ignore[arg-type]
    return after


@dataclass(slots=True)
class EditResult:
    ld: str
    group: int
    items: list[dict[str, Any]] = field(default_factory=list)
    confirmed: bool = False
    verified: bool | None = None
    error: ErrorInfo | None = None
    message: str = ""

    def to_json(self) -> dict:
        return {
            "ld": self.ld,
            "group": self.group,
            "items": self.items,
            "confirmed": self.confirmed,
            "verified": self.verified,
            "error": self.error.to_json() if self.error else None,
            "message": self.message,
        }


def edit(
    session: Session,
    group: int,
    assignments: list[tuple[str, str]],
    *,
    ld: str | None = None,
    confirm: bool = True,
    log_extra: dict[str, Any] | None = None,
) -> EditResult:
    """Select group ``group`` for editing, write the SE values, confirm (CnfEdit), release."""
    client = session.require_client()
    model = session.model()
    if not assignments:
        raise PolicyError(codes.tool("nothing-to-edit"), "give at least one <reference> <value> pair")
    parsed: list[tuple[ObjectRef, VarSpec, Any]] = []
    for text, value_text in assignments:
        ref = session.parse_ref(text).with_fc("SE")
        try:
            ref, spec = model.resolve_fc(ref)
        except RefError as e:
            raise PolicyError(codes.tool("not-a-setting"), f"{text}: no SE (editable setting) attribute: {e}") from e
        parsed.append((ref, spec, parse_text(spec, value_text, str(ref))))
    lds = {r.ld for r, _, _ in parsed}
    if len(lds) != 1 or (ld and ld not in lds):
        raise PolicyError(codes.tool("ambiguous-sgcb"), "all settings of one edit must be in the same logical device")
    cb = _sgcb_for(session, lds.pop())
    state = show(session, cb.ld)[0]
    num = state.values.get("NumOfSG")
    if isinstance(num, int) and not 1 <= group <= num:
        raise PolicyError(codes.tool("invalid-setting-group"), f"group must be 1..{num}")
    res = EditResult(cb.ld, group)
    key = f"sgcb:{cb.ld}:edit"
    session.register_cleanup(
        CleanupAction(
            key,
            f"edit session of setting group {group} in {cb.ld} (discarded)",
            lambda: _write_field(session, cb, "EditSG", 0),
            "the device ends an edit session when the association ends or its SGCB ResvTms expires; "
            "unconfirmed edits are discarded",
        )
    )
    try:
        _write_field(session, cb, "EditSG", group)
        befores = [client.read(r, s) for r, s, _ in parsed]
        if confirm:
            lines = [f"Edit setting group {group} of {cb.ld} on {session.device_name}:"]
            for (r, _s, v), b in zip(parsed, befores, strict=True):
                lines.append(f"  {r.iec()}: {format_value(b)} -> {format_value(v)}")
            session.policy.confirm_write(session.ui, "\n".join(lines))
        for (r, s, v), b in zip(parsed, befores, strict=True):
            item = {"reference": r.iec(), "before": value_to_json(b), "requested": value_to_json(v), "ok": False}
            try:
                client.write(r, s, v)
                item["ok"] = True
            except ServiceError as e:
                item["error"] = e.error.to_json()
                res.error = e.error
            res.items.append(item)
        if res.error is not None:
            res.message = "a value was refused; the edit is discarded (not confirmed)"
            _write_field(session, cb, "EditSG", 0)
            session.drop_cleanup(key)
            return _log_edit(session, res, log_extra)
        _write_field(session, cb, "CnfEdit", True)
        res.confirmed = True
        # verify: re-select the group and read the SE values back
        _write_field(session, cb, "EditSG", group)
        for item, (r, s, v) in zip(res.items, parsed, strict=True):
            after = client.read(r, s)
            item["after"] = value_to_json(after)
            item["applied"] = values_equal(after, v)
        res.verified = all(i.get("applied") for i in res.items)
        _write_field(session, cb, "EditSG", 0)
        session.drop_cleanup(key)
        if not res.verified:
            res.error = codes.tool("write-readback-mismatch")
            res.message = "the device confirmed the edit but a value reads back differently"
    except ServiceError as e:
        res.error = e.error
        res.message = str(e)
        session.remember_error(e.error, {"service": "setgroup", "fc": "SE"}, str(e))
        _end_edit(session, cb, key)
    except BaseException:
        # Declined confirmation, Ctrl-C, a tool error: end the edit session now. Left open, it would block
        # other clients from editing settings until this session ends (RW-7).
        _end_edit(session, cb, key)
        raise
    return _log_edit(session, res, log_extra)


def _end_edit(session: Session, cb: ControlBlockInfo, key: str) -> None:
    """Release the SGCB edit session (EditSG := 0). If that fails, the registered cleanup tries again at the
    end of the session and reports what lingers."""
    if not any(a.key == key for a in session.cleanups):
        return
    try:
        _write_field(session, cb, "EditSG", 0)
    except (ServiceError, NotConnectedError):
        return
    session.drop_cleanup(key)


def _log_edit(session: Session, res: EditResult, log_extra: dict[str, Any] | None = None) -> EditResult:
    for item in res.items:
        session.log.write(
            "write",
            ref=item["reference"],
            fc="SE",
            write_kind="setgroup",
            setting_group=res.group,
            before=item.get("before"),
            requested=item.get("requested"),
            after=item.get("after"),
            ok=bool(item.get("ok")) and res.confirmed,
            applied=item.get("applied"),
            error=item.get("error"),
            **(log_extra or {}),
        )
    return res
