"""bType → MMS type / libiec61850 type table."""

from __future__ import annotations

import pytest

from mms_client.scl import BTYPES, MmsType, libiec_type_of, mms_type_of
from mms_client.scl.btypes import MMS_KINDS


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
def test_mms_type(b_type: str, kind: str, size: int | None) -> None:
    assert mms_type_of(b_type) == MmsType(kind, size)


@pytest.mark.parametrize(
    ("b_type", "number"),
    [
        ("BOOLEAN", 0), ("INT8", 1), ("INT16", 2), ("INT32", 3), ("INT64", 4), ("INT128", 5),
        ("INT8U", 6), ("INT16U", 7), ("INT24U", 8), ("INT32U", 9), ("FLOAT32", 10), ("FLOAT64", 11),
        ("Enum", 12), ("Octet64", 13), ("VisString32", 16), ("VisString64", 17), ("VisString65", 18),
        ("VisString129", 19), ("ObjRef", 19), ("VisString255", 20), ("Unicode255", 21), ("Timestamp", 22),
        ("Quality", 23), ("Check", 24), ("Dbpos", 25), ("Tcmd", 25), ("Struct", 27), ("EntryTime", 28),
        ("PhyComAddr", 29), ("Currency", 30), ("OptFlds", 31), ("TrgOps", 32),
    ],
)
def test_libiec_type_numbers(b_type: str, number: int) -> None:
    assert libiec_type_of(b_type) == number


def test_arrays_and_unknown() -> None:
    t = mms_type_of("FLOAT32", 4)
    assert t == MmsType("array", 4, MmsType("float", 32))
    assert str(t) == "array[4] of float/32"
    assert mms_type_of("NoSuchType") == MmsType("unknown")
    assert libiec_type_of("NoSuchType") is None


def test_table_uses_known_kinds() -> None:
    assert {i.mms.kind for i in BTYPES.values()} <= MMS_KINDS
