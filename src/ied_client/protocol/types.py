"""Plain-Python data types that cross the boundary between the client and a protocol module.

Nothing here refers to native memory or to one protocol: a protocol module converts what its
stack returns into these types, so the core never manages a stack's lifetimes or encodings.
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from ied_client.codes import ErrorInfo

if TYPE_CHECKING:
    from ied_client.core.refs import ObjectRef


class ValueKind(StrEnum):
    """Basic and constructed value types of the data model.

    The values are persisted in snapshots and session logs (``type_name()``), so they keep the
    spellings they had when MMS was the only protocol.
    """

    ARRAY = "array"
    STRUCTURE = "structure"
    BOOLEAN = "boolean"
    BIT_STRING = "bit-string"
    INTEGER = "integer"
    UNSIGNED = "unsigned"
    FLOAT = "float"
    OCTET_STRING = "octet-string"
    VISIBLE_STRING = "visible-string"
    GENERALIZED_TIME = "generalized-time"
    BINARY_TIME = "binary-time"
    BCD = "bcd"
    OBJ_ID = "obj-id"
    UNICODE_STRING = "mms-string"
    UTC_TIME = "utc-time"
    ACCESS_ERROR = "data-access-error"


@dataclass(frozen=True, slots=True)
class VarSpec:
    """Type description of a variable, as the device's model declares it.

    ``size`` is: bits for integer/unsigned/bit-string, the format width (32/64) for float,
    the maximum length for strings and octet strings (negative = variable length), the octet
    count (4 or 6) for binary-time, and the element count for arrays and structures.
    """

    kind: ValueKind
    name: str | None = None
    size: int | None = None
    children: tuple[VarSpec, ...] = ()
    element: VarSpec | None = None

    def child(self, name: str) -> VarSpec | None:
        for c in self.children:
            if c.name == name:
                return c
        return None

    def find(self, path: list[str] | tuple[str, ...]) -> VarSpec | None:
        """Descend by component names (array indices given as decimal strings)."""
        node: VarSpec | None = self
        for part in path:
            if node is None:
                return None
            if node.kind is ValueKind.ARRAY and part.isdigit():
                node = node.element
            else:
                node = node.child(part)
        return node

    @property
    def is_basic(self) -> bool:
        return self.kind not in (ValueKind.STRUCTURE, ValueKind.ARRAY)

    def type_name(self) -> str:
        """Compact human-readable type, e.g. ``integer(32)``, ``struct[3]``, ``visible-string(255)``."""
        if self.kind is ValueKind.STRUCTURE:
            return f"struct[{len(self.children)}]"
        if self.kind is ValueKind.ARRAY:
            inner = self.element.type_name() if self.element else "?"
            return f"array[{self.size}] of {inner}"
        if self.kind in (ValueKind.BOOLEAN, ValueKind.UTC_TIME) or self.size is None:
            return self.kind.value
        if self.size < 0:  # variable length with a maximum
            return f"{self.kind.value}(<={-self.size})"
        return f"{self.kind.value}({self.size})"

    def walk(self, prefix: tuple[str, ...] = ()) -> Iterator[tuple[tuple[str, ...], VarSpec]]:
        """Yield (path, spec) for this node and all descendants (depth first, document order)."""
        yield prefix, self
        for c in self.children:
            yield from c.walk((*prefix, c.name or "?"))

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": self.kind.value}
        if self.name is not None:
            out["name"] = self.name
        if self.size is not None:
            out["size"] = self.size
        if self.children:
            out["children"] = [c.to_json() for c in self.children]
        if self.element is not None:
            out["element"] = self.element.to_json()
        return out

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> VarSpec:
        return cls(
            kind=ValueKind(d["kind"]),
            name=d.get("name"),
            size=d.get("size"),
            children=tuple(cls.from_json(c) for c in d.get("children", ())),
            element=cls.from_json(d["element"]) if d.get("element") else None,
        )


# ---------------------------------------------------------------------------
# Decoded values
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class BitString:
    """A bit string; ``bits[0]`` is the first bit on the wire (bit position 0)."""

    bits: tuple[bool, ...]

    @property
    def size(self) -> int:
        return len(self.bits)

    def as_int_lsb0(self) -> int:
        """Integer where bit position n has weight 2**n (used for Quality, TrgOps and OptFlds)."""
        return sum(1 << i for i, b in enumerate(self.bits) if b)

    def as_int_msb0(self) -> int:
        """Integer where bit position 0 is the most significant bit (used for Dbpos/Tcmd)."""
        n = len(self.bits)
        return sum(1 << (n - 1 - i) for i, b in enumerate(self.bits) if b)

    @classmethod
    def from_int_lsb0(cls, value: int, size: int) -> BitString:
        return cls(tuple(bool(value >> i & 1) for i in range(size)))

    @classmethod
    def from_text(cls, text: str) -> BitString:
        return cls(tuple(c == "1" for c in text))

    def __str__(self) -> str:
        return "".join("1" if b else "0" for b in self.bits)


@dataclass(frozen=True, slots=True)
class UtcTime:
    """IEC 61850 TimeStamp: milliseconds + microseconds since the epoch and the time quality byte."""

    ms: int
    us: int = 0
    quality: int = 0

    @property
    def leap_second_known(self) -> bool:
        return bool(self.quality & 0x80)

    @property
    def clock_failure(self) -> bool:
        return bool(self.quality & 0x40)

    @property
    def clock_not_synchronized(self) -> bool:
        return bool(self.quality & 0x20)

    @property
    def accuracy_bits(self) -> int:
        return self.quality & 0x1F

    def datetime(self) -> _dt.datetime:
        return _dt.datetime.fromtimestamp(self.ms / 1000, tz=_dt.UTC).replace(
            microsecond=(self.ms % 1000) * 1000 + (self.us % 1000)
        )

    def iso(self) -> str:
        if self.ms == 0 and self.us == 0:
            return "1970-01-01T00:00:00.000Z"
        try:
            return self.datetime().isoformat(timespec="microseconds").replace("+00:00", "Z")
        except (OverflowError, ValueError, OSError):
            return f"<invalid time {self.ms} ms>"

    def flags(self) -> list[str]:
        out = []
        if self.leap_second_known:
            out.append("leap-second-known")
        if self.clock_failure:
            out.append("clock-failure")
        if self.clock_not_synchronized:
            out.append("clock-not-synchronized")
        return out

    def __str__(self) -> str:
        f = self.flags()
        return self.iso() + (f" [{', '.join(f)}]" if f else "")


@dataclass(frozen=True, slots=True)
class BinaryTime:
    """IEC 61850 EntryTime: milliseconds since the epoch."""

    ms: int

    def iso(self) -> str:
        try:
            return _dt.datetime.fromtimestamp(self.ms / 1000, tz=_dt.UTC).isoformat(timespec="milliseconds").replace(
                "+00:00", "Z"
            )
        except (OverflowError, ValueError, OSError):
            return f"<invalid time {self.ms} ms>"

    def __str__(self) -> str:
        return self.iso()


@dataclass(frozen=True, slots=True)
class AccessError:
    """A read that returned an access error instead of a value (also used in dataset reads)."""

    error: ErrorInfo

    def __str__(self) -> str:
        return f"<{self.error.name}>"


@dataclass(frozen=True, slots=True)
class RawValue:
    """A value of a rarely used type kept in raw form (generalized-time, bcd, obj-id)."""

    kind: ValueKind
    text: str

    def __str__(self) -> str:
        return self.text


# A decoded value is one of:
#   bool | int | float | str | bytes | BitString | UtcTime | BinaryTime | AccessError | RawValue
#   | list[Value] (array, or structure without spec) | dict[str, Value] (structure with spec)
Value = Any


def value_to_json(v: Value) -> Any:
    """Loss-less, JSON-serialisable form of a decoded value."""
    if isinstance(v, bool | int | float | str) or v is None:
        return v
    if isinstance(v, bytes):
        return {"octets": v.hex()}
    if isinstance(v, BitString):
        return {"bits": str(v)}
    if isinstance(v, UtcTime):
        return {"utc": v.iso(), "ms": v.ms, "us": v.us, "quality": v.quality}
    if isinstance(v, BinaryTime):
        return {"binary_time": v.iso(), "ms": v.ms}
    if isinstance(v, AccessError):
        return {"error": v.error.to_json()}
    if isinstance(v, RawValue):
        return {"raw": v.text, "kind": v.kind.value}
    if isinstance(v, dict):
        return {k: value_to_json(x) for k, x in v.items()}
    if isinstance(v, list | tuple):
        return [value_to_json(x) for x in v]
    return repr(v)


def value_from_json(j: Any) -> Value:
    """Inverse of :func:`value_to_json` (used when loading snapshots and session logs)."""
    if isinstance(j, dict):
        if "octets" in j and len(j) == 1:
            return bytes.fromhex(j["octets"])
        if "bits" in j and len(j) == 1:
            return BitString.from_text(j["bits"])
        if "utc" in j and "ms" in j:
            return UtcTime(j["ms"], j.get("us", 0), j.get("quality", 0))
        if "binary_time" in j:
            return BinaryTime(j["ms"])
        if "error" in j and len(j) == 1 and isinstance(j["error"], dict):
            e = j["error"]
            return AccessError(ErrorInfo(e["domain"], e.get("code"), e["name"]))
        if "raw" in j and "kind" in j:
            return RawValue(ValueKind(j["kind"]), j["raw"])
        return {k: value_from_json(x) for k, x in j.items()}
    if isinstance(j, list):
        return [value_from_json(x) for x in j]
    return j


def format_value(v: Value, max_len: int = 200) -> str:
    """Short single-line human-readable rendering."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, bytes):
        s = v.hex(" ") if v else "(empty)"
    elif isinstance(v, str):
        s = repr(v)
    elif isinstance(v, dict):
        s = "{" + ", ".join(f"{k}: {format_value(x, max_len)}" for k, x in v.items()) + "}"
    elif isinstance(v, list | tuple):
        s = "[" + ", ".join(format_value(x, max_len) for x in v) + "]"
    else:
        s = str(v)
    return s if len(s) <= max_len else s[: max_len - 1] + "…"


# ---------------------------------------------------------------------------
# Records returned by services
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ServerIdentity:
    vendor: str | None
    model: str | None
    revision: str | None

    def to_json(self) -> dict:
        return {"vendor": self.vendor, "model": self.model, "revision": self.revision}


@dataclass(frozen=True, slots=True)
class DatasetRef:
    """A dataset: the LN it belongs to (empty if the protocol reports none) and its name, in an LD."""

    ld: str
    ln: str
    name: str

    def iec(self) -> str:
        return f"{self.ld}/{self.ln}.{self.name}" if self.ln else f"{self.ld}/{self.name}"

    def __str__(self) -> str:
        return self.iec()


@dataclass(frozen=True, slots=True)
class DataSetMember:
    """One member of a dataset, as the device lists it.

    ``native`` is the protocol's own name for the member, shown and stored as reported. ``ref`` is
    the member as an object reference with its FC, or None when the protocol module could not turn
    the native name into one (``problem`` says why).
    """

    native: str
    ref: ObjectRef | None
    problem: str | None = None


@dataclass(frozen=True, slots=True)
class FileEntry:
    name: str
    size: int
    last_modified_ms: int

    def last_modified_iso(self) -> str:
        if not self.last_modified_ms:
            return ""
        return BinaryTime(self.last_modified_ms).iso()

    def to_json(self) -> dict:
        return {"name": self.name, "size": self.size, "last_modified": self.last_modified_iso()}


@dataclass(slots=True)
class RcbValues:
    """Attributes of a report control block as read from the device (None = not present).

    ``dataset`` is the DatSet value as the device reports it (the protocol's own form); ``trg_ops``
    and ``opt_flds`` use the bit encoding of :data:`ied_client.codes.TRG_OPS` / ``OPT_FLDS``.
    """

    reference: str
    buffered: bool
    rpt_id: str | None = None
    rpt_ena: bool | None = None
    resv: bool | None = None
    dataset: str | None = None
    conf_rev: int | None = None
    opt_flds: int | None = None
    buf_tm: int | None = None
    sq_num: int | None = None
    trg_ops: int | None = None
    intg_pd: int | None = None
    gi: bool | None = None
    purge_buf: bool | None = None
    entry_id: bytes | None = None
    time_of_entry_ms: int | None = None
    resv_tms: int | None = None
    owner: bytes | None = None

    def to_json(self) -> dict:
        d = {k: getattr(self, k) for k in self.__slots__}
        for k in ("entry_id", "owner"):
            if d[k] is not None:
                d[k] = d[k].hex()
        return d


# RCB attributes a client may write (the keys of Association.set_rcb).
RCB_WRITABLE: frozenset[str] = frozenset(
    {"rpt_id", "rpt_ena", "resv", "dataset", "opt_flds", "buf_tm", "trg_ops", "intg_pd", "gi", "purge_buf", "entry_id",
     "resv_tms"}
)


@dataclass(slots=True)
class Report:
    """A received report, decoded by the protocol module and handed over by value."""

    rcb_reference: str
    rpt_id: str
    received_at: float  # time.time() at arrival
    dataset: str | None = None
    seq_num: int | None = None
    sub_seq_num: int | None = None
    more_segments_follow: bool = False
    timestamp_ms: int | None = None
    conf_rev: int | None = None
    buf_ovfl: bool | None = None
    entry_id: bytes | None = None
    # per dataset member: (index, reason names, value) for members included in the report
    entries: list[ReportEntry] = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            "rcb": self.rcb_reference,
            "rpt_id": self.rpt_id,
            "received_at": self.received_at,
            "dataset": self.dataset,
            "seq_num": self.seq_num,
            "sub_seq_num": self.sub_seq_num,
            "more_segments_follow": self.more_segments_follow,
            "timestamp_ms": self.timestamp_ms,
            "conf_rev": self.conf_rev,
            "buf_ovfl": self.buf_ovfl,
            "entry_id": self.entry_id.hex() if self.entry_id is not None else None,
            "entries": [e.to_json() for e in self.entries],
        }


@dataclass(slots=True)
class ReportEntry:
    index: int
    reasons: tuple[str, ...]
    value: Value
    data_reference: str | None = None

    def to_json(self) -> dict:
        return {
            "index": self.index,
            "reasons": list(self.reasons),
            "value": value_to_json(self.value),
            "data_reference": self.data_reference,
        }


@dataclass(frozen=True, slots=True)
class ControlStepResult:
    """Outcome of one control service call (select / operate / cancel).

    ``ied_error`` is the error the protocol stack reported for the call; its ``code`` is 0 when the
    stack reported none.
    """

    service: str
    ok: bool
    duration_s: float
    ied_error: ErrorInfo
    add_cause: ErrorInfo | None = None
    ctl_error: ErrorInfo | None = None
    ctl_num: int | None = None

    def to_json(self) -> dict:
        return {
            "service": self.service,
            "ok": self.ok,
            "duration_s": round(self.duration_s, 6),
            "ied_error": self.ied_error.to_json(),
            "add_cause": self.add_cause.to_json() if self.add_cause else None,
            "ctl_error": self.ctl_error.to_json() if self.ctl_error else None,
            "ctl_num": self.ctl_num,
        }


@dataclass(frozen=True, slots=True)
class CommandTermination:
    """CommandTermination received for an enhanced-security control."""

    positive: bool
    received_at: float
    add_cause: ErrorInfo | None
    ctl_error: ErrorInfo | None
    ctl_num: int | None

    def to_json(self) -> dict:
        return {
            "positive": self.positive,
            "received_at": self.received_at,
            "add_cause": self.add_cause.to_json() if self.add_cause else None,
            "ctl_error": self.ctl_error.to_json() if self.ctl_error else None,
            "ctl_num": self.ctl_num,
        }
