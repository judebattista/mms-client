"""Undo the writes of a session (LOG-2). Controls are never replayed or reversed (CTL-10)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mms_client import codes
from mms_client.adapter import ServiceError, format_value, value_from_json
from mms_client.codes import ErrorInfo

from . import setgroup
from .readwrite import restore_value
from .safety import ConfirmationDeclined, PolicyError
from .session import Session
from .sessionlog import LoggedWrite, read_log, restorable_writes


@dataclass(slots=True)
class RestoreItem:
    write: LoggedWrite
    ok: bool = False
    skipped: str | None = None
    error: ErrorInfo | None = None

    def to_json(self) -> dict:
        return {
            "seq": self.write.seq,
            "reference": self.write.ref,
            "fc": self.write.fc,
            "kind": self.write.kind,
            "restore_to": self.write.before,
            "ok": self.ok,
            "skipped": self.skipped,
            "error": self.error.to_json() if self.error else None,
        }


@dataclass(slots=True)
class RestorePlan:
    items: list[RestoreItem] = field(default_factory=list)
    excluded_controls: int = 0

    def summary(self) -> str:
        lines = [f"Restore {len(self.items)} value(s), newest first:"]
        for it in self.items:
            w = it.write
            grp = f" (setting group {w.extra.get('setting_group')})" if w.kind == "setgroup" else ""
            lines.append(
                f"  {w.ref} [{w.fc}]{grp}: {format_value(value_from_json(w.after))} -> {format_value(value_from_json(w.before))}"
            )
        if self.excluded_controls:
            lines.append(f"  ({self.excluded_controls} control(s) in the log are not restored: controls are never replayed)")
        return "\n".join(lines)

    def to_json(self) -> dict:
        return {"items": [i.to_json() for i in self.items], "excluded_controls": self.excluded_controls}


def plan_restore(session: Session, log_file: Path | None = None) -> RestorePlan:
    entries: list[dict[str, Any]] = read_log(log_file) if log_file else session.log.entries
    device = next((e.get("device") for e in entries if e.get("kind") == "session-start"), None)
    if log_file and device and device != session.device_name:
        raise PolicyError(
            codes.tool("restore-device-mismatch"),
            f"that log belongs to {device}, not {session.device_name}",
        )
    plan = RestorePlan()
    plan.excluded_controls = sum(1 for e in entries if e.get("kind") == "control" and e.get("action") == "operate")
    for w in restorable_writes(entries):
        # Several writes to the same attribute are undone one by one (newest first), so the
        # oldest "before" is what remains.
        plan.items.append(RestoreItem(w))
    return plan


def run_restore(session: Session, plan: RestorePlan, *, confirm: bool = True) -> RestorePlan:
    if not plan.items:
        return plan
    if confirm:
        session.policy.confirm_write(session.ui, plan.summary())
    for it in plan.items:
        w = it.write
        try:
            if w.kind == "setgroup":
                group = int(w.extra.get("setting_group") or 0)
                res = setgroup.edit(session, group, [(w.ref, _text(w.before))], confirm=False)
                it.ok = res.confirmed and bool(res.verified)
                it.error = res.error
            elif w.kind == "sgcb-actsg":
                setgroup.activate(session, int(w.before), ld=w.ref.split("/")[0], confirm=False)
                it.ok = True
            else:
                res = restore_value(session, w.ref, w.fc, w.before)
                it.ok = res.ok and res.applied is not False
                it.error = res.error
        except (ServiceError, PolicyError, ConfirmationDeclined) as e:
            it.error = getattr(e, "error", None)
            it.skipped = str(e)
        session.log.write(
            "write" if it.ok else "note",
            ref=w.ref,
            fc=w.fc,
            restored_from=w.seq,
            ok=it.ok,
            before=w.after,
            after=w.before,
            error=it.error,
        )
    return plan


def _text(json_value: Any) -> str:
    v = value_from_json(json_value)
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)
