"""bType → value type table (the libiec61850 type numbers are tested with the MMS module's simulator export)."""

from __future__ import annotations

import pytest

from ied_client.scl import BTYPES, ValueType, value_type_of
from ied_client.scl.btypes import VALUE_KINDS


@pytest.mark.parametrize(
    ("b_type", "kind", "size"),
    [
        ("BOOLEAN", "boolean", None),
        ("INT8", "integer", 8),
        ("INT32", "integer", 32),
        ("INT32U", "unsigned", 32),
        ("INT8U", "unsigned", 8),
        ("FLOAT32", "float", 32),
        ("FLOAT64", "float", 64),
        ("Enum", "integer", 8),
        ("Dbpos", "bit-string", 2),
        ("Tcmd", "bit-string", 2),
        ("Quality", "bit-string", 13),
        ("Timestamp", "utc-time", None),
        ("EntryTime", "binary-time", 6),
        ("VisString255", "visible-string", 255),
        ("VisString64", "visible-string", 64),
        ("Unicode255", "mms-string", 255),
        ("Octet64", "octet-string", 64),
        ("Check", "bit-string", 2),
        ("TrgOps", "bit-string", 6),
        ("OptFlds", "bit-string", 10),
        ("ObjRef", "visible-string", 129),
        ("Currency", "visible-string", 3),
        ("PhyComAddr", "structure", None),
        ("Struct", "structure", None),
    ],
)
def test_value_type(b_type: str, kind: str, size: int | None) -> None:
    assert value_type_of(b_type) == ValueType(kind, size)


def test_arrays_and_unknown() -> None:
    t = value_type_of("FLOAT32", 4)
    assert t == ValueType("array", 4, ValueType("float", 32))
    assert str(t) == "array[4] of float/32"
    assert value_type_of("NoSuchType") == ValueType("unknown")


def test_table_uses_known_kinds() -> None:
    assert {i.value_type.kind for i in BTYPES.values()} <= VALUE_KINDS
