"""Checking values against the device's declared type, and parsing operator input (RW-3).

A value is always checked against the device's own :class:`VarSpec`; the input is never used to guess
a type. Protocol modules call :func:`coerce` before encoding a basic value, so the rules (and the
messages) are the same whatever the protocol.
"""

from __future__ import annotations

import math
from typing import Any

from .errors import EncodeError
from .types import BinaryTime, BitString, UtcTime, Value, ValueKind, VarSpec

INT_LIMITS = {8: (-(2**7), 2**7 - 1), 16: (-(2**15), 2**15 - 1), 32: (-(2**31), 2**31 - 1), 64: (-(2**63), 2**63 - 1)}


def _check_int(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise EncodeError(f"{path}: expected an integer, got {value!r}")
    return value


def coerce(spec: VarSpec, value: Value, path: str = "value") -> Value:
    """``value`` checked against the basic type ``spec`` (hex text becomes octets, 0/1 text a bit string).

    Raises :class:`EncodeError` when the value does not fit, or when ``spec`` is not a basic type that
    can be written.
    """
    k = spec.kind
    if k is ValueKind.BOOLEAN:
        if not isinstance(value, bool):
            raise EncodeError(f"{path}: expected true/false, got {value!r}")
        return value
    if k is ValueKind.INTEGER:
        v = _check_int(value, path)
        bits = spec.size if spec.size in INT_LIMITS else 64
        lo, hi = INT_LIMITS[bits]
        if not lo <= v <= hi:
            raise EncodeError(f"{path}: {v} does not fit a {bits}-bit signed integer ({lo}..{hi})")
        return v
    if k is ValueKind.UNSIGNED:
        v = _check_int(value, path)
        bits = spec.size if spec.size and spec.size > 0 else 32
        if not 0 <= v < 2 ** min(bits, 32):
            raise EncodeError(f"{path}: {v} does not fit a {bits}-bit unsigned integer")
        return v
    if k is ValueKind.FLOAT:
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise EncodeError(f"{path}: expected a number, got {value!r}")
        return value
    if k is ValueKind.BIT_STRING:
        if isinstance(value, str) and set(value) <= {"0", "1"}:
            value = BitString.from_text(value)
        if not isinstance(value, BitString):
            raise EncodeError(f"{path}: expected a bit string such as '0110', got {value!r}")
        size = spec.size if spec.size is not None else value.size
        if size >= 0 and value.size != size:
            raise EncodeError(f"{path}: bit string needs {size} bits, got {value.size}")
        return value
    if k is ValueKind.OCTET_STRING:
        if isinstance(value, str):
            try:
                value = bytes.fromhex(value.replace(" ", "").replace(":", ""))
            except ValueError as exc:
                raise EncodeError(f"{path}: expected hex octets, got {value!r}") from exc
        if not isinstance(value, bytes | bytearray):
            raise EncodeError(f"{path}: expected octets, got {value!r}")
        max_size = spec.size if spec.size is not None else len(value)
        if abs(max_size) and len(value) > abs(max_size):
            raise EncodeError(f"{path}: at most {abs(max_size)} octets allowed, got {len(value)}")
        return bytes(value)
    if k in (ValueKind.VISIBLE_STRING, ValueKind.UNICODE_STRING):
        if not isinstance(value, str):
            raise EncodeError(f"{path}: expected text, got {value!r}")
        if k is ValueKind.VISIBLE_STRING and not all(0x20 <= ord(ch) <= 0x7E for ch in value):
            raise EncodeError(f"{path}: visible strings may only contain printable ASCII")
        limit = abs(spec.size) if spec.size else 0
        if limit and len(value.encode("utf-8")) > limit:
            raise EncodeError(f"{path}: at most {limit} characters allowed")
        return value
    if k is ValueKind.UTC_TIME:
        if not isinstance(value, UtcTime):
            raise EncodeError(f"{path}: expected a timestamp, got {value!r}")
        return value
    if k is ValueKind.BINARY_TIME:
        if not isinstance(value, BinaryTime):
            raise EncodeError(f"{path}: expected a binary time, got {value!r}")
        return value
    raise EncodeError(f"{path}: writing values of type {k.value} is not supported")


_TRUE = {"true", "1", "on", "yes"}
_FALSE = {"false", "0", "off", "no"}


def parse_text(spec: VarSpec, text: str, path: str = "value") -> Value:
    """Turn command-line text into a value of the type the device's model declares.

    Refuses (EncodeError) when the type is constructed or not representable as text; structured
    values must be written component by component.
    """
    t = text.strip()
    k = spec.kind
    if k is ValueKind.BOOLEAN:
        low = t.lower()
        if low in _TRUE:
            return True
        if low in _FALSE:
            return False
        raise EncodeError(f"{path}: expected true/false, got {text!r}")
    if k in (ValueKind.INTEGER, ValueKind.UNSIGNED):
        try:
            v = int(t, 0)
        except ValueError as exc:
            raise EncodeError(f"{path}: expected an integer, got {text!r}") from exc
        return coerce(spec, v)
    if k is ValueKind.FLOAT:
        try:
            f = float(t)
        except ValueError as exc:
            raise EncodeError(f"{path}: expected a number, got {text!r}") from exc
        if not math.isfinite(f):
            raise EncodeError(f"{path}: value must be finite")
        return f
    if k is ValueKind.BIT_STRING:
        if not t or set(t) - {"0", "1"}:
            raise EncodeError(f"{path}: expected a bit string of 0/1 characters, got {text!r}")
        return coerce(spec, BitString.from_text(t))
    if k is ValueKind.OCTET_STRING:
        try:
            b = bytes.fromhex(t.replace(" ", "").replace(":", ""))
        except ValueError as exc:
            raise EncodeError(f"{path}: expected hex octets, got {text!r}") from exc
        return coerce(spec, b)
    if k in (ValueKind.VISIBLE_STRING, ValueKind.UNICODE_STRING):
        if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'":
            t = t[1:-1]
        return coerce(spec, t)
    raise EncodeError(
        f"{path}: values of type {spec.type_name()} cannot be written from text; "
        "write the individual components instead"
    )
