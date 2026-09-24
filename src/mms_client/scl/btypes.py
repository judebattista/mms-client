"""SCL basic types (``bType``): the MMS type a client sees, and the libiec61850 type number.

``MmsType.size`` uses the same meaning as the adapter's ``VarSpec.size``: bits for
``integer`` / ``unsigned`` / ``bit-string``, format width (32/64) for ``float``, maximum
length for strings and octet strings, octet count for ``binary-time``, element count for
``array``; ``None`` where the size carries no information (``boolean``, ``utc-time``,
``structure``).

=============  ======================  ===========================
bType          MMS kind / size         libiec61850 DataAttributeType
=============  ======================  ===========================
BOOLEAN        boolean                 0  BOOLEAN
INT8           integer/8               1  INT8
INT16          integer/16              2  INT16
INT24          integer/24              3  INT32 (no INT24 in libiec61850)
INT32          integer/32              3  INT32
INT64          integer/64              4  INT64
INT128         integer/128             5  INT128
INT8U          unsigned/8              6  INT8U
INT16U         unsigned/16             7  INT16U
INT24U         unsigned/24             8  INT24U
INT32U         unsigned/32             9  INT32U
FLOAT32        float/32                10 FLOAT32
FLOAT64        float/64                11 FLOAT64
Enum           integer/8               12 ENUMERATED
Octet64        octet-string/64         13 OCTET_STRING_64
Octet6         octet-string/6          14 OCTET_STRING_6
Octet16        octet-string/16         13 OCTET_STRING_64 (closest available)
EntryID        octet-string/8          15 OCTET_STRING_8
VisString32    visible-string/32       16 VISIBLE_STRING_32
VisString64    visible-string/64       17 VISIBLE_STRING_64
VisString65    visible-string/65       18 VISIBLE_STRING_65
VisString129   visible-string/129      19 VISIBLE_STRING_129
ObjRef         visible-string/129      19 VISIBLE_STRING_129
VisString255   visible-string/255      20 VISIBLE_STRING_255
Unicode255     mms-string/255          21 UNICODE_STRING_255
Timestamp      utc-time                22 TIMESTAMP
Quality        bit-string/13           23 QUALITY
Check          bit-string/2            24 CHECK
Dbpos          bit-string/2            25 CODEDENUM
Tcmd           bit-string/2            25 CODEDENUM
Struct         structure               27 CONSTRUCTED
EntryTime      binary-time/6           28 ENTRY_TIME
PhyComAddr     structure               29 PHYCOMADDR
Currency       visible-string/3        30 CURRENCY
OptFlds        bit-string/10           31 OPTFLDS
TrgOps         bit-string/6            32 TRGOPS
SvOptFlds      bit-string/5            (none: only inside SVCBs)
LogOptFlds     bit-string/1            (none: only inside LCBs)
=============  ======================  ===========================

``Enum`` is ``integer/8`` by default; a device may legitimately use a wider integer, so
comparisons should treat the Enum size as a default rather than a requirement.
Unrecognised bTypes map to kind ``"unknown"`` and no libiec61850 type.
"""

from __future__ import annotations

from dataclasses import dataclass

#: The MMS kinds used for expected types (spelled like the adapter's ``MmsKind`` values).
MMS_KINDS: frozenset[str] = frozenset(
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
class MmsType:
    """The MMS type of an attribute: ``kind`` plus ``size`` (see module docstring).

    For ``kind == "array"``, ``size`` is the element count and ``element`` the element type.
    """

    kind: str
    size: int | None = None
    element: MmsType | None = None

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
    mms: MmsType
    libiec_type: int | None
    value_kind: str


def _b(b_type: str, kind: str, size: int | None, libiec: int | None, value_kind: str) -> BTypeInfo:
    return BTypeInfo(b_type, MmsType(kind, size), libiec, value_kind)


#: bType → info. Keys are the exact SCL spellings.
BTYPES: dict[str, BTypeInfo] = {
    info.b_type: info
    for info in (
        _b("BOOLEAN", "boolean", None, 0, "bool"),
        _b("INT8", "integer", 8, 1, "int"),
        _b("INT16", "integer", 16, 2, "int"),
        _b("INT24", "integer", 24, 3, "int"),
        _b("INT32", "integer", 32, 3, "int"),
        _b("INT64", "integer", 64, 4, "int"),
        _b("INT128", "integer", 128, 5, "int"),
        _b("INT8U", "unsigned", 8, 6, "uint"),
        _b("INT16U", "unsigned", 16, 7, "uint"),
        _b("INT24U", "unsigned", 24, 8, "uint"),
        _b("INT32U", "unsigned", 32, 9, "uint"),
        _b("FLOAT32", "float", 32, 10, "float"),
        _b("FLOAT64", "float", 64, 11, "float"),
        _b("Enum", "integer", 8, 12, "enum"),
        _b("Octet64", "octet-string", 64, 13, "raw"),
        _b("Octet6", "octet-string", 6, 14, "raw"),
        _b("Octet16", "octet-string", 16, 13, "raw"),
        _b("EntryID", "octet-string", 8, 15, "raw"),
        _b("VisString32", "visible-string", 32, 16, "string"),
        _b("VisString64", "visible-string", 64, 17, "string"),
        _b("VisString65", "visible-string", 65, 18, "string"),
        _b("VisString129", "visible-string", 129, 19, "string"),
        _b("ObjRef", "visible-string", 129, 19, "string"),
        _b("VisString255", "visible-string", 255, 20, "string"),
        _b("Unicode255", "mms-string", 255, 21, "string"),
        _b("Timestamp", "utc-time", None, 22, "raw"),
        _b("Quality", "bit-string", 13, 23, "raw"),
        _b("Check", "bit-string", 2, 24, "raw"),
        _b("Dbpos", "bit-string", 2, 25, "raw"),
        _b("Tcmd", "bit-string", 2, 25, "raw"),
        _b("Struct", "structure", None, 27, "raw"),
        _b("EntryTime", "binary-time", 6, 28, "raw"),
        _b("PhyComAddr", "structure", None, 29, "raw"),
        _b("Currency", "visible-string", 3, 30, "string"),
        _b("OptFlds", "bit-string", 10, 31, "raw"),
        _b("TrgOps", "bit-string", 6, 32, "raw"),
        _b("SvOptFlds", "bit-string", 5, None, "raw"),
        _b("LogOptFlds", "bit-string", 1, None, "raw"),
    )
}

#: libiec61850 DataAttributeType number for constructed attributes.
LIBIEC_CONSTRUCTED = 27

_UNKNOWN = MmsType("unknown")


def btype_info(b_type: str) -> BTypeInfo | None:
    """Info for a bType, or None if the bType is not known."""
    return BTYPES.get(b_type)


def mms_type_of(b_type: str, count: int = 0) -> MmsType:
    """The MMS type a client sees for an attribute of this bType (array when ``count`` > 0)."""
    info = BTYPES.get(b_type)
    base = info.mms if info is not None else _UNKNOWN
    if count > 0:
        return MmsType("array", count, base)
    return base


def libiec_type_of(b_type: str) -> int | None:
    """libiec61850 ``DataAttributeType`` number, or None if libiec61850 cannot model it."""
    info = BTYPES.get(b_type)
    return info.libiec_type if info is not None else None
