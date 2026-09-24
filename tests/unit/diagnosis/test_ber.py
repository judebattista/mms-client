"""The small BER codec used by the association probes."""

from __future__ import annotations

import pytest

from mms_client.diagnosis import ber
from mms_client.diagnosis.ber import APPLICATION, CONTEXT, UNIVERSAL, BerError


@pytest.mark.parametrize(
    ("n", "hexed"),
    [(0, "00"), (5, "05"), (127, "7f"), (128, "0080"), (255, "00ff"), (256, "0100"), (65000, "00fde8"),
     (-1, "ff"), (-128, "80"), (-129, "ff7f"), (2**31 - 1, "7fffffff")],
)  # fmt: skip
def test_integer_minimal_encoding_and_roundtrip(n, hexed):
    assert ber.encode_integer_content(n).hex() == hexed
    t, end = ber.decode_one(ber.integer(n))
    assert t.is_(UNIVERSAL, 2) and t.as_int() == n and end == len(ber.integer(n))


@pytest.mark.parametrize("n", [0, 1, 127, 128, 255, 256, 65535, 70000])
def test_length_forms(n):
    enc = ber.encode_length(n)
    assert (len(enc) == 1) == (n < 128)
    t, _ = ber.decode_one(ber.tlv(0x04, bytes(n)))
    assert len(t.value) == n


@pytest.mark.parametrize(
    ("oid", "hexed"),
    [("1.0.9506.2.3", "28ca220203"), ("2.2.1.0.1", "52010001"), ("2.1.1", "5101"), ("1.1.1.999.1", "2901876701"),
     ("2.999.3", "883703")],
)  # fmt: skip
def test_oid(oid, hexed):
    assert ber.encode_oid_content(oid).hex() == hexed
    assert ber.decode_oid(bytes.fromhex(hexed)) == oid


def test_invalid_oids_rejected():
    for bad in ("1", "3.1", "1.40.1"):
        with pytest.raises(ValueError):
            ber.encode_oid_content(bad)
    with pytest.raises(BerError):
        ber.decode_oid(bytes.fromhex("2981"))  # truncated arc


def test_bit_string():
    enc = ber.bit_string(bytes.fromhex("f100"), 5, 0x81)
    assert enc.hex() == "810305f100"
    t, _ = ber.decode_one(enc)
    data, n = ber.decode_bit_string(t.value)
    assert (data, n) == (bytes.fromhex("f100"), 11)
    assert ber.bits_set(data, n) == [0, 1, 2, 3, 7]
    with pytest.raises(BerError):
        ber.decode_bit_string(b"\x08\x00")


def test_high_tag_numbers():
    ident = ber.encode_identifier(CONTEXT, True, 79)
    assert ident.hex() == "bf4f"
    t, _ = ber.decode_one(ber.tlv(ident, b""))
    assert t.cls == CONTEXT and t.constructed and t.number == 79
    t, _ = ber.decode_one(ber.tlv(ber.encode_identifier(APPLICATION, False, 300), b"\x01"))
    assert t.number == 300 and t.value == b"\x01"


def test_constructed_children_and_lookup():
    seq = ber.tlv(0x30, ber.integer(1) + ber.tlv(0xA0, ber.integer(7, 0x80)) + ber.oid("2.1.1"))
    t, _ = ber.decode_one(seq)
    kids = t.children()
    assert [k.describe() for k in kids] == ["[universal 2]", "[context 0] constructed", "[universal 6]"]
    assert t.child(CONTEXT, 0).children()[0].as_int() == 7
    assert t.child(CONTEXT, 5) is None
    with pytest.raises(BerError):
        kids[0].children()


@pytest.mark.parametrize(
    "bad",
    ["", "30", "3005020101", "3080", "0285ffffffffff", "1f", "1f81", "02"],
)
def test_malformed_input_raises_ber_error(bad):
    with pytest.raises(BerError):
        ber.decode_all(bytes.fromhex(bad)) if bad else ber.decode_one(b"")
