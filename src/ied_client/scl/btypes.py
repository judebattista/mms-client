"""SCL basic types (``bType``): the value type a client sees (in the client's ``ValueKind`` spellings).

``ValueType.size`` uses the same meaning as ``VarSpec.size``: bits for
``integer`` / ``unsigned`` / ``bit-string``, format width (32/64) for ``float``, maximum
length for strings and octet strings, octet count for ``binary-time``, element count for
``array``; ``None`` where the size carries no information (``boolean``, ``utc-time``,
``structure``).

=============  ======================
bType          value kind / size
=============  ======================
BOOLEAN        boolean
INT8           integer/8
INT16          integer/16
INT24          integer/24
INT32          integer/32
INT64          integer/64
INT128         integer/128
INT8U          unsigned/8
INT16U         unsigned/16
INT24U         unsigned/24
INT32U         unsigned/32
FLOAT32        float/32
FLOAT64        float/64
Enum           integer/8
Octet64        octet-string/64
Octet6         octet-string/6
Octet16        octet-string/16
EntryID        octet-string/8
VisString32    visible-string/32
VisString64    visible-string/64
VisString65    visible-string/65
VisString129   visible-string/129
ObjRef         visible-string/129
VisString255   visible-string/255
Unicode255     mms-string/255
Timestamp      utc-time
Quality        bit-string/13
Check          bit-string/2
Dbpos          bit-string/2
Tcmd           bit-string/2
Struct         structure
EntryTime      binary-time/6
PhyComAddr     structure
Currency       visible-string/3
OptFlds        bit-string/10
TrgOps         bit-string/6
SvOptFlds      bit-string/5
LogOptFlds     bit-string/1
=============  ======================

``Enum`` is ``integer/8`` by default; a device may legitimately use a wider integer, so
comparisons should treat the Enum size as a default rather than a requirement.
Unrecognised bTypes map to kind ``"unknown"``.
"""

from __future__ import annotations

from dataclasses import dataclass

#: The value kinds used for expected types (spelled like ``ied_client.protocol.types.ValueKind``).
VALUE_KINDS: frozenset[str] = frozenset(
    {
        "boolean",
        "integer",
        "unsigned",
        "float",
        "bit-string",
        "octet-string",
        "visible-string",
        "mms-string",
        "utc-time",
        "binary-time",
        "structure",
        "array",
        "unknown",
    }
)


@dataclass(frozen=True, slots=True)
class ValueType:
    """The value type of an attribute: ``kind`` plus ``size`` (see module docstring).

    For ``kind == "array"``, ``size`` is the element count and ``element`` the element type.
    """

    kind: str
    size: int | None = None
    element: ValueType | None = None

    def __str__(self) -> str:
        if self.kind == "array" and self.element is not None:
            return f"array[{self.size}] of {self.element}"
        return self.kind if self.size is None else f"{self.kind}/{self.size}"


@dataclass(frozen=True, slots=True)
class BTypeInfo:
    """Everything we know about one SCL bType.

    ``value_kind`` says how a configured ``Val`` is interpreted: ``bool``, ``int``, ``uint``,
    ``float``, ``enum``, ``string`` or ``raw`` (kept as text: bit strings, times, octets).
    """

    b_type: str
    value_type: ValueType
    value_kind: str


def _b(b_type: str, kind: str, size: int | None, value_kind: str) -> BTypeInfo:
    return BTypeInfo(b_type, ValueType(kind, size), value_kind)


#: bType → info. Keys are the exact SCL spellings.
BTYPES: dict[str, BTypeInfo] = {
    info.b_type: info
    for info in (
        _b("BOOLEAN", "boolean", None, "bool"),
        _b("INT8", "integer", 8, "int"),
        _b("INT16", "integer", 16, "int"),
        _b("INT24", "integer", 24, "int"),
        _b("INT32", "integer", 32, "int"),
        _b("INT64", "integer", 64, "int"),
        _b("INT128", "integer", 128, "int"),
        _b("INT8U", "unsigned", 8, "uint"),
        _b("INT16U", "unsigned", 16, "uint"),
        _b("INT24U", "unsigned", 24, "uint"),
        _b("INT32U", "unsigned", 32, "uint"),
        _b("FLOAT32", "float", 32, "float"),
        _b("FLOAT64", "float", 64, "float"),
        _b("Enum", "integer", 8, "enum"),
        _b("Octet64", "octet-string", 64, "raw"),
        _b("Octet6", "octet-string", 6, "raw"),
        _b("Octet16", "octet-string", 16, "raw"),
        _b("EntryID", "octet-string", 8, "raw"),
        _b("VisString32", "visible-string", 32, "string"),
        _b("VisString64", "visible-string", 64, "string"),
        _b("VisString65", "visible-string", 65, "string"),
        _b("VisString129", "visible-string", 129, "string"),
        _b("ObjRef", "visible-string", 129, "string"),
        _b("VisString255", "visible-string", 255, "string"),
        _b("Unicode255", "mms-string", 255, "string"),
        _b("Timestamp", "utc-time", None, "raw"),
        _b("Quality", "bit-string", 13, "raw"),
        _b("Check", "bit-string", 2, "raw"),
        _b("Dbpos", "bit-string", 2, "raw"),
        _b("Tcmd", "bit-string", 2, "raw"),
        _b("Struct", "structure", None, "raw"),
        _b("EntryTime", "binary-time", 6, "raw"),
        _b("PhyComAddr", "structure", None, "raw"),
        _b("Currency", "visible-string", 3, "string"),
        _b("OptFlds", "bit-string", 10, "raw"),
        _b("TrgOps", "bit-string", 6, "raw"),
        _b("SvOptFlds", "bit-string", 5, "raw"),
        _b("LogOptFlds", "bit-string", 1, "raw"),
    )
}

_UNKNOWN = ValueType("unknown")


def btype_info(b_type: str) -> BTypeInfo | None:
    """Info for a bType, or None if the bType is not known."""
    return BTYPES.get(b_type)


def value_type_of(b_type: str, count: int = 0) -> ValueType:
    """The value type a client sees for an attribute of this bType (array when ``count`` > 0)."""
    info = BTYPES.get(b_type)
    base = info.value_type if info is not None else _UNKNOWN
    if count > 0:
        return ValueType("array", count, base)
    return base
