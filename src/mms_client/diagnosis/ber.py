"""A small BER (ISO/IEC 8825-1) encoder and decoder for the association probes.

Only what the ISO presentation, ACSE and MMS initiate PDUs need: definite lengths, low and high
tag numbers, INTEGER, BIT STRING, OBJECT IDENTIFIER and constructed values. Decoding never trusts
the peer: every length is checked against the buffer and malformed input raises :class:`BerError`.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

# Tag classes (the two high bits of the identifier octet).
UNIVERSAL = 0x00
APPLICATION = 0x40
CONTEXT = 0x80
PRIVATE = 0xC0
CONSTRUCTED = 0x20

_CLASS_NAMES = {UNIVERSAL: "universal", APPLICATION: "application", CONTEXT: "context", PRIVATE: "private"}


class BerError(ValueError):
    """The bytes are not valid BER (or use a form this decoder does not support)."""


# ---------------------------------------------------------------------------
# Encoding
# ---------------------------------------------------------------------------
def encode_identifier(cls: int, constructed: bool, number: int) -> bytes:
    """Identifier octets for a tag (class bits as in :data:`CONTEXT` etc.)."""
    if number < 0:
        raise ValueError("tag number must be >= 0")
    first = cls | (CONSTRUCTED if constructed else 0)
    if number < 31:
        return bytes([first | number])
    out = [number & 0x7F]
    number >>= 7
    while number:
        out.append(0x80 | (number & 0x7F))
        number >>= 7
    return bytes([first | 0x1F, *reversed(out)])


def encode_length(n: int) -> bytes:
    """Definite-form length octets."""
    if n < 0:
        raise ValueError("length must be >= 0")
    if n < 0x80:
        return bytes([n])
    body = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(body)]) + body


def tlv(tag: int | bytes, value: bytes) -> bytes:
    """Tag + length + value. ``tag`` is a single identifier octet or the full identifier bytes."""
    ident = bytes([tag]) if isinstance(tag, int) else tag
    return ident + encode_length(len(value)) + value


def encode_integer_content(n: int) -> bytes:
    """Minimal two's-complement content octets of an INTEGER."""
    length = max(1, (n.bit_length() + 8) // 8)
    body = n.to_bytes(length, "big", signed=True)
    # strip redundant leading octets (keep the sign bit correct)
    while len(body) > 1 and ((body[0] == 0x00 and body[1] < 0x80) or (body[0] == 0xFF and body[1] >= 0x80)):
        body = body[1:]
    return body


def integer(n: int, tag: int = 0x02) -> bytes:
    """An INTEGER (``tag`` defaults to UNIVERSAL 2; pass e.g. 0x80 for ``[0] IMPLICIT``)."""
    return tlv(tag, encode_integer_content(n))


def encode_oid_content(oid: str | tuple[int, ...]) -> bytes:
    """Content octets of an OBJECT IDENTIFIER given as ``"1.0.9506.2.3"`` or a tuple."""
    arcs = tuple(int(a) for a in oid.split(".")) if isinstance(oid, str) else tuple(oid)
    if len(arcs) < 2 or arcs[0] > 2 or (arcs[0] < 2 and arcs[1] >= 40):
        raise ValueError(f"invalid object identifier {oid!r}")
    out = bytearray()
    for i, arc in enumerate((arcs[0] * 40 + arcs[1], *arcs[2:])):
        if arc < 0:
            raise ValueError(f"invalid object identifier {oid!r} (arc {i})")
        chunk = [arc & 0x7F]
        arc >>= 7
        while arc:
            chunk.append(0x80 | (arc & 0x7F))
            arc >>= 7
        out.extend(reversed(chunk))
    return bytes(out)


def oid(value: str | tuple[int, ...], tag: int = 0x06) -> bytes:
    """An OBJECT IDENTIFIER."""
    return tlv(tag, encode_oid_content(value))


def bit_string(data: bytes, unused_bits: int = 0, tag: int = 0x03) -> bytes:
    """A BIT STRING from its octets (first bit = most significant bit of the first octet)."""
    if not 0 <= unused_bits <= 7 or (unused_bits and not data):
        raise ValueError("invalid number of unused bits")
    return tlv(tag, bytes([unused_bits]) + data)


# ---------------------------------------------------------------------------
# Decoding
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Tlv:
    """One decoded BER element. ``value`` is the content octets; ``raw`` the whole encoding."""

    cls: int
    constructed: bool
    number: int
    value: bytes
    raw: bytes

    @property
    def tag(self) -> int:
        """The first identifier octet (handy for comparing low tag numbers, e.g. ``0xA2``)."""
        return self.raw[0]

    def is_(self, cls: int, number: int) -> bool:
        return self.cls == cls and self.number == number

    def children(self) -> list[Tlv]:
        if not self.constructed:
            raise BerError(f"{self.describe()} is primitive, not constructed")
        return decode_all(self.value)

    def child(self, cls: int, number: int) -> Tlv | None:
        """First child with the given class and tag number, or None."""
        for c in self.children():
            if c.is_(cls, number):
                return c
        return None

    def as_int(self) -> int:
        return decode_integer(self.value)

    def as_oid(self) -> str:
        return decode_oid(self.value)

    def describe(self) -> str:
        return f"[{_CLASS_NAMES[self.cls]} {self.number}]{' constructed' if self.constructed else ''}"


def decode_one(data: bytes, offset: int = 0) -> tuple[Tlv, int]:
    """Decode the element at ``offset``; return it and the offset just after it."""
    start = offset
    if offset >= len(data):
        raise BerError("unexpected end of data (identifier)")
    first = data[offset]
    offset += 1
    cls = first & 0xC0
    constructed = bool(first & CONSTRUCTED)
    number = first & 0x1F
    if number == 0x1F:
        number = 0
        while True:
            if offset >= len(data):
                raise BerError("unexpected end of data (long tag)")
            b = data[offset]
            offset += 1
            number = (number << 7) | (b & 0x7F)
            if not b & 0x80:
                break
            if number > 0xFFFFFF:
                raise BerError("tag number too large")
    if offset >= len(data):
        raise BerError("unexpected end of data (length)")
    lb = data[offset]
    offset += 1
    if lb == 0x80:
        raise BerError("indefinite length is not supported")
    if lb & 0x80:
        n = lb & 0x7F
        if n > 4 or offset + n > len(data):
            raise BerError("invalid long-form length")
        length = int.from_bytes(data[offset : offset + n], "big")
        offset += n
    else:
        length = lb
    end = offset + length
    if end > len(data):
        raise BerError(f"length {length} exceeds the {len(data) - offset} remaining octets")
    return Tlv(cls, constructed, number, bytes(data[offset:end]), bytes(data[start:end])), end


def decode_all(data: bytes) -> list[Tlv]:
    """Decode a concatenation of elements (e.g. the contents of a SEQUENCE)."""
    return list(iter_tlvs(data))


def iter_tlvs(data: bytes) -> Iterator[Tlv]:
    offset = 0
    while offset < len(data):
        t, offset = decode_one(data, offset)
        yield t


def decode_integer(content: bytes, *, signed: bool = True) -> int:
    if not content:
        raise BerError("empty INTEGER")
    return int.from_bytes(content, "big", signed=signed)


def decode_oid(content: bytes) -> str:
    if not content:
        raise BerError("empty OBJECT IDENTIFIER")
    arcs: list[int] = []
    value = 0
    for i, b in enumerate(content):
        value = (value << 7) | (b & 0x7F)
        if not b & 0x80:
            arcs.append(value)
            value = 0
        elif i == len(content) - 1:
            raise BerError("truncated OBJECT IDENTIFIER")
    first = arcs[0]
    head = [min(first // 40, 2), first - 40 * min(first // 40, 2)]
    return ".".join(str(a) for a in head + arcs[1:])


def decode_bit_string(content: bytes) -> tuple[bytes, int]:
    """Return (octets, number of bits)."""
    if not content:
        raise BerError("empty BIT STRING")
    unused = content[0]
    if unused > 7 or (unused and len(content) == 1):
        raise BerError("invalid unused-bits octet in BIT STRING")
    data = content[1:]
    return data, len(data) * 8 - unused


def bits_set(data: bytes, nbits: int) -> list[int]:
    """Bit positions (0 = first bit on the wire) that are set in a BIT STRING."""
    return [i for i in range(nbits) if data[i // 8] & (0x80 >> (i % 8))]
