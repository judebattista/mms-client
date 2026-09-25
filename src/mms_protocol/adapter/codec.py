"""Conversion between native ``MmsValue`` / ``MmsVariableSpecification`` and Python objects.

The adapter owns every native lifetime: :func:`decode` copies a value out of native memory,
:func:`encode` returns a *new* native value that the caller must free with
``lib().MmsValue_delete`` (the :func:`encoded` context manager does that).

RW-3: values are always encoded from the device's own :class:`VarSpec`; the Python input is
validated against it (:func:`ied_client.protocol.values.coerce`) and never used to guess a type.
"""

from __future__ import annotations

import ctypes as C
import math
import struct
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager

from ied_client.protocol.errors import EncodeError
from ied_client.protocol.types import (
    AccessError,
    BinaryTime,
    BitString,
    RawValue,
    UtcTime,
    Value,
    ValueKind,
    VarSpec,
)
from ied_client.protocol.values import coerce

from ..codes import data_access_error
from ._native import lib
from .types import MMS_KIND_BY_NUMBER


# ---------------------------------------------------------------------------
# Specifications
# ---------------------------------------------------------------------------
def spec_from_native(p: int, _depth: int = 0) -> VarSpec:
    """Copy a native MmsVariableSpecification tree (does not free it)."""
    L = lib()
    if _depth > 64:
        raise ValueError("variable specification nested too deeply")
    kind = MMS_KIND_BY_NUMBER.get(L.MmsVariableSpecification_getType(p), ValueKind.STRUCTURE)
    raw_name = L.MmsVariableSpecification_getName(p)
    name = raw_name.decode("utf-8", "replace") if raw_name else None
    size = L.MmsVariableSpecification_getSize(p)
    if kind is ValueKind.STRUCTURE:
        children = tuple(
            spec_from_native(L.MmsVariableSpecification_getChildSpecificationByIndex(p, i), _depth + 1)
            for i in range(size)
        )
        return VarSpec(kind, name, size, children)
    if kind is ValueKind.ARRAY:
        elem_p = L.MmsVariableSpecification_getArrayElementSpecification(p)
        element = spec_from_native(elem_p, _depth + 1) if elem_p else None
        return VarSpec(kind, name, size, (), element)
    if kind in (ValueKind.BOOLEAN, ValueKind.UTC_TIME):
        size = None
    return VarSpec(kind, name, size)


# ---------------------------------------------------------------------------
# Values: native -> Python
# ---------------------------------------------------------------------------
def _nice_float(v: float) -> float:
    """Present a value that came from a float32 with float32 precision (0.1, not 0.100000001)."""
    if not math.isfinite(v):
        return v
    try:
        f32 = struct.unpack("<f", struct.pack("<f", v))[0]
    except OverflowError:
        return v
    if f32 == v:
        return float(f"{v:.7g}")
    return v


def decode(p: int | None, spec: VarSpec | None = None, _depth: int = 0) -> Value:
    """Copy a native MmsValue into Python objects (see ``types.Value``). Does not free ``p``."""
    if not p:
        return None
    L = lib()
    kind = MMS_KIND_BY_NUMBER.get(L.MmsValue_getType(p))
    if _depth > 64:
        raise ValueError("value nested too deeply")
    if kind is ValueKind.STRUCTURE or kind is ValueKind.ARRAY:
        n = L.MmsValue_getArraySize(p)
        if kind is ValueKind.ARRAY:
            elem_spec = spec.element if spec is not None and spec.kind is ValueKind.ARRAY else None
            return [decode(L.MmsValue_getElement(p, i), elem_spec, _depth + 1) for i in range(n)]
        if spec is not None and spec.kind is ValueKind.STRUCTURE and len(spec.children) == n:
            return {
                (c.name or str(i)): decode(L.MmsValue_getElement(p, i), c, _depth + 1)
                for i, c in enumerate(spec.children)
            }
        return [decode(L.MmsValue_getElement(p, i), None, _depth + 1) for i in range(n)]
    if kind is ValueKind.BOOLEAN:
        return bool(L.MmsValue_getBoolean(p))
    if kind is ValueKind.INTEGER:
        return int(L.MmsValue_toInt64(p))
    if kind is ValueKind.UNSIGNED:
        return int(L.MmsValue_toUint32(p))
    if kind is ValueKind.FLOAT:
        v = float(L.MmsValue_toDouble(p))
        if spec is not None and spec.kind is ValueKind.FLOAT and spec.size == 64:
            return v
        return _nice_float(v)
    if kind is ValueKind.BIT_STRING:
        n = L.MmsValue_getBitStringSize(p)
        return BitString(tuple(bool(L.MmsValue_getBitStringBit(p, i)) for i in range(n)))
    if kind is ValueKind.OCTET_STRING:
        n = L.MmsValue_getOctetStringSize(p)
        buf = L.MmsValue_getOctetStringBuffer(p)
        return bytes(buf[:n]) if n and buf else b""
    if kind in (ValueKind.VISIBLE_STRING, ValueKind.UNICODE_STRING):
        s = L.MmsValue_toString(p)
        return C.string_at(s).decode("utf-8", "replace") if s else ""
    if kind is ValueKind.UTC_TIME:
        # Decode the 8 octets ourselves: libiec61850 truncates the 24-bit fraction, so a time
        # written as .123 s would read back as .122999 s. Round to the nearest microsecond.
        buf = L.MmsValue_getUtcTimeBuffer(p)
        raw = bytes(buf[:8])
        secs = int.from_bytes(raw[0:4], "big")
        total_us = secs * 1_000_000 + round(int.from_bytes(raw[4:7], "big") * 1_000_000 / (1 << 24))
        return UtcTime(total_us // 1000, total_us % 1000, raw[7])
    if kind is ValueKind.BINARY_TIME:
        return BinaryTime(int(L.MmsValue_getBinaryTimeAsUtcMs(p)))
    if kind is ValueKind.ACCESS_ERROR:
        return AccessError(data_access_error(int(L.MmsValue_getDataAccessError(p))))
    if kind is None:
        return RawValue(ValueKind.OBJ_ID, "<unknown MMS type>")
    return RawValue(kind, f"<{kind.value} value not decoded>")


# ---------------------------------------------------------------------------
# Values: Python -> native
# ---------------------------------------------------------------------------
def encode(spec: VarSpec, value: Value, path: str = "value") -> int:
    """Build a new native MmsValue of type ``spec`` holding ``value``.

    Raises :class:`EncodeError` when the value does not fit the specification. The caller owns
    the result and must free it (see :func:`encoded`).
    """
    L = lib()
    k = spec.kind
    if k is ValueKind.STRUCTURE:
        if isinstance(value, Mapping):
            missing = [c.name for c in spec.children if c.name not in value]
            extra = [n for n in value if spec.child(n) is None]
            if missing or extra:
                raise EncodeError(
                    f"{path}: structure needs exactly the components {[c.name for c in spec.children]}"
                    + (f"; missing {missing}" if missing else "")
                    + (f"; unknown {extra}" if extra else "")
                )
            items = [value[c.name] for c in spec.children]
        elif isinstance(value, Sequence) and not isinstance(value, str | bytes):
            if len(value) != len(spec.children):
                raise EncodeError(f"{path}: structure has {len(spec.children)} components, got {len(value)}")
            items = list(value)
        else:
            raise EncodeError(f"{path}: expected a structure (mapping of components), got {value!r}")
        p = L.MmsValue_createEmptyStructure(len(spec.children))
        try:
            for i, (c, v) in enumerate(zip(spec.children, items, strict=True)):
                L.MmsValue_setElement(p, i, encode(c, v, f"{path}.{c.name}"))
        except BaseException:
            L.MmsValue_delete(p)
            raise
        return p
    if k is ValueKind.ARRAY:
        if not isinstance(value, Sequence) or isinstance(value, str | bytes):
            raise EncodeError(f"{path}: expected an array, got {value!r}")
        if spec.size is not None and spec.size >= 0 and len(value) != spec.size:
            raise EncodeError(f"{path}: array has {spec.size} elements, got {len(value)}")
        if spec.element is None:
            raise EncodeError(f"{path}: array element type unknown")
        p = L.MmsValue_createEmptyArray(len(value))
        try:
            for i, v in enumerate(value):
                L.MmsValue_setElement(p, i, encode(spec.element, v, f"{path}[{i}]"))
        except BaseException:
            L.MmsValue_delete(p)
            raise
        return p
    value = coerce(spec, value, path)
    if k is ValueKind.BOOLEAN:
        return L.MmsValue_newBoolean(value)
    if k is ValueKind.INTEGER:
        bits = spec.size if spec.size in (8, 16, 32, 64) else 64
        return L.MmsValue_newIntegerFromInt32(value) if bits <= 32 else L.MmsValue_newIntegerFromInt64(value)
    if k is ValueKind.UNSIGNED:
        return L.MmsValue_newUnsignedFromUint32(value)
    if k is ValueKind.FLOAT:
        if spec.size == 64:
            return L.MmsValue_newDouble(float(value))
        return L.MmsValue_newFloat(float(value))
    if k is ValueKind.BIT_STRING:
        p = L.MmsValue_newBitString(value.size)
        for i, b in enumerate(value.bits):
            L.MmsValue_setBitStringBit(p, i, b)
        return p
    if k is ValueKind.OCTET_STRING:
        max_size = spec.size if spec.size is not None else len(value)
        p = L.MmsValue_newOctetString(len(value), abs(max_size) or len(value))
        buf = (C.c_uint8 * max(len(value), 1)).from_buffer_copy(bytes(value) or b"\0")
        L.MmsValue_setOctetString(p, buf, len(value))
        return p
    if k in (ValueKind.VISIBLE_STRING, ValueKind.UNICODE_STRING):
        raw = value.encode("utf-8")
        return L.MmsValue_newVisibleString(raw) if k is ValueKind.VISIBLE_STRING else L.MmsValue_newMmsString(raw)
    if k is ValueKind.UTC_TIME:
        p = L.MmsValue_newUtcTimeByMsTime(value.ms)
        L.MmsValue_setUtcTimeQuality(p, value.quality & 0xFF)
        return p
    if k is ValueKind.BINARY_TIME:
        p = L.MmsValue_newBinaryTime(spec.size == 4)
        L.MmsValue_setBinaryTime(p, value.ms)
        return p
    raise EncodeError(f"{path}: writing values of type {k.value} is not supported")


@contextmanager
def encoded(spec: VarSpec, value: Value) -> Iterator[int]:
    p = encode(spec, value)
    try:
        yield p
    finally:
        lib().MmsValue_delete(p)
