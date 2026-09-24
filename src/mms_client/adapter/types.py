"""Plain-Python data types returned by the adapter.

Nothing here refers to native memory: every value crossing the adapter boundary is a copy,
so callers never manage libiec61850 lifetimes.
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from mms_client import codes
from mms_client.codes import ErrorInfo


class MmsKind(StrEnum):
    """MMS basic and constructed types (libiec61850 ``MmsType``)."""

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
    MMS_STRING = "mms-string"
    UTC_TIME = "utc-time"
    DATA_ACCESS_ERROR = "data-access-error"


# libiec61850 MmsType numbering
MMS_KIND_BY_NUMBER: dict[int, MmsKind] = {
    0: MmsKind.ARRAY,
    1: MmsKind.STRUCTURE,
    2: MmsKind.BOOLEAN,
    3: MmsKind.BIT_STRING,
    4: MmsKind.INTEGER,
    5: MmsKind.UNSIGNED,
    6: MmsKind.FLOAT,
    7: MmsKind.OCTET_STRING,
    8: MmsKind.VISIBLE_STRING,
    9: MmsKind.GENERALIZED_TIME,
    10: MmsKind.BINARY_TIME,
    11: MmsKind.BCD,
    12: MmsKind.OBJ_ID,
    13: MmsKind.MMS_STRING,
    14: MmsKind.UTC_TIME,
    15: MmsKind.DATA_ACCESS_ERROR,
}


@dataclass(frozen=True, slots=True)
class VarSpec:
    """Type description of an MMS variable, as returned by GetVariableAccessAttributes.

    ``size`` is: bits for integer/unsigned/bit-string, the format width (32/64) for float,
    the maximum length for strings and octet strings (negative = variable length), the octet
    count (4 or 6) for binary-time, and the element count for arrays and structures.
    """

    kind: MmsKind
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
            if node.kind is MmsKind.ARRAY and part.isdigit():
                node = node.element
            else:
                node = node.child(part)
        return node

    @property
    def is_basic(self) -> bool:
        return self.kind not in (MmsKind.STRUCTURE, MmsKind.ARRAY)

    def type_name(self) -> str:
        """Compact human-readable type, e.g. ``integer(32)``, ``struct[3]``, ``visible-string(255)``."""
        if self.kind is MmsKind.STRUCTURE:
            return f"struct[{len(self.children)}]"
        if self.kind is MmsKind.ARRAY:
            inner = self.element.type_name() if self.element else "?"
            return f"array[{self.size}] of {inner}"
        if self.kind in (MmsKind.BOOLEAN, MmsKind.UTC_TIME) or self.size is None:
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
            kind=MmsKind(d["kind"]),
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
        """Integer where bit position n has weight 2**n (libiec61850 ``getBitStringAsInteger``;
        used for Quality, TrgOps and OptFlds)."""
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
    """MMS binary time (IEC 61850 EntryTime): milliseconds since the epoch."""

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
    """A read that returned a DataAccessError instead of a value (also used in dataset reads)."""

    error: ErrorInfo

    @classmethod
    def from_code(cls, code: int) -> AccessError:
        return cls(codes.data_access_error(code))

    def __str__(self) -> str:
        return f"<{self.error.name}>"


@dataclass(frozen=True, slots=True)
class RawValue:
    """A value of a rarely used type kept in raw form (generalized-time, bcd, obj-id)."""

    kind: MmsKind
    text: str

    def __str__(self) -> str:
        return self.text


# A decoded MMS value is one of:
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
            return AccessError(ErrorInfo(codes.Domain(e["domain"]), e.get("code"), e["name"]))
        if "raw" in j and "kind" in j:
            return RawValue(MmsKind(j["kind"]), j["raw"])
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


# MMS services-supported bit positions (ISO 9506 ServiceSupportOptions) that matter here.
SERVICE_BITS: dict[int, str] = {
    0: "status",
    1: "getNameList",
    2: "identify",
    3: "rename",
    4: "read",
    5: "write",
    6: "getVariableAccessAttributes",
    11: "defineNamedVariableList",
    12: "getNamedVariableListAttributes",
    13: "deleteNamedVariableList",
    46: "obtainFile",
    65: "readJournal",
    67: "initializeJournal",
    68: "reportJournalStatus",
    72: "fileOpen",
    73: "fileRead",
    74: "fileClose",
    75: "fileRename",
    76: "fileDelete",
    77: "fileDirectory",
    78: "unsolicitedStatus",
    79: "informationReport",
    83: "conclude",
    84: "cancel",
}


@dataclass(frozen=True, slots=True)
class ConnectionParams:
    """Negotiated MMS initiate parameters."""

    max_pdu_size: int
    max_serv_outstanding_calling: int
    max_serv_outstanding_called: int
    data_structure_nesting_level: int
    services_supported: bytes

    def supports(self, bit: int) -> bool:
        byte, off = divmod(bit, 8)
        if byte >= len(self.services_supported):
            return False
        return bool(self.services_supported[byte] & (0x80 >> off))

    def supported_services(self) -> list[str]:
        return [name for bit, name in sorted(SERVICE_BITS.items()) if self.supports(bit)]

    def to_json(self) -> dict:
        return {
            "max_pdu_size": self.max_pdu_size,
            "max_serv_outstanding_calling": self.max_serv_outstanding_calling,
            "max_serv_outstanding_called": self.max_serv_outstanding_called,
            "data_structure_nesting_level": self.data_structure_nesting_level,
            "services_supported_hex": self.services_supported.hex(),
            "services_supported": self.supported_services(),
        }


@dataclass(frozen=True, slots=True)
class DataSetMember:
    """One entry of a named variable list, in MMS terms."""

    domain: str
    item: str
    array_index: int | None = None
    component: str | None = None

    def mms_ref(self) -> str:
        s = f"{self.domain}/{self.item}"
        if self.array_index is not None:
            s += f"({self.array_index})"
        if self.component:
            s += f"${self.component}"
        return s


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
    """Attributes of a report control block as read from the device (None = not present)."""

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


# Keys accepted by IedClient.set_rcb, mapped to libiec61850 RCB_ELEMENT_* mask bits.
RCB_ELEMENTS: dict[str, int] = {
    "rpt_id": 1,
    "rpt_ena": 2,
    "resv": 4,
    "dataset": 8,
    "conf_rev": 16,
    "opt_flds": 32,
    "buf_tm": 64,
    "sq_num": 128,
    "trg_ops": 256,
    "intg_pd": 512,
    "gi": 1024,
    "purge_buf": 2048,
    "entry_id": 4096,
    "time_of_entry": 8192,
    "resv_tms": 16384,
    "owner": 32768,
}


@dataclass(slots=True)
class Report:
    """A received report, decoded in the callback and handed over by value."""

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
    """Outcome of one control service call (select / operate / cancel)."""

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
