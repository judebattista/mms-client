"""Read, write and watch data attributes (RW-1 … RW-6, RW-8)."""

from __future__ import annotations

import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from ied_client import codes
from ied_client.codes import ErrorInfo
from ied_client.protocol.errors import EncodeError, ServiceError
from ied_client.protocol.types import (
    AccessError,
    BitString,
    UtcTime,
    Value,
    VarSpec,
    format_value,
    value_from_json,
    value_to_json,
)
from ied_client.protocol.values import parse_text

from .refs import ObjectRef
from .safety import PolicyError
from .session import Session

# When a whole DO or a DA is read, these siblings are shown alongside when present (RW-1).
QUALITY_NAMES = ("q",)
TIME_NAMES = ("t",)
READ_FC_PREFERENCE = ("ST", "MX", "SP", "SG", "SE", "CF", "DC", "SV", "EX", "BL", "OR", "CO")


@dataclass(slots=True)
class ReadResult:
    ref: ObjectRef  # with FC
    spec: VarSpec
    value: Value
    quality: BitString | None = None
    timestamp: UtcTime | None = None
    error: ErrorInfo | None = None
    duration_s: float = 0.0
    native: tuple[str, str] = ("native", "")  # (JSON key, the protocol's name for the reference)

    @property
    def ok(self) -> bool:
        return self.error is None

    def to_json(self) -> dict:
        return {
            "reference": self.ref.iec(),
            "fc": self.ref.fc,
            self.native[0]: self.native[1],
            "type": self.spec.type_name(),
            "value": value_to_json(self.value),
            "quality": describe_quality(self.quality) if self.quality is not None else None,
            "timestamp": self.timestamp.iso() if self.timestamp else None,
            "timestamp_quality": self.timestamp.flags() if self.timestamp else None,
            "error": self.error.to_json() if self.error else None,
            "duration_s": round(self.duration_s, 6),
        }


def describe_quality(q: BitString) -> dict[str, Any]:
    """Decode an IEC 61850 Quality bit string (validity, detail, source, test, operatorBlocked)."""
    v = q.as_int_lsb0()
    validity = {0: "good", 1: "reserved", 2: "invalid", 3: "questionable"}[v & 3]
    details = [
        name
        for bit, name in (
            (4, "overflow"),
            (8, "outOfRange"),
            (16, "badReference"),
            (32, "oscillatory"),
            (64, "failure"),
            (128, "oldData"),
            (256, "inconsistent"),
            (512, "inaccurate"),
        )
        if v & bit
    ]
    return {
        "bits": str(q),
        "validity": validity,
        "detail": details,
        "source": "substituted" if v & 1024 else "process",
        "test": bool(v & 2048),
        "operatorBlocked": bool(v & 4096),
    }


def native_name(session: Session, ref: ObjectRef) -> tuple[str, str]:
    names = session.protocol.names
    return names.key, names.data(ref)


def resolve(session: Session, text: str | ObjectRef, fc: str | None = None) -> tuple[ObjectRef, VarSpec]:
    model = session.model()
    ref = text if isinstance(text, ObjectRef) else session.parse_ref(text, fc)
    if fc and ref.fc is None:
        ref = ref.with_fc(fc.upper())
    return model.resolve_fc(ref, prefer=READ_FC_PREFERENCE)


def read(session: Session, text: str | ObjectRef, fc: str | None = None, *, with_qt: bool = True) -> ReadResult:
    """RW-1: value, type, FC, and quality/timestamp where the model has them."""
    client = session.require_client()
    ref, spec = resolve(session, text, fc)
    t0 = time.perf_counter()
    try:
        value = client.read(ref, spec)
    except ServiceError as e:
        session.log.write("request", service="read", target=str(ref), ok=False, error=e.error)
        session.remember_error(e.error, {"service": "read", "fc": ref.fc}, str(e))
        return ReadResult(ref, spec, None, error=e.error, duration_s=time.perf_counter() - t0,
                          native=native_name(session, ref))
    dt = time.perf_counter() - t0
    res = ReadResult(ref, spec, value, duration_s=dt, native=native_name(session, ref))
    if isinstance(value, AccessError):
        res.error = value.error
        session.remember_error(value.error, {"service": "read", "fc": ref.fc}, f"read {ref}")
    elif isinstance(value, dict):
        q = next((value[n] for n in QUALITY_NAMES if isinstance(value.get(n), BitString)), None)
        t = next((value[n] for n in TIME_NAMES if isinstance(value.get(n), UtcTime)), None)
        res.quality, res.timestamp = q, t
    elif with_qt and len(ref.path) >= 2:
        _attach_siblings(session, res)
    session.log.write(
        "request",
        service="read",
        target=str(ref),
        ok=res.ok,
        value=value_to_json(value),
        error=res.error,
        duration_s=dt,
    )
    return res


def _attach_siblings(session: Session, res: ReadResult) -> None:
    """For a single DA like Pos.stVal, also fetch Pos.q and Pos.t in the same FC."""
    model = session.model()
    parent = res.ref.parent()
    if parent is None or parent.ln is None or res.ref.path[-1] in QUALITY_NAMES + TIME_NAMES:
        return
    node = model.resolve(parent).node
    if node is None:
        return
    items: list[ObjectRef] = []
    names: list[str] = []
    for n in QUALITY_NAMES + TIME_NAMES:
        child = node.children.get(n)
        if child is not None and res.ref.fc in child.specs:
            items.append(parent.child(n).with_fc(res.ref.fc))
            names.append(n)
    if not items:
        return
    try:
        vals = session.require_client().read_many(items)
    except ServiceError:
        return
    for n, v in zip(names, vals, strict=False):
        if n in QUALITY_NAMES and isinstance(v, BitString):
            res.quality = v
        if n in TIME_NAMES and isinstance(v, UtcTime):
            res.timestamp = v


# ---------------------------------------------------------------------------------- write
@dataclass(slots=True)
class WriteResult:
    ref: ObjectRef
    spec: VarSpec
    before: Value
    requested: Value
    after: Value = None
    ok: bool = False
    applied: bool | None = None  # read-back matched (RW-5)
    error: ErrorInfo | None = None
    warnings: list[str] = field(default_factory=list)
    duration_s: float = 0.0
    native: tuple[str, str] = ("native", "")  # (JSON key, the protocol's name for the reference)

    def to_json(self) -> dict:
        return {
            "reference": self.ref.iec(),
            "fc": self.ref.fc,
            self.native[0]: self.native[1],
            "type": self.spec.type_name(),
            "before": value_to_json(self.before),
            "requested": value_to_json(self.requested),
            "after": value_to_json(self.after),
            "ok": self.ok,
            "applied": self.applied,
            "error": self.error.to_json() if self.error else None,
            "warnings": self.warnings,
            "duration_s": round(self.duration_s, 6),
        }


def check_write_allowed(session: Session, ref: ObjectRef, spec: VarSpec) -> list[str]:
    """RW-6/7/8 guardrails. Returns warnings; raises PolicyError when refused."""
    fc = ref.fc or ""
    if fc == "CO":
        raise PolicyError(
            codes.tool("write-co-needs-operate"),
            f"{ref}: control attributes are not written directly; use `operate {ref.data_object.iec() if ref.data_object else ref.iec()}` "
            "(it runs the select/operate sequence the device's ctlModel requires)",
        )
    if fc == "SE":
        raise PolicyError(
            codes.tool("write-se-needs-setgroup"),
            f"{ref}: setting-group values are edited with `setgroup edit <group> {ref.iec()} <value>` "
            "(select-edit-confirm sequence of the SGCB)",
        )
    warnings = []
    if fc not in codes.NORMALLY_WRITABLE_FCS:
        session.policy.require_expert(f"writing [{fc}] attributes (not normally writable)")
        warnings.append(
            f"[{fc}] is not a normally writable functional constraint; this is a negative test and the "
            "device's response will be reported exactly as received"
        )
    if not spec.is_basic:
        raise PolicyError(
            codes.tool("write-type-unsupported"),
            f"{ref} is a {spec.type_name()}; write its components individually",
        )
    return warnings


def write(
    session: Session,
    text: str | ObjectRef,
    value_text: str | None = None,
    *,
    value: Value = None,
    fc: str | None = None,
    verify: bool = True,
    confirm: bool = True,
    log_extra: dict[str, Any] | None = None,
) -> WriteResult:
    """Write one basic attribute (RW-3 … RW-6): type from the device's model, show current and
    new value, confirm, write, read back. ``log_extra`` is added to the log entry (restore marks its
    own writes with ``restored_from`` so that they are never restored in turn, LOG-2)."""
    client = session.require_client()
    model = session.model()
    ref = text if isinstance(text, ObjectRef) else session.parse_ref(text, fc)
    if fc and ref.fc is None:
        ref = ref.with_fc(fc.upper())
    ref, spec = model.resolve_fc(ref, prefer=tuple(codes.NORMALLY_WRITABLE_FCS) + ("ST", "MX"))
    warnings = check_write_allowed(session, ref, spec)
    if value is None:
        if value_text is None:
            raise EncodeError("no value given")
        try:
            value = parse_text(spec, value_text, str(ref))
        except EncodeError as e:
            raise PolicyError(codes.tool("write-type-unsupported"), str(e)) from e
    before = client.read(ref, spec)
    res = WriteResult(ref, spec, before, value, warnings=warnings, native=native_name(session, ref))
    if confirm:
        summary = (
            f"Write {ref.iec()} [{ref.fc}] ({spec.type_name()}) on {session.device_name}\n"
            f"  current: {format_value(before)}\n  new:     {format_value(value)}"
        )
        for w in warnings:
            summary += f"\n  WARNING: {w}"
        session.policy.confirm_write(session.ui, summary)
    t0 = time.perf_counter()
    try:
        client.write(ref, spec, value)
        res.ok = True
    except ServiceError as e:
        res.error = e.error
        session.remember_error(e.error, {"service": "write", "fc": ref.fc, "mode": session.mode.value}, str(e))
    res.duration_s = time.perf_counter() - t0
    if res.ok and verify:
        after = client.read(ref, spec)
        res.after = after
        res.applied = values_equal(after, value)
        if not res.applied:
            res.error = codes.tool("write-readback-mismatch")
            session.remember_error(
                res.error,
                {"service": "write", "fc": ref.fc},
                f"{ref}: the device accepted the write but reads back {format_value(after)}",
            )
    session.log.write(
        "write",
        ref=ref.iec(),
        fc=ref.fc,
        type=spec.type_name(),
        before=value_to_json(before),
        requested=value_to_json(value),
        after=value_to_json(res.after),
        ok=res.ok,
        applied=res.applied,
        error=res.error,
        mode=session.mode.value,
        warnings=warnings,
        **(log_extra or {}),
    )
    return res


def values_equal(a: Value, b: Value) -> bool:
    if isinstance(a, float) or isinstance(b, float):
        try:
            fa, fb = float(a), float(b)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return False
        return abs(fa - fb) <= 1e-6 * max(1.0, abs(fa), abs(fb))
    return value_to_json(a) == value_to_json(b)


def restore_value(
    session: Session, ref_text: str, fc: str, before_json: Any, *, log_extra: dict[str, Any] | None = None
) -> WriteResult:
    """Write a previously logged value back (LOG-2); confirmation is handled by the caller."""
    ref = session.parse_ref(ref_text, fc, relative=False)
    return write(session, ref, value=value_from_json(before_json), confirm=False, log_extra=log_extra)


# ---------------------------------------------------------------------------------- watch
@dataclass(slots=True)
class WatchEvent:
    at: float
    result: ReadResult
    changed: bool
    previous: Value = None

    def to_json(self) -> dict:
        return {
            "at": self.at,
            "changed": self.changed,
            "previous": value_to_json(self.previous),
            **self.result.to_json(),
        }


def watch(
    session: Session,
    text: str,
    *,
    fc: str | None = None,
    interval_s: float = 1.0,
    count: int | None = None,
    duration_s: float | None = None,
    only_changes: bool = True,
) -> Iterator[WatchEvent]:
    """RW-2: poll and yield changes (the first sample is always yielded)."""
    ref, _ = resolve(session, text, fc)
    prev: Value = None
    start = time.monotonic()
    n = 0
    while True:
        res = read(session, ref, with_qt=True)
        changed = n == 0 or not values_equal(prev, res.value)
        if changed or not only_changes:
            yield WatchEvent(time.time(), res, changed, None if n == 0 else prev)
        prev = res.value
        n += 1
        if count is not None and n >= count:
            return
        if duration_s is not None and time.monotonic() - start >= duration_s:
            return
        time.sleep(interval_s)

