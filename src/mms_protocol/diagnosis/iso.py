"""Builders and parsers for the ISO stack below MMS (no sockets here).

Layers, bottom up, as used by IEC 61850-8-1 over TCP:

* TPKT (RFC 1006) framing on TCP port 102;
* COTP (ISO 8073 class 0): CR / CC / DR / ER / DT;
* ISO session (ISO 8327): CONNECT / ACCEPT / REFUSE / ABORT / FINISH / DISCONNECT / DATA TRANSFER;
* ISO presentation (ISO 8823): CP-type / CPA / CPR / ARU / ARP and fully-encoded user data;
* ACSE (ISO 8650): AARQ / AARE / RLRQ / RLRE / ABRT;
* MMS (ISO 9506): initiate-Request / -Response / -Error and conclude.

The requests mirror what libiec61850 1.6.1 sends by default (transport, session and presentation
selectors 0001 / 0001 / 00000001, calling AP title 1.1.1.999 with AE qualifier 12, called AP title
1.1.1.999.1 with AE qualifier 12, max PDU 65000, 5/5 outstanding services, nesting level 10), so a
probe exercises the same path as the real client. Byte-for-byte equality with libiec61850's
requests is checked by the unit tests against a capture of its traffic.

The PDU structure follows the published ASN.1 of the standards; the tables of reason codes give
our own short names for the numeric values (the numbers are what the peer sent and are always
reported alongside, CLI-7).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..codes import ACSE_USER_DIAGNOSTICS
from . import ber
from .ber import APPLICATION, CONTEXT, UNIVERSAL, BerError, Tlv

# ---------------------------------------------------------------------------
# Defaults (libiec61850 1.6.1 client)
# ---------------------------------------------------------------------------
ACSE_ABSTRACT_SYNTAX = "2.2.1.0.1"
BER_TRANSFER_SYNTAX = "2.1.1"
MMS_ABSTRACT_SYNTAX = "1.0.9506.2.1"
MMS_APPLICATION_CONTEXT = "1.0.9506.2.3"
ACSE_CONTEXT_ID = 1
MMS_CONTEXT_ID = 3

DEFAULT_CALLING_AP_TITLE = "1.1.1.999"
DEFAULT_CALLED_AP_TITLE = "1.1.1.999.1"
DEFAULT_AE_QUALIFIER = 12

# libiec61850's proposed ParameterSupportOptions (str1 str2 vnam valt vlis) and
# ServiceSupportOptions for the calling side, as captured from its initiate-RequestPDU.
DEFAULT_PARAMETER_CBB = (bytes.fromhex("f100"), 11)
DEFAULT_SERVICES_CALLING = (bytes.fromhex("ee1c00000408000079ef18"), 85)


class IsoError(ValueError):
    """A received PDU could not be understood at some layer."""

    def __init__(self, layer: str, message: str) -> None:
        self.layer = layer
        super().__init__(f"{layer}: {message}")


# ---------------------------------------------------------------------------
# TPKT
# ---------------------------------------------------------------------------
TPKT_VERSION = 3
TPKT_HEADER = 4
TPKT_MAX = 0xFFFF


def tpkt(payload: bytes) -> bytes:
    """Wrap a COTP TPDU in a TPKT header (RFC 1006)."""
    n = len(payload) + TPKT_HEADER
    if n > TPKT_MAX:
        raise ValueError("TPKT payload too large")
    return bytes([TPKT_VERSION, 0, n >> 8, n & 0xFF]) + payload


def tpkt_length(header: bytes) -> int:
    """Total packet length announced by a 4-byte TPKT header (raises IsoError if invalid)."""
    if len(header) < TPKT_HEADER:
        raise IsoError("tpkt", "short header")
    if header[0] != TPKT_VERSION:
        raise IsoError("tpkt", f"version {header[0]} (expected 3); first bytes {header[:4].hex(' ')}")
    n = (header[2] << 8) | header[3]
    if n < TPKT_HEADER + 2:
        raise IsoError("tpkt", f"invalid length {n}")
    return n


# ---------------------------------------------------------------------------
# COTP (ISO 8073 / X.224 class 0)
# ---------------------------------------------------------------------------
COTP_CR = 0xE0
COTP_CC = 0xD0
COTP_DR = 0x80
COTP_DC = 0xC0
COTP_DT = 0xF0
COTP_ER = 0x70

COTP_TYPE_NAMES: dict[int, str] = {
    COTP_CR: "CR",
    COTP_CC: "CC",
    COTP_DR: "DR",
    COTP_DC: "DC",
    COTP_DT: "DT",
    COTP_ER: "ER",
    0x10: "ED",
    0x20: "EA",
    0x50: "RJ",
    0x60: "AK",
}

COTP_PARAM_TPDU_SIZE = 0xC0
COTP_PARAM_CALLING_TSEL = 0xC1
COTP_PARAM_CALLED_TSEL = 0xC2
COTP_PARAM_ADDITIONAL_INFO = 0xE0
COTP_PARAM_INVALID_TPDU = 0xC1  # in ER TPDUs

COTP_DR_REASONS: dict[int, str] = {
    0x00: "reason-not-specified",
    0x01: "congestion-at-tsap",
    0x02: "session-entity-not-attached-to-tsap",
    0x03: "address-unknown",
    0x80: "normal-disconnect",
    0x81: "remote-transport-entity-congestion",
    0x82: "connection-negotiation-failed",
    0x83: "duplicate-source-reference",
    0x84: "mismatched-references",
    0x85: "protocol-error",
    0x87: "reference-overflow",
    0x88: "connection-request-refused",
    0x8A: "header-or-parameter-length-invalid",
}
COTP_ER_CAUSES: dict[int, str] = {
    0: "reason-not-specified",
    1: "invalid-parameter-code",
    2: "invalid-tpdu-type",
    3: "invalid-parameter-value",
}
# DR reasons that mean "busy right now" rather than "never".
COTP_CONGESTION_REASONS = frozenset({0x01, 0x81})


def tpdu_size_bytes(code: int) -> int:
    """TPDU size parameter value (7..13) to octets (128..8192)."""
    return 1 << code


def cotp_cr(
    *,
    src_ref: int = 1,
    tpdu_size_code: int = 0x0D,
    calling_tsel: bytes = b"\x00\x01",
    called_tsel: bytes = b"\x00\x01",
) -> bytes:
    """A class-0 connection request TPDU (the parameter order is libiec61850's)."""
    var = bytes([COTP_PARAM_TPDU_SIZE, 1, tpdu_size_code])
    if called_tsel:
        var += bytes([COTP_PARAM_CALLED_TSEL, len(called_tsel)]) + called_tsel
    if calling_tsel:
        var += bytes([COTP_PARAM_CALLING_TSEL, len(calling_tsel)]) + calling_tsel
    fixed = bytes([COTP_CR, 0, 0, src_ref >> 8, src_ref & 0xFF, 0x00])
    return bytes([len(fixed) + len(var)]) + fixed + var


def cotp_dt(payload: bytes, *, eot: bool = True) -> bytes:
    return bytes([2, COTP_DT, 0x80 if eot else 0x00]) + payload


def cotp_dr(dst_ref: int, src_ref: int, reason: int = 0x80) -> bytes:
    fixed = bytes([COTP_DR, dst_ref >> 8, dst_ref & 0xFF, src_ref >> 8, src_ref & 0xFF, reason])
    return bytes([len(fixed)]) + fixed


@dataclass(frozen=True)
class CotpPdu:
    """A decoded COTP TPDU."""

    code: int
    kind: str
    dst_ref: int | None = None
    src_ref: int | None = None
    class_option: int | None = None
    reason: int | None = None  # DR reason or ER reject cause
    params: dict[int, bytes] = field(default_factory=dict)
    user_data: bytes = b""
    eot: bool = True

    @property
    def reason_name(self) -> str | None:
        if self.reason is None:
            return None
        table = COTP_ER_CAUSES if self.code == COTP_ER else COTP_DR_REASONS
        return table.get(self.reason, f"unknown-{self.reason}")

    @property
    def tpdu_size(self) -> int | None:
        v = self.params.get(COTP_PARAM_TPDU_SIZE)
        return tpdu_size_bytes(v[0]) if v and 7 <= v[0] <= 13 else None

    def to_json(self) -> dict[str, Any]:
        d: dict[str, Any] = {"type": self.kind, "code": f"0x{self.code:02x}"}
        for k in ("dst_ref", "src_ref", "class_option"):
            if getattr(self, k) is not None:
                d[k] = getattr(self, k)
        if self.reason is not None:
            d["reason"] = self.reason
            d["reason_name"] = self.reason_name
        if self.params:
            d["params"] = {f"0x{k:02x}": v.hex() for k, v in self.params.items()}
        if self.tpdu_size:
            d["tpdu_size"] = self.tpdu_size
        return d


def _cotp_params(data: bytes) -> dict[int, bytes]:
    out: dict[int, bytes] = {}
    i = 0
    while i < len(data):
        if i + 2 > len(data):
            raise IsoError("cotp", "truncated parameter")
        code, n = data[i], data[i + 1]
        if i + 2 + n > len(data):
            raise IsoError("cotp", f"parameter 0x{code:02x} overruns the header")
        out[code] = data[i + 2 : i + 2 + n]
        i += 2 + n
    return out


def parse_cotp(tpdu: bytes) -> CotpPdu:
    """Decode one COTP TPDU (the TPKT payload)."""
    if len(tpdu) < 2:
        raise IsoError("cotp", "TPDU too short")
    li = tpdu[0]
    if li == 0 or li + 1 > len(tpdu) or li == 0xFF:
        raise IsoError("cotp", f"invalid length indicator {li} for a {len(tpdu)}-byte TPDU")
    header = tpdu[1 : li + 1]
    data = tpdu[li + 1 :]
    code = header[0] & 0xF0
    kind = COTP_TYPE_NAMES.get(code, f"unknown-0x{code:02x}")
    if code in (COTP_CR, COTP_CC):
        if len(header) < 6:
            raise IsoError("cotp", f"{kind} header too short")
        return CotpPdu(
            code,
            kind,
            dst_ref=int.from_bytes(header[1:3], "big"),
            src_ref=int.from_bytes(header[3:5], "big"),
            class_option=header[5],
            params=_cotp_params(header[6:]),
            user_data=data,
        )
    if code == COTP_DR:
        if len(header) < 6:
            raise IsoError("cotp", "DR header too short")
        return CotpPdu(
            code,
            kind,
            dst_ref=int.from_bytes(header[1:3], "big"),
            src_ref=int.from_bytes(header[3:5], "big"),
            reason=header[5],
            params=_cotp_params(header[6:]),
            user_data=data,
        )
    if code == COTP_ER:
        if len(header) < 4:
            raise IsoError("cotp", "ER header too short")
        return CotpPdu(
            code,
            kind,
            dst_ref=int.from_bytes(header[1:3], "big"),
            reason=header[3],
            params=_cotp_params(header[4:]),
        )
    if code == COTP_DT:
        if len(header) < 2:
            raise IsoError("cotp", "DT header too short")
        return CotpPdu(code, kind, eot=bool(header[1] & 0x80), user_data=data)
    if code == COTP_DC:
        return CotpPdu(code, kind, dst_ref=int.from_bytes(header[1:3], "big") if len(header) >= 3 else None)
    return CotpPdu(code, kind, user_data=data)


# ---------------------------------------------------------------------------
# ISO session (ISO 8327-1)
# ---------------------------------------------------------------------------
SPDU_DT = 1  # DATA TRANSFER (also GIVE TOKENS, same SI)
SPDU_NF = 8
SPDU_FN = 9
SPDU_DN = 10
SPDU_RF = 12
SPDU_CN = 13
SPDU_AC = 14
SPDU_AB = 25
SPDU_AA = 26

SPDU_NAMES: dict[int, str] = {
    SPDU_DT: "DATA-TRANSFER",
    SPDU_NF: "NOT-FINISHED",
    SPDU_FN: "FINISH",
    SPDU_DN: "DISCONNECT",
    SPDU_RF: "REFUSE",
    SPDU_CN: "CONNECT",
    SPDU_AC: "ACCEPT",
    SPDU_AB: "ABORT",
    SPDU_AA: "ABORT-ACCEPT",
}

PI_CONNECT_ACCEPT_ITEM = 5
PI_PROTOCOL_OPTIONS = 19
PI_VERSION_NUMBER = 22
PI_TRANSPORT_DISCONNECT = 17
PI_SESSION_REQUIREMENTS = 20
PI_CALLING_SSEL = 51
PI_CALLED_SSEL = 52  # also "responding session selector" in AC
PI_REASON_CODE = 50
PGI_USER_DATA = 193
PGI_EXTENDED_USER_DATA = 194

SESSION_REFUSE_REASONS: dict[int, str] = {
    0: "rejected-by-user-no-reason",
    1: "rejected-by-user-temporary-congestion",
    2: "rejected-by-user",
    0x81: "session-selector-unknown",
    0x82: "user-not-attached-to-ssap",
    0x83: "spm-congestion-at-connect-time",
    0x84: "protocol-version-not-supported",
    0x85: "rejected-by-spm-no-reason",
    0x86: "rejected-by-spm-implementation-restriction",
}
SESSION_CONGESTION_REASONS = frozenset({1, 0x83})


def _session_param(code: int, value: bytes) -> bytes:
    n = len(value)
    if n < 0xFF:
        return bytes([code, n]) + value
    return bytes([code, 0xFF, n >> 8, n & 0xFF]) + value


def _spdu(si: int, params: bytes) -> bytes:
    n = len(params)
    if n < 0xFF:
        return bytes([si, n]) + params
    return bytes([si, 0xFF, n >> 8, n & 0xFF]) + params


def session_connect(
    user_data: bytes, *, calling_ssel: bytes = b"\x00\x01", called_ssel: bytes = b"\x00\x01"
) -> bytes:
    """CONNECT SPDU: protocol options 0, version 2, duplex functional unit, selectors, user data."""
    params = _session_param(
        PI_CONNECT_ACCEPT_ITEM,
        _session_param(PI_PROTOCOL_OPTIONS, b"\x00") + _session_param(PI_VERSION_NUMBER, b"\x02"),
    )
    params += _session_param(PI_SESSION_REQUIREMENTS, b"\x00\x02")
    if calling_ssel:
        params += _session_param(PI_CALLING_SSEL, calling_ssel)
    if called_ssel:
        params += _session_param(PI_CALLED_SSEL, called_ssel)
    params += _session_param(PGI_USER_DATA, user_data)
    return _spdu(SPDU_CN, params)


def session_data(user_data: bytes) -> bytes:
    """GIVE-TOKENS + DATA-TRANSFER SPDUs (both empty) followed by the user information."""
    return bytes([SPDU_DT, 0, SPDU_DT, 0]) + user_data


def session_finish(user_data: bytes) -> bytes:
    return _spdu(SPDU_FN, _session_param(PGI_USER_DATA, user_data))


def session_abort(user_data: bytes) -> bytes:
    """ABORT SPDU with transport-disconnect 0x0b (release transport, user abort), as libiec61850."""
    return _spdu(
        SPDU_AB, _session_param(PI_TRANSPORT_DISCONNECT, b"\x0b") + _session_param(PGI_USER_DATA, user_data)
    )


@dataclass(frozen=True)
class Spdu:
    """A decoded SPDU. ``params`` are the top-level (code, value) pairs in wire order."""

    si: int
    params: tuple[tuple[int, bytes], ...] = ()
    user_data: bytes = b""

    @property
    def name(self) -> str:
        return SPDU_NAMES.get(self.si, f"unknown-{self.si}")

    def param(self, code: int) -> bytes | None:
        for c, v in self.params:
            if c == code:
                return v
        return None

    @property
    def refuse_reason(self) -> int | None:
        v = self.param(PI_REASON_CODE)
        return v[0] if (self.si == SPDU_RF and v) else None

    @property
    def refuse_reason_name(self) -> str | None:
        r = self.refuse_reason
        return None if r is None else SESSION_REFUSE_REASONS.get(r, f"unknown-{r}")

    @property
    def transport_disconnect(self) -> int | None:
        v = self.param(PI_TRANSPORT_DISCONNECT)
        return v[0] if v else None

    @property
    def version(self) -> int | None:
        item = self.param(PI_CONNECT_ACCEPT_ITEM)
        if item is None:
            return None
        for c, v in _session_params(item):
            if c == PI_VERSION_NUMBER and v:
                return v[0]
        return None

    def to_json(self) -> dict[str, Any]:
        d: dict[str, Any] = {"si": self.si, "type": self.name}
        d["params"] = [{"code": c, "value": v.hex()} for c, v in self.params if c not in (PGI_USER_DATA,)]
        if self.refuse_reason is not None:
            d["refuse_reason"] = self.refuse_reason
            d["refuse_reason_name"] = self.refuse_reason_name
        if self.transport_disconnect is not None:
            d["transport_disconnect"] = f"0x{self.transport_disconnect:02x}"
        if self.version is not None:
            d["version"] = self.version
        d["user_data_len"] = len(self.user_data)
        return d


def _session_params(data: bytes) -> list[tuple[int, bytes]]:
    out: list[tuple[int, bytes]] = []
    i = 0
    while i < len(data):
        if i + 2 > len(data):
            raise IsoError("session", "truncated parameter")
        code, n = data[i], data[i + 1]
        i += 2
        if n == 0xFF:
            if i + 2 > len(data):
                raise IsoError("session", "truncated extended length")
            n = (data[i] << 8) | data[i + 1]
            i += 2
        if i + n > len(data):
            raise IsoError("session", f"parameter {code} overruns the SPDU")
        out.append((code, data[i : i + n]))
        i += n
    return out


def _spdu_header(data: bytes, offset: int) -> tuple[int, int, int]:
    """(si, length, offset of the parameters)."""
    if offset + 2 > len(data):
        raise IsoError("session", "SPDU too short")
    si, n = data[offset], data[offset + 1]
    offset += 2
    if n == 0xFF:
        if offset + 2 > len(data):
            raise IsoError("session", "truncated SPDU length")
        n = (data[offset] << 8) | data[offset + 1]
        offset += 2
    if offset + n > len(data):
        raise IsoError("session", f"SPDU length {n} exceeds the {len(data) - offset} octets received")
    return si, n, offset


def parse_spdu(data: bytes) -> Spdu:
    """Decode the SPDU(s) of one session message and extract the user information."""
    si, n, off = _spdu_header(data, 0)
    if si == SPDU_DT:
        # GIVE TOKENS (category 0) followed by DATA TRANSFER; user information follows the header.
        if n == 0 and off < len(data) and data[off] == SPDU_DT:
            si2, n2, off2 = _spdu_header(data, off)
            params = tuple(_session_params(data[off2 : off2 + n2]))
            return Spdu(SPDU_DT, params, data[off2 + n2 :])
        params = tuple(_session_params(data[off : off + n]))
        return Spdu(SPDU_DT, params, data[off + n :])
    params = tuple(_session_params(data[off : off + n]))
    spdu = Spdu(si, params)
    user = spdu.param(PGI_USER_DATA)
    if user is None:
        user = spdu.param(PGI_EXTENDED_USER_DATA)
    if si == SPDU_RF:
        reason = spdu.param(PI_REASON_CODE)
        if reason and len(reason) > 1:
            user = reason[1:]
    return Spdu(si, params, user or b"")


# ---------------------------------------------------------------------------
# Presentation (ISO 8823-1)
# ---------------------------------------------------------------------------
PRESENTATION_PROVIDER_REASONS: dict[int, str] = {
    0: "reason-not-specified",
    1: "temporary-congestion",
    2: "local-limit-exceeded",
    3: "called-presentation-address-unknown",
    4: "protocol-version-not-supported",
    5: "default-context-not-supported",
    6: "user-data-not-readable",
    7: "no-psap-available",
}
PRESENTATION_CONGESTION_REASONS = frozenset({1, 2, 7})
PRESENTATION_CONTEXT_RESULTS: dict[int, str] = {0: "acceptance", 1: "user-rejection", 2: "provider-rejection"}
PRESENTATION_ABORT_REASONS: dict[int, str] = {
    0: "reason-not-specified",
    1: "unrecognized-ppdu",
    2: "unexpected-ppdu",
    3: "unexpected-session-service-primitive",
    4: "unrecognized-ppdu-parameter",
    5: "unexpected-ppdu-parameter",
    6: "invalid-ppdu-parameter-value",
}


def presentation_user_data(context_id: int, payload: bytes) -> bytes:
    """Fully-encoded-data with one PDV-list carrying ``payload`` as single-ASN1-type."""
    pdv = ber.integer(context_id) + ber.tlv(0xA0, payload)
    return ber.tlv(0x61, ber.tlv(0x30, pdv))


def presentation_cp(
    acse: bytes,
    *,
    calling_psel: bytes = b"\x00\x00\x00\x01",
    called_psel: bytes = b"\x00\x00\x00\x01",
) -> bytes:
    """CP-type PPDU (normal mode) proposing the ACSE and MMS contexts with BER transfer syntax."""
    ts = ber.tlv(0x30, ber.oid(BER_TRANSFER_SYNTAX))
    ctx_list = ber.tlv(0x30, ber.integer(ACSE_CONTEXT_ID) + ber.oid(ACSE_ABSTRACT_SYNTAX) + ts)
    ctx_list += ber.tlv(0x30, ber.integer(MMS_CONTEXT_ID) + ber.oid(MMS_ABSTRACT_SYNTAX) + ts)
    normal = b""
    if calling_psel:
        normal += ber.tlv(0x81, calling_psel)
    if called_psel:
        normal += ber.tlv(0x82, called_psel)
    normal += ber.tlv(0xA4, ctx_list)
    normal += presentation_user_data(ACSE_CONTEXT_ID, acse)
    mode = ber.tlv(0xA0, ber.tlv(0x80, b"\x01"))
    return ber.tlv(0x31, mode + ber.tlv(0xA2, normal))


def presentation_aru(acse: bytes, context_id: int = ACSE_CONTEXT_ID) -> bytes:
    """ARU-PPDU (normal mode) carrying an ACSE APDU, as libiec61850 sends it in an ABORT SPDU."""
    return ber.tlv(0xA0, presentation_user_data(context_id, acse))


@dataclass(frozen=True)
class PresentationResponse:
    """A decoded CPA / CPR (or abort) PPDU."""

    kind: str  # "CPA" | "CPR" | "ARU" | "ARP"
    responding_psel: bytes | None = None
    context_results: tuple[tuple[int, int | None], ...] = ()  # (result, provider-reason)
    provider_reason: int | None = None
    abort_reason: int | None = None
    user_data: tuple[tuple[int | None, bytes], ...] = ()  # (context id, APDU)

    @property
    def provider_reason_name(self) -> str | None:
        r = self.provider_reason
        return None if r is None else PRESENTATION_PROVIDER_REASONS.get(r, f"unknown-{r}")

    def apdu(self, context_id: int | None = ACSE_CONTEXT_ID) -> bytes | None:
        """The user data for a context (the first one if the context id is absent)."""
        for ctx, data in self.user_data:
            if ctx == context_id or ctx is None:
                return data
        return self.user_data[0][1] if self.user_data else None

    def to_json(self) -> dict[str, Any]:
        d: dict[str, Any] = {"type": self.kind}
        if self.responding_psel is not None:
            d["responding_psel"] = self.responding_psel.hex()
        if self.context_results:
            d["context_results"] = [
                {
                    "result": r,
                    "result_name": PRESENTATION_CONTEXT_RESULTS.get(r, f"unknown-{r}"),
                    "provider_reason": p,
                }
                for r, p in self.context_results
            ]
        if self.provider_reason is not None:
            d["provider_reason"] = self.provider_reason
            d["provider_reason_name"] = self.provider_reason_name
        if self.abort_reason is not None:
            d["abort_reason"] = self.abort_reason
            d["abort_reason_name"] = PRESENTATION_ABORT_REASONS.get(
                self.abort_reason, f"unknown-{self.abort_reason}"
            )
        return d


def parse_user_data(t: Tlv) -> tuple[tuple[int | None, bytes], ...]:
    """User-data: fully-encoded-data [APPLICATION 1] or simply-encoded-data [APPLICATION 0]."""
    if t.is_(APPLICATION, 0):  # simply-encoded-data: OCTET STRING content is the APDU itself
        return ((None, t.value),)
    if not t.is_(APPLICATION, 1):
        raise IsoError("presentation", f"unexpected user-data element {t.describe()}")
    out: list[tuple[int | None, bytes]] = []
    for pdv in t.children():
        if not pdv.is_(UNIVERSAL, 16):
            raise IsoError("presentation", f"unexpected PDV-list element {pdv.describe()}")
        ctx: int | None = None
        data: bytes | None = None
        for f in pdv.children():
            if f.is_(UNIVERSAL, 2):
                ctx = f.as_int()
            elif f.cls == CONTEXT and f.number in (0, 1, 2):  # single-ASN1-type / octet-aligned / arbitrary
                data = f.value
        if data is None:
            raise IsoError("presentation", "PDV without presentation-data-values")
        out.append((ctx, data))
    return tuple(out)


def _context_results(t: Tlv) -> tuple[tuple[int, int | None], ...]:
    out = []
    for r in t.children():
        result = r.child(CONTEXT, 0)
        reason = r.child(CONTEXT, 2)
        out.append((result.as_int() if result else -1, reason.as_int() if reason else None))
    return tuple(out)


def parse_presentation(data: bytes, *, abort: bool = False) -> PresentationResponse:
    """Decode the PPDU carried in an ACCEPT / REFUSE SPDU, or in an ABORT SPDU when ``abort``.

    The SPDU type decides which PPDUs are possible: a SEQUENCE is a CPR-PPDU in a REFUSE but an
    ARP-PPDU in an ABORT.
    """
    try:
        top, _ = ber.decode_one(data)
        if abort:
            if top.is_(CONTEXT, 0) and top.constructed:  # ARU-PPDU, normal mode
                user: tuple[tuple[int | None, bytes], ...] = ()
                for f in top.children():
                    if f.cls == APPLICATION:
                        user = parse_user_data(f)
                return PresentationResponse("ARU", user_data=user)
            if top.is_(UNIVERSAL, 16):  # ARP-PPDU
                reason = next((f.as_int() for f in top.children() if f.is_(CONTEXT, 0)), None)
                return PresentationResponse("ARP", abort_reason=reason)
            raise IsoError("presentation", f"unexpected abort PPDU {top.describe()}")
        if top.is_(UNIVERSAL, 17):  # SET: CPA-PPDU
            normal = top.child(CONTEXT, 2)
            if normal is None:
                raise IsoError(
                    "presentation", "CPA without normal-mode-parameters (X.410 mode is not supported)"
                )
            kind, fields = "CPA", normal.children()
        elif top.is_(UNIVERSAL, 16):  # SEQUENCE: CPR-PPDU, normal mode
            kind, fields = "CPR", top.children()
        else:
            raise IsoError("presentation", f"unexpected PPDU {top.describe()}")
        psel = None
        results: tuple[tuple[int, int | None], ...] = ()
        user = ()
        reason = None
        for f in fields:
            if f.is_(CONTEXT, 3):
                psel = f.value
            elif f.is_(CONTEXT, 5):
                results = _context_results(f)
            elif f.is_(CONTEXT, 10) and kind == "CPR":
                reason = f.as_int()
            elif f.cls == APPLICATION:
                user = parse_user_data(f)
        return PresentationResponse(kind, psel, results, provider_reason=reason, user_data=user)
    except BerError as e:
        raise IsoError("presentation", str(e)) from e


def parse_presentation_data(data: bytes) -> tuple[tuple[int | None, bytes], ...]:
    """User data of a P-DATA / release PPDU (fully-encoded-data only)."""
    try:
        top, _ = ber.decode_one(data)
        return parse_user_data(top)
    except BerError as e:
        raise IsoError("presentation", str(e)) from e


# ---------------------------------------------------------------------------
# ACSE (ISO 8650-1)
# ---------------------------------------------------------------------------
ACSE_RESULTS: dict[int, str] = {0: "accepted", 1: "rejected-permanent", 2: "rejected-transient"}
ACSE_PROVIDER_DIAGNOSTICS: dict[int, str] = {0: "null", 1: "no-reason-given", 2: "no-common-acse-version"}
ACSE_ABORT_SOURCES: dict[int, str] = {0: "acse-service-user", 1: "acse-service-provider"}
ACSE_ABORT_DIAGNOSTICS: dict[int, str] = {
    1: "no-reason-given",
    2: "protocol-error",
    3: "authentication-mechanism-name-not-recognized",
    4: "authentication-mechanism-name-required",
    5: "authentication-failure",
    6: "authentication-required",
}
ACSE_RELEASE_REASONS: dict[int, str] = {0: "normal", 1: "urgent", 30: "user-defined"}


def acse_aarq(
    user_information: bytes,
    *,
    calling_ap_title: str | None = DEFAULT_CALLING_AP_TITLE,
    calling_ae_qualifier: int | None = DEFAULT_AE_QUALIFIER,
    called_ap_title: str | None = DEFAULT_CALLED_AP_TITLE,
    called_ae_qualifier: int | None = DEFAULT_AE_QUALIFIER,
    context_id: int = MMS_CONTEXT_ID,
) -> bytes:
    """AARQ-apdu carrying an MMS PDU in user-information (no authentication: v1 has none)."""
    body = ber.tlv(0xA1, ber.oid(MMS_APPLICATION_CONTEXT))
    if called_ap_title:
        body += ber.tlv(0xA2, ber.oid(called_ap_title))
    if called_ae_qualifier is not None:
        body += ber.tlv(0xA3, ber.integer(called_ae_qualifier))
    if calling_ap_title:
        body += ber.tlv(0xA6, ber.oid(calling_ap_title))
    if calling_ae_qualifier is not None:
        body += ber.tlv(0xA7, ber.integer(calling_ae_qualifier))
    external = ber.integer(context_id) + ber.tlv(0xA0, user_information)
    body += ber.tlv(0xBE, ber.tlv(0x28, external))
    return ber.tlv(0x60, body)


def acse_rlrq(reason: int = 0) -> bytes:
    return ber.tlv(0x62, ber.integer(reason, 0x80))


def acse_abrt(source: int = 0) -> bytes:
    return ber.tlv(0x64, ber.integer(source, 0x80))


@dataclass(frozen=True)
class AcsePdu:
    """A decoded ACSE APDU (AARE, RLRE, ABRT, or the request types when parsing captures)."""

    kind: str  # "AARQ" | "AARE" | "RLRQ" | "RLRE" | "ABRT"
    application_context: str | None = None
    result: int | None = None
    diagnostic_source: str | None = None  # "service-user" | "service-provider"
    diagnostic: int | None = None
    responding_ap_title: str | None = None
    responding_ae_qualifier: int | None = None
    abort_source: int | None = None
    abort_diagnostic: int | None = None
    release_reason: int | None = None
    mechanism_name: str | None = None
    user_information: bytes | None = None

    @property
    def result_name(self) -> str | None:
        return None if self.result is None else ACSE_RESULTS.get(self.result, f"unknown-{self.result}")

    @property
    def diagnostic_name(self) -> str | None:
        if self.diagnostic is None:
            return None
        if self.diagnostic_source == "service-provider":
            return ACSE_PROVIDER_DIAGNOSTICS.get(self.diagnostic, f"unknown-{self.diagnostic}")
        return ACSE_USER_DIAGNOSTICS.get(self.diagnostic, f"unknown-{self.diagnostic}")

    @property
    def abort_diagnostic_name(self) -> str | None:
        d = self.abort_diagnostic
        return None if d is None else ACSE_ABORT_DIAGNOSTICS.get(d, f"unknown-{d}")

    def to_json(self) -> dict[str, Any]:
        d: dict[str, Any] = {"type": self.kind}
        if self.application_context:
            d["application_context"] = self.application_context
        if self.result is not None:
            d["result"] = self.result
            d["result_name"] = self.result_name
        if self.diagnostic is not None:
            d["diagnostic_source"] = self.diagnostic_source
            d["diagnostic"] = self.diagnostic
            d["diagnostic_name"] = self.diagnostic_name
        if self.responding_ap_title:
            d["responding_ap_title"] = self.responding_ap_title
        if self.responding_ae_qualifier is not None:
            d["responding_ae_qualifier"] = self.responding_ae_qualifier
        if self.abort_source is not None:
            d["abort_source"] = self.abort_source
            d["abort_source_name"] = ACSE_ABORT_SOURCES.get(self.abort_source, f"unknown-{self.abort_source}")
        if self.abort_diagnostic is not None:
            d["abort_diagnostic"] = self.abort_diagnostic
            d["abort_diagnostic_name"] = self.abort_diagnostic_name
        if self.release_reason is not None:
            d["release_reason"] = self.release_reason
        if self.mechanism_name:
            d["mechanism_name"] = self.mechanism_name
        return d


_ACSE_KINDS = {0: "AARQ", 1: "AARE", 2: "RLRQ", 3: "RLRE", 4: "ABRT"}


def _external_data(assoc_info: Tlv) -> bytes | None:
    """First EXTERNAL's encoding from an Association-information element."""
    for ext in assoc_info.children():
        if not ext.is_(UNIVERSAL, 8):
            continue
        for f in ext.children():
            if f.cls == CONTEXT and f.number in (0, 1, 2):
                return f.value
    return None


def _ap_title(t: Tlv) -> str | None:
    inner = t.children()
    if inner and inner[0].is_(UNIVERSAL, 6):
        return inner[0].as_oid()
    return None


def _inner_int(t: Tlv) -> int | None:
    inner = t.children()
    if inner and inner[0].is_(UNIVERSAL, 2):
        return inner[0].as_int()
    return None


def parse_acse(data: bytes) -> AcsePdu:
    try:
        top, _ = ber.decode_one(data)
        if top.cls != APPLICATION or top.number not in _ACSE_KINDS:
            raise IsoError("acse", f"unexpected APDU {top.describe()}")
        kind = _ACSE_KINDS[top.number]
        f: dict[str, Any] = {}
        for t in top.children():
            n = t.number if t.cls == CONTEXT else -1
            if kind in ("AARQ", "AARE"):
                if n == 1:
                    f["application_context"] = _ap_title(t)
                elif n == 2 and kind == "AARE":
                    f["result"] = _inner_int(t)
                elif n == 3 and kind == "AARE":
                    for d in t.children():
                        f["diagnostic_source"] = {1: "service-user", 2: "service-provider"}.get(
                            d.number, f"unknown-{d.number}"
                        )
                        f["diagnostic"] = _inner_int(d)
                elif n == 4 and kind == "AARE":
                    f["responding_ap_title"] = _ap_title(t)
                elif n == 5 and kind == "AARE":
                    f["responding_ae_qualifier"] = _inner_int(t)
                elif (n == 9 and kind == "AARE") or (n == 11 and kind == "AARQ"):
                    f["mechanism_name"] = ber.decode_oid(t.value) if t.value else None
                elif n == 30:
                    f["user_information"] = _external_data(t)
            elif kind == "ABRT":
                if n == 0:
                    f["abort_source"] = t.as_int()
                elif n == 1:
                    f["abort_diagnostic"] = t.as_int()
                elif n == 30:
                    f["user_information"] = _external_data(t)
            elif kind in ("RLRQ", "RLRE"):
                if n == 0:
                    f["release_reason"] = t.as_int()
                elif n == 30:
                    f["user_information"] = _external_data(t)
        return AcsePdu(kind, **f)
    except BerError as e:
        raise IsoError("acse", str(e)) from e


# ---------------------------------------------------------------------------
# MMS initiate / conclude (ISO 9506-2)
# ---------------------------------------------------------------------------
MMS_PDU_NAMES: dict[int, str] = {
    0: "confirmed-RequestPDU",
    1: "confirmed-ResponsePDU",
    2: "confirmed-ErrorPDU",
    3: "unconfirmed-PDU",
    4: "rejectPDU",
    5: "cancel-RequestPDU",
    6: "cancel-ResponsePDU",
    7: "cancel-ErrorPDU",
    8: "initiate-RequestPDU",
    9: "initiate-ResponsePDU",
    10: "initiate-ErrorPDU",
    11: "conclude-RequestPDU",
    12: "conclude-ResponsePDU",
    13: "conclude-ErrorPDU",
}
MMS_ERROR_CLASSES: dict[int, str] = {
    0: "vmd-state",
    1: "application-reference",
    2: "definition",
    3: "resource",
    4: "service",
    5: "service-preempt",
    6: "time-resolution",
    7: "access",
    8: "initiate",
    9: "conclude",
    10: "cancel",
    11: "file",
    12: "others",
}
MMS_INITIATE_ERRORS: dict[int, str] = {
    0: "other",
    1: "version-incompatible",
    2: "max-segment-insufficient",
    3: "max-services-outstanding-calling-insufficient",
    4: "max-services-outstanding-called-insufficient",
    5: "service-cbb-insufficient",
    6: "parameter-cbb-insufficient",
    7: "nesting-level-insufficient",
}
MMS_RESOURCE_ERRORS: dict[int, str] = {
    0: "other",
    1: "memory-unavailable",
    2: "processor-resource-unavailable",
    3: "mass-storage-unavailable",
    4: "capability-unavailable",
    5: "capability-unknown",
}
MMS_REJECT_TYPES: dict[int, str] = {
    1: "confirmed-request",
    2: "confirmed-response",
    3: "confirmed-error",
    4: "unconfirmed",
    5: "pdu-error",
    6: "cancel-request",
    7: "cancel-response",
    8: "cancel-error",
    9: "conclude-request",
    10: "conclude-response",
    11: "conclude-error",
}
PARAMETER_CBB_NAMES: dict[int, str] = {
    0: "str1",
    1: "str2",
    2: "vnam",
    3: "valt",
    4: "vadr",
    5: "vsca",
    6: "tpy",
    7: "vlis",
    8: "real",
    10: "cei",
    11: "aco",
    12: "sem",
    13: "csr",
    14: "csnc",
    15: "csplc",
    16: "cspi",
    17: "char",
}
SERVICE_NAMES: tuple[str, ...] = (
    "status", "getNameList", "identify", "rename", "read", "write", "getVariableAccessAttributes",
    "defineNamedVariable", "defineScatteredAccess", "getScatteredAccessAttributes", "deleteVariableAccess",
    "defineNamedVariableList", "getNamedVariableListAttributes", "deleteNamedVariableList", "defineNamedType",
    "getNamedTypeAttributes", "deleteNamedType", "input", "output", "takeControl", "relinquishControl",
    "defineSemaphore", "deleteSemaphore", "reportSemaphoreStatus", "reportPoolSemaphoreStatus",
    "reportSemaphoreEntryStatus", "initiateDownloadSequence", "downloadSegment", "terminateDownloadSequence",
    "initiateUploadSequence", "uploadSegment", "terminateUploadSequence", "requestDomainDownload",
    "requestDomainUpload", "loadDomainContent", "storeDomainContent", "deleteDomain", "getDomainAttributes",
    "createProgramInvocation", "deleteProgramInvocation", "start", "stop", "resume", "reset", "kill",
    "getProgramInvocationAttributes", "obtainFile", "defineEventCondition", "deleteEventCondition",
    "getEventConditionAttributes", "reportEventConditionStatus", "alterEventConditionMonitoring", "triggerEvent",
    "defineEventAction", "deleteEventAction", "getEventActionAttributes", "reportEventActionStatus",
    "defineEventEnrollment", "deleteEventEnrollment", "alterEventEnrollment", "reportEventEnrollmentStatus",
    "getEventEnrollmentAttributes", "acknowledgeEventNotification", "getAlarmSummary",
    "getAlarmEnrollmentSummary", "readJournal", "writeJournal", "initializeJournal", "reportJournalStatus",
    "createJournal", "deleteJournal", "getCapabilityList", "fileOpen", "fileRead", "fileClose", "fileRename",
    "fileDelete", "fileDirectory", "unsolicitedStatus", "informationReport", "eventNotification",
    "attachToEventCondition", "attachToSemaphore", "conclude", "cancel", "getDataExchangeAttributes",
    "exchangeData", "defineAccessControlList", "getAccessControlListAttributes",
    "reportAccessControlledObjects", "deleteAccessControlList", "changeAccessControl",
)  # fmt: skip


def mms_initiate_request(
    *,
    max_pdu_size: int = 65000,
    max_serv_outstanding_calling: int = 5,
    max_serv_outstanding_called: int = 5,
    data_structure_nesting_level: int | None = 10,
    version: int = 1,
    parameter_cbb: tuple[bytes, int] = DEFAULT_PARAMETER_CBB,
    services_supported: tuple[bytes, int] = DEFAULT_SERVICES_CALLING,
) -> bytes:
    """initiate-RequestPDU. Bit strings are given as (octets, number of bits)."""

    def bits(tag: int, value: tuple[bytes, int]) -> bytes:
        data, n = value
        return ber.bit_string(data, len(data) * 8 - n, tag)

    body = ber.integer(max_pdu_size, 0x80)
    body += ber.integer(max_serv_outstanding_calling, 0x81)
    body += ber.integer(max_serv_outstanding_called, 0x82)
    if data_structure_nesting_level is not None:
        body += ber.integer(data_structure_nesting_level, 0x83)
    detail = ber.integer(version, 0x80) + bits(0x81, parameter_cbb) + bits(0x82, services_supported)
    body += ber.tlv(0xA4, detail)
    return ber.tlv(0xA8, body)


def mms_conclude_request() -> bytes:
    return bytes([0x8B, 0x00])


@dataclass(frozen=True)
class InitiateParams:
    """Parameters negotiated in an MMS initiate-ResponsePDU (as the server sent them)."""

    max_pdu_size: int | None  # localDetailCalled
    max_serv_outstanding_calling: int | None
    max_serv_outstanding_called: int | None
    data_structure_nesting_level: int | None
    version: int | None
    parameter_cbb: bytes = b""
    parameter_cbb_bits: int = 0
    services_supported: bytes = b""
    services_supported_bits: int = 0

    def parameter_cbb_names(self) -> list[str]:
        return [
            PARAMETER_CBB_NAMES.get(i, f"bit{i}")
            for i in ber.bits_set(self.parameter_cbb, self.parameter_cbb_bits)
        ]

    def supports(self, service: str | int) -> bool:
        bit = SERVICE_NAMES.index(service) if isinstance(service, str) else service
        return bit < self.services_supported_bits and bit in ber.bits_set(
            self.services_supported, self.services_supported_bits
        )

    def supported_services(self) -> list[str]:
        return [
            SERVICE_NAMES[i] if i < len(SERVICE_NAMES) else f"bit{i}"
            for i in ber.bits_set(self.services_supported, self.services_supported_bits)
        ]

    def to_json(self) -> dict[str, Any]:
        return {
            "max_pdu_size": self.max_pdu_size,
            "max_serv_outstanding_calling": self.max_serv_outstanding_calling,
            "max_serv_outstanding_called": self.max_serv_outstanding_called,
            "data_structure_nesting_level": self.data_structure_nesting_level,
            "version": self.version,
            "parameter_cbb_hex": self.parameter_cbb.hex(),
            "parameter_cbb_bits": self.parameter_cbb_bits,
            "parameter_cbb": self.parameter_cbb_names(),
            "services_supported_hex": self.services_supported.hex(),
            "services_supported_bits": self.services_supported_bits,
            "services_supported": self.supported_services(),
        }


@dataclass(frozen=True)
class MmsPdu:
    """A decoded MMS PDU (only the initiate / conclude / reject forms are interpreted)."""

    number: int
    initiate: InitiateParams | None = None
    error_class: int | None = None
    error_code: int | None = None
    raw: bytes = b""

    @property
    def name(self) -> str:
        return MMS_PDU_NAMES.get(self.number, f"unknown-{self.number}")

    @property
    def error_class_name(self) -> str | None:
        """Error class of an initiate/conclude error, or the rejected PDU type of a rejectPDU."""
        c = self.error_class
        if c is None:
            return None
        table = MMS_REJECT_TYPES if self.number == 4 else MMS_ERROR_CLASSES
        return table.get(c, f"unknown-{c}")

    @property
    def error_code_name(self) -> str | None:
        if self.error_code is None:
            return None
        table = {8: MMS_INITIATE_ERRORS, 3: MMS_RESOURCE_ERRORS}.get(self.error_class or -1)
        if table is None or self.number == 4:
            return str(self.error_code)
        return table.get(self.error_code, f"unknown-{self.error_code}")

    def to_json(self) -> dict[str, Any]:
        d: dict[str, Any] = {"pdu": self.name}
        if self.initiate:
            d["initiate"] = self.initiate.to_json()
        if self.error_class is not None:
            d["error_class"] = self.error_class
            d["error_class_name"] = self.error_class_name
            d["error_code"] = self.error_code
            d["error_code_name"] = self.error_code_name
        return d


def _parse_initiate(t: Tlv) -> InitiateParams:
    f: dict[str, Any] = {
        "max_pdu_size": None,
        "max_serv_outstanding_calling": None,
        "max_serv_outstanding_called": None,
        "data_structure_nesting_level": None,
        "version": None,
    }
    for c in t.children():
        if c.cls != CONTEXT:
            continue
        if c.number == 0:
            f["max_pdu_size"] = c.as_int()
        elif c.number == 1:
            f["max_serv_outstanding_calling"] = c.as_int()
        elif c.number == 2:
            f["max_serv_outstanding_called"] = c.as_int()
        elif c.number == 3:
            f["data_structure_nesting_level"] = c.as_int()
        elif c.number == 4 and c.constructed:
            for d in c.children():
                if d.number == 0:
                    f["version"] = d.as_int()
                elif d.number == 1:
                    f["parameter_cbb"], f["parameter_cbb_bits"] = ber.decode_bit_string(d.value)
                elif d.number == 2:
                    f["services_supported"], f["services_supported_bits"] = ber.decode_bit_string(d.value)
    return InitiateParams(**f)


def _parse_service_error(t: Tlv) -> tuple[int | None, int | None]:
    """(error class, error code) of a ServiceError."""
    for c in t.children():
        if c.is_(CONTEXT, 0) and c.constructed:
            inner = c.children()
            if inner:
                return inner[0].number, ber.decode_integer(inner[0].value) if inner[0].value else None
    return None, None


def parse_mms(data: bytes) -> MmsPdu:
    try:
        top, _ = ber.decode_one(data)
        if top.cls != CONTEXT:
            raise IsoError("mms", f"unexpected MMS PDU {top.describe()}")
        if top.number in (8, 9):
            return MmsPdu(top.number, initiate=_parse_initiate(top), raw=top.raw)
        if top.number in (10, 13):  # initiate-ErrorPDU / conclude-ErrorPDU are ServiceErrors
            cls_, code = _parse_service_error(top)
            return MmsPdu(top.number, error_class=cls_, error_code=code, raw=top.raw)
        if top.number == 4 and top.constructed:  # rejectPDU: invokeID?, rejectReason CHOICE
            reason = [c for c in top.children() if c.number != 0 and c.cls == CONTEXT]
            if reason:
                return MmsPdu(4, error_class=reason[0].number, error_code=reason[0].as_int(), raw=top.raw)
        return MmsPdu(top.number, raw=top.raw)
    except BerError as e:
        raise IsoError("mms", str(e)) from e


# ---------------------------------------------------------------------------
# Complete requests
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class AssociateParams:
    """Everything that goes into an association request. Defaults are libiec61850's.

    AP titles are dotted object identifiers; ``None`` omits the field (as does an AE qualifier of
    ``None``). Selectors are raw octets; empty omits them.
    """

    calling_tsel: bytes = b"\x00\x01"
    called_tsel: bytes = b"\x00\x01"
    calling_ssel: bytes = b"\x00\x01"
    called_ssel: bytes = b"\x00\x01"
    calling_psel: bytes = b"\x00\x00\x00\x01"
    called_psel: bytes = b"\x00\x00\x00\x01"
    calling_ap_title: str | None = DEFAULT_CALLING_AP_TITLE
    calling_ae_qualifier: int | None = DEFAULT_AE_QUALIFIER
    called_ap_title: str | None = DEFAULT_CALLED_AP_TITLE
    called_ae_qualifier: int | None = DEFAULT_AE_QUALIFIER
    tpdu_size_code: int = 0x0D
    max_pdu_size: int = 65000
    max_serv_outstanding_calling: int = 5
    max_serv_outstanding_called: int = 5
    data_structure_nesting_level: int | None = 10
    version: int = 1
    parameter_cbb: tuple[bytes, int] = DEFAULT_PARAMETER_CBB
    services_supported: tuple[bytes, int] = DEFAULT_SERVICES_CALLING

    def to_json(self) -> dict[str, Any]:
        return {
            "calling_tsel": self.calling_tsel.hex(),
            "called_tsel": self.called_tsel.hex(),
            "calling_ssel": self.calling_ssel.hex(),
            "called_ssel": self.called_ssel.hex(),
            "calling_psel": self.calling_psel.hex(),
            "called_psel": self.called_psel.hex(),
            "calling_ap_title": self.calling_ap_title,
            "calling_ae_qualifier": self.calling_ae_qualifier,
            "called_ap_title": self.called_ap_title,
            "called_ae_qualifier": self.called_ae_qualifier,
            "tpdu_size": tpdu_size_bytes(self.tpdu_size_code),
            "max_pdu_size": self.max_pdu_size,
            "max_serv_outstanding_calling": self.max_serv_outstanding_calling,
            "max_serv_outstanding_called": self.max_serv_outstanding_called,
            "data_structure_nesting_level": self.data_structure_nesting_level,
            "version": self.version,
        }


def connection_request(p: AssociateParams | None = None) -> bytes:
    """COTP CR TPDU for ``p``."""
    p = p or AssociateParams()
    return cotp_cr(tpdu_size_code=p.tpdu_size_code, calling_tsel=p.calling_tsel, called_tsel=p.called_tsel)


def association_request(p: AssociateParams | None = None) -> bytes:
    """The session CONNECT SPDU carrying CP-type / AARQ / initiate-RequestPDU (COTP DT payload)."""
    p = p or AssociateParams()
    initiate = mms_initiate_request(
        max_pdu_size=p.max_pdu_size,
        max_serv_outstanding_calling=p.max_serv_outstanding_calling,
        max_serv_outstanding_called=p.max_serv_outstanding_called,
        data_structure_nesting_level=p.data_structure_nesting_level,
        version=p.version,
        parameter_cbb=p.parameter_cbb,
        services_supported=p.services_supported,
    )
    aarq = acse_aarq(
        initiate,
        calling_ap_title=p.calling_ap_title,
        calling_ae_qualifier=p.calling_ae_qualifier,
        called_ap_title=p.called_ap_title,
        called_ae_qualifier=p.called_ae_qualifier,
    )
    cp = presentation_cp(aarq, calling_psel=p.calling_psel, called_psel=p.called_psel)
    return session_connect(cp, calling_ssel=p.calling_ssel, called_ssel=p.called_ssel)


def conclude_request() -> bytes:
    """DATA-TRANSFER SPDU carrying an MMS conclude-RequestPDU."""
    return session_data(presentation_user_data(MMS_CONTEXT_ID, mms_conclude_request()))


def release_request() -> bytes:
    """FINISH SPDU carrying an ACSE RLRQ (reason normal)."""
    return session_finish(presentation_user_data(ACSE_CONTEXT_ID, acse_rlrq(0)))


def abort_request() -> bytes:
    """ABORT SPDU carrying ARU-PPDU / ACSE ABRT (source: service user), as libiec61850 sends it."""
    return session_abort(presentation_aru(acse_abrt(0)))


def looks_like_tls(data: bytes) -> bool:
    """True when bytes look like a TLS record (handshake 0x16 or alert 0x15, version 3.x)."""
    return len(data) >= 3 and data[0] in (0x15, 0x16) and data[1] == 0x03 and data[2] <= 0x04
