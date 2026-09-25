"""Layered connectivity probes below libiec61850 (DIA-3, DIA-6).

libiec61850 reports every failed association attempt as one generic code (see
:mod:`mms_protocol.diagnosis.classify` for the observed behaviour), so these probes speak the lower
layers themselves over a plain TCP socket and report exactly where an attempt stops:

========== ============== ==========================================================
layer      probe name     what it shows
========== ============== ==========================================================
network    icmp           ICMP echo via the system ``ping`` (never conclusive alone)
network    tcp-<port>     TCP connect: accepted / refused / timeout / unreachable
transport  cotp           COTP CR -> CC / DR / ER / nothing / immediate close
session    iso-session    session CONNECT -> ACCEPT / REFUSE / ABORT
session    presentation   CP-type -> CPA / CPR (provider reason)
mms        acse           AARQ -> AARE result and result-source-diagnostic
mms        mms-initiate   initiate-Request -> initiate-Response (parameters) / -Error
mms        release        MMS conclude + ACSE release (or abort) at the end
========== ============== ==========================================================

Rules: every probe has a timeout, can bind to a local address (NBR-2), needs no privileges, never
raises for network conditions, and records the exact bytes sent and received (hex) and the
decoded fields in ``raw`` so that an expert can check the conclusion (CLI-7).

Each probe opens its own TCP connection. On a real IED every connection may take one of a small
number of association slots for a moment, so :func:`run_layered_probes` avoids redundant
connections: the association probe includes the TCP and COTP steps.
"""

from __future__ import annotations

import hashlib
import socket
import ssl
import time
import warnings
from dataclasses import dataclass, field
from typing import Any

from ied_client import codes
from ied_client.codes import ErrorInfo
from ied_client.diagnosis.probe import (
    DEFAULT_TIMEOUT_S,
    ProbeResult,
    errno_text,
    open_tcp,
    ping,
    tcp_connect,
)

from ..codes import acse_diagnostic
from . import iso
from .iso import AssociateParams, IsoError

LAYERS = ("network", "transport", "session", "mms")
TLS_PORT = 3782
DEFAULT_LINGER_S = 1.0


def _hex(data: bytes) -> str:
    return data.hex(" ")


def tls_port_open(
    host: str,
    timeout: float = DEFAULT_TIMEOUT_S,
    port: int = TLS_PORT,
    *,
    local_ip: str | None = None,
    handshake: bool = True,
) -> ProbeResult:
    """Is the IEC 62351 TLS port open (DIA-6)? Optionally confirm with a TLS handshake.

    Outcomes: ``tls`` (handshake completed), ``tls-alert`` (the server answered with a TLS alert,
    e.g. because it wants a client certificate: still TLS), ``open`` (TCP open, TLS not confirmed),
    or the TCP outcomes. ``ok`` is True whenever the port accepted the connection. No certificate is
    validated and nothing is sent after the handshake.
    """
    sock, tcp = open_tcp(host, port, timeout, local_ip)
    tcp.name = f"tcp-{port}"
    if sock is None:
        return tcp
    raw = dict(tcp.raw)
    t0 = time.monotonic()
    outcome, detail = "open", f"TCP {port} (IEC 62351 TLS port) is open"
    if handshake:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with warnings.catch_warnings():  # old IEDs may only speak TLS 1.0: still worth detecting
            warnings.simplefilter("ignore", DeprecationWarning)
            try:
                ctx.minimum_version = ssl.TLSVersion.TLSv1
            except (ValueError, AttributeError):
                pass
        try:
            sock.settimeout(timeout)
            with ctx.wrap_socket(sock, do_handshake_on_connect=False) as tls:
                tls.do_handshake()
                cert = tls.getpeercert(binary_form=True)
                raw["tls_version"] = tls.version()
                raw["cipher"] = tls.cipher()[0] if tls.cipher() else None
                if cert:
                    raw["peer_certificate_sha256"] = hashlib.sha256(cert).hexdigest()
                outcome = "tls"
                detail = f"TCP {port} is open and speaks TLS ({raw['tls_version']}, {raw['cipher']})"
            sock = None
        except ssl.SSLError as e:
            raw["tls_error"] = f"{e.reason or e}"
            not_tls = isinstance(e, ssl.SSLEOFError) or e.reason in (
                "WRONG_VERSION_NUMBER",
                "RECORD_LAYER_FAILURE",
            )
            if not_tls:
                detail = f"TCP {port} is open; no TLS handshake ({e.reason or e})"
            else:
                outcome = "tls-alert"
                detail = (
                    f"TCP {port} is open and a TLS peer ended the handshake ({e.reason or e}): a TLS server"
                )
        except (OSError, ValueError) as e:
            raw["tls_error"] = str(e)
            detail = f"TCP {port} is open; TLS handshake not completed ({e})"
    if sock is not None:
        sock.close()
    return ProbeResult(
        "network", f"tcp-{port}", True, outcome, tcp.duration_s + time.monotonic() - t0, detail, raw=raw
    )


# ---------------------------------------------------------------------------
# Stream helper
# ---------------------------------------------------------------------------
class _Closed(Exception):
    def __init__(self, reset: bool, text: str) -> None:
        self.reset = reset
        self.text = text
        super().__init__(text)


class _Timeout(Exception):
    pass


class _Stream:
    """TPKT framing over a connected socket, with a hex log of everything sent and received."""

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.buf = bytearray()
        self.t_open = time.monotonic()

    def send(self, data: bytes) -> None:
        try:
            self.sock.sendall(data)
        except OSError as e:
            raise _Closed(True, f"send failed ({errno_text(e)})") from e

    def _fill(self, deadline: float) -> None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _Timeout()
        self.sock.settimeout(remaining)
        try:
            d = self.sock.recv(65536)
        except TimeoutError as e:
            raise _Timeout() from e
        except ConnectionResetError as e:
            raise _Closed(True, "connection reset by the server (RST)") from e
        except OSError as e:
            raise _Closed(True, f"receive failed ({errno_text(e)})") from e
        if not d:
            raise _Closed(False, "connection closed by the server (FIN)")
        self.buf += d

    def recv_tpkt(self, timeout: float) -> bytes:
        """The next whole TPKT packet (header included)."""
        deadline = time.monotonic() + timeout
        while True:
            if len(self.buf) >= iso.TPKT_HEADER:
                if iso.looks_like_tls(bytes(self.buf[:3])):
                    raise IsoError(
                        "tpkt", f"received a TLS record instead of TPKT: {_hex(bytes(self.buf[:16]))}"
                    )
                n = iso.tpkt_length(bytes(self.buf[:4]))
                if len(self.buf) >= n:
                    pkt = bytes(self.buf[:n])
                    del self.buf[:n]
                    return pkt
            self._fill(deadline)

    def recv_cotp(self, timeout: float, received: list[bytes]) -> tuple[iso.CotpPdu, bytes]:
        """Next COTP TPDU; for DT, reassemble until end-of-TSDU and return the payload too."""
        deadline = time.monotonic() + timeout
        payload = b""
        while True:
            pkt = self.recv_tpkt(max(0.0, deadline - time.monotonic()))
            received.append(pkt)
            pdu = iso.parse_cotp(pkt[iso.TPKT_HEADER :])
            if pdu.code != iso.COTP_DT:
                return pdu, b""
            payload += pdu.user_data
            if pdu.eot:
                return pdu, payload

    def wait_closed(self, timeout: float) -> tuple[bool, str, bytes]:
        """Wait up to ``timeout`` for the server to close. Returns (closed, how, data received)."""
        deadline = time.monotonic() + timeout
        start = len(self.buf)
        try:
            while True:
                self._fill(deadline)
        except _Timeout:
            return False, "", bytes(self.buf[start:])
        except _Closed as e:
            return True, e.text, bytes(self.buf[start:])

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def _send_dt(stream: _Stream, spdu: bytes, tpdu_size: int, sent: list[bytes]) -> None:
    """Send a session message as COTP DT TPDUs no larger than the negotiated TPDU size."""
    room = max(1, tpdu_size - 3)
    chunks = [spdu[i : i + room] for i in range(0, len(spdu), room)] or [b""]
    for i, chunk in enumerate(chunks):
        pkt = iso.tpkt(iso.cotp_dt(chunk, eot=i == len(chunks) - 1))
        sent.append(pkt)
        stream.send(pkt)


# ---------------------------------------------------------------------------
# COTP
# ---------------------------------------------------------------------------
def _cotp_exchange(
    sock: socket.socket, params: AssociateParams, timeout: float
) -> tuple[_Stream, ProbeResult, iso.CotpPdu | None]:
    """Send CR, read the answer. Returns the stream, the ``cotp`` step, and the CC (if accepted)."""
    stream = _Stream(sock)
    cr = iso.tpkt(iso.connection_request(params))
    received: list[bytes] = []
    raw: dict[str, Any] = {"sent": [_hex(cr)]}
    t0 = time.monotonic()
    pdu: iso.CotpPdu | None = None
    try:
        stream.send(cr)
        pdu, _ = stream.recv_cotp(timeout, received)
    except _Timeout:
        outcome, detail = (
            "cotp-no-response",
            f"no answer to the COTP connection request (CR) within {timeout:g} s",
        )
    except _Closed as e:
        after = time.monotonic() - stream.t_open
        raw.update(closed_after_s=round(after, 6), reset=e.reset)
        if stream.buf:
            received.append(bytes(stream.buf))
        outcome = "tcp-closed-immediately"
        detail = f"TCP accepted, then {e.text} {after:.3f} s after connect, before any COTP response"
    except IsoError as e:
        received.append(bytes(stream.buf))
        raw["looks_like_tls"] = iso.looks_like_tls(bytes(stream.buf[:3]))
        outcome = "cotp-invalid-response"
        detail = f"COTP response not understood ({e})" + (
            " — the reply looks like TLS" if raw["looks_like_tls"] else ""
        )
    else:
        if pdu.code == iso.COTP_CC:
            outcome = "accepted"
            detail = (
                f"COTP CC: connection confirmed (class {pdu.class_option}, TPDU size {pdu.tpdu_size or 'not stated'}, "
                f"src-ref 0x{pdu.src_ref or 0:04x})"
            )
        elif pdu.code == iso.COTP_DR:
            outcome = "cotp-rejected"
            detail = f"COTP DR: disconnect request, reason 0x{pdu.reason:02x} {pdu.reason_name}"
        elif pdu.code == iso.COTP_ER:
            bad = pdu.params.get(iso.COTP_PARAM_INVALID_TPDU, b"")
            outcome = "cotp-rejected"
            detail = f"COTP ER: TPDU error, cause {pdu.reason} {pdu.reason_name}"
            detail += f", rejected TPDU header {_hex(bad)}" if bad else ""
        else:
            outcome, detail = "cotp-invalid-response", f"unexpected COTP {pdu.kind} TPDU in reply to CR"
        raw["pdu"] = pdu.to_json()
    raw["received"] = [_hex(b) for b in received]
    ok = outcome == "accepted"
    err = None if ok else codes.association(outcome)
    step = ProbeResult("transport", "cotp", ok, outcome, time.monotonic() - t0, detail, err, raw)
    return stream, step, pdu if ok else None


def cotp_connect(
    host: str,
    port: int = 102,
    timeout: float = DEFAULT_TIMEOUT_S,
    local_ip: str | None = None,
    *,
    params: AssociateParams | None = None,
) -> ProbeResult:
    """TCP connect + COTP CR (class 0, TPDU size 8192, TSELs 0001/0001 like libiec61850).

    Outcomes: accepted (CC), cotp-rejected (DR/ER, reason included), cotp-no-response,
    tcp-closed-immediately, cotp-invalid-response, or the TCP outcome if TCP failed. Afterwards the
    TCP connection is closed (class 0 has no disconnect TPDU of its own).
    """
    sock, tcp = open_tcp(host, port, timeout, local_ip)
    if sock is None:
        return ProbeResult(
            "transport", "cotp", tcp.ok, tcp.outcome, tcp.duration_s, tcp.detail, tcp.error, {"tcp": tcp.raw}
        )
    stream, step, _ = _cotp_exchange(sock, params or AssociateParams(), timeout)
    stream.close()
    step.raw["tcp"] = tcp.raw
    step.duration_s += tcp.duration_s
    return step


# ---------------------------------------------------------------------------
# Full association
# ---------------------------------------------------------------------------
@dataclass
class AssociationProbe:
    """Result of :func:`iso_associate`: per-layer steps and the final classification."""

    host: str
    port: int
    local_ip: str | None
    outcome: str
    failed_layer: str | None
    steps: list[ProbeResult]
    duration_s: float
    initiate: iso.InitiateParams | None = None
    acse_diagnostic: ErrorInfo | None = None
    acse: iso.AcsePdu | None = None
    closed_after_accept: bool | None = None
    closed_after_s: float | None = None
    ended_by: str | None = None  # "release" | "abort" | "server-closed" | "client-closed"
    request: AssociateParams = field(default_factory=AssociateParams)

    @property
    def ok(self) -> bool:
        return self.outcome == "accepted"

    @property
    def classification(self) -> ErrorInfo:
        return codes.association(self.outcome)

    def step(self, name: str) -> ProbeResult | None:
        for s in self.steps:
            if s.name == name:
                return s
        return None

    @property
    def tcp(self) -> ProbeResult:
        return self.steps[0]

    @property
    def cotp(self) -> ProbeResult | None:
        return self.step("cotp")

    @property
    def detail(self) -> str:
        if self.ok:
            p = self.initiate
            extra = ""
            if p:
                extra = (
                    f": max PDU {p.max_pdu_size}, outstanding {p.max_serv_outstanding_calling}/"
                    f"{p.max_serv_outstanding_called}, nesting {p.data_structure_nesting_level}, version {p.version}"
                )
            return f"association accepted{extra}"
        failing = next((s for s in reversed(self.steps) if s.ok is False and s.name != "release"), None)
        text = codes.ASSOCIATION_OUTCOMES[self.outcome]
        return f"{text}: {failing.detail}" if failing else text

    @property
    def result(self) -> ProbeResult:
        """Summary ProbeResult named ``iso-association``."""
        return ProbeResult(
            self.failed_layer or "mms",
            "iso-association",
            self.ok,
            self.outcome,
            self.duration_s,
            self.detail,
            None if self.ok else self.classification,
            {"steps": [s.name for s in self.steps], "ended_by": self.ended_by},
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "port": self.port,
            "local_ip": self.local_ip,
            "ok": self.ok,
            "outcome": self.outcome,
            "classification": self.classification.to_json(),
            "failed_layer": self.failed_layer,
            "detail": self.detail,
            "duration_s": round(self.duration_s, 6),
            "initiate": self.initiate.to_json() if self.initiate else None,
            "acse_diagnostic": self.acse_diagnostic.to_json() if self.acse_diagnostic else None,
            "closed_after_accept": self.closed_after_accept,
            "closed_after_s": self.closed_after_s,
            "ended_by": self.ended_by,
            "request": self.request.to_json(),
            "steps": [s.to_json() for s in self.steps],
        }


# The layer each step of the association probe belongs to.
STEP_LAYERS: dict[str, str] = {
    "cotp-disconnect": "transport",
    "iso-session": "session",
    "presentation": "session",
    "acse": "mms",
    "mms-initiate": "mms",
    "linger": "mms",
    "release": "mms",
}


class _Assoc:
    """State of one association attempt (keeps iso_associate readable)."""

    def __init__(self) -> None:
        self.steps: list[ProbeResult] = []
        self.outcome = "unknown"
        self.failed_layer: str | None = None
        self.initiate: iso.InitiateParams | None = None
        self.acse: iso.AcsePdu | None = None
        self.acse_diag: ErrorInfo | None = None

    def fail(self, layer: str, outcome: str) -> None:
        """Record a rejection; a deeper (or equally deep) layer's answer replaces a shallower one,
        except that failing to understand a deeper PDU never hides a rejection already seen."""
        deeper = self.failed_layer is not None and LAYERS.index(layer) >= LAYERS.index(self.failed_layer)
        if self.failed_layer is None or (deeper and outcome != "invalid-response"):
            self.outcome, self.failed_layer = outcome, layer

    def add(
        self,
        name: str,
        ok: bool | None,
        outcome: str,
        t0: float,
        detail: str,
        error: ErrorInfo | None = None,
        **raw: Any,
    ) -> ProbeResult:
        """Append a step (its layer comes from :data:`STEP_LAYERS`); a failed step counts as a rejection."""
        layer = STEP_LAYERS[name]
        if ok is False and error is None and outcome in codes.ASSOCIATION_OUTCOMES:
            error = codes.association(outcome)
        r = ProbeResult(layer, name, ok, outcome, time.monotonic() - t0, detail, error, raw)
        self.steps.append(r)
        if ok is False:
            self.fail(layer, outcome)
        return r

    # -- layers above the session ---------------------------------------------------------
    def presentation(self, spdu: iso.Spdu, t0: float) -> iso.PresentationResponse | None:
        try:
            ppdu = iso.parse_presentation(spdu.user_data, abort=spdu.si == iso.SPDU_AB)
        except IsoError as e:
            detail = f"presentation PDU not understood ({e})"
            self.add("presentation", False, "invalid-response", t0, detail, received=_hex(spdu.user_data))
            return None
        info = ppdu.to_json()
        if ppdu.kind == "CPA":
            results = ", ".join(
                iso.PRESENTATION_CONTEXT_RESULTS.get(r, str(r)) for r, _ in ppdu.context_results
            )
            if any(r != 0 for r, _ in ppdu.context_results):
                detail = f"presentation CPA, but a context was rejected: [{results}]"
                self.add("presentation", False, "presentation-rejected", t0, detail, pdu=info)
            else:
                self.add(
                    "presentation", True, "accepted", t0, f"presentation CPA: contexts [{results}]", pdu=info
                )
        elif ppdu.kind == "CPR":
            if ppdu.provider_reason is not None:
                reason = f"provider-reason {ppdu.provider_reason} {ppdu.provider_reason_name}"
            else:
                reason = "no provider-reason (rejected by the user: see ACSE)"
            self.add(
                "presentation", False, "presentation-rejected", t0, f"presentation CPR: {reason}", pdu=info
            )
        elif ppdu.kind == "ARP":
            name = iso.PRESENTATION_ABORT_REASONS.get(ppdu.abort_reason or 0, "?")
            detail = f"presentation provider abort (ARP), reason {ppdu.abort_reason} {name}"
            self.add("presentation", False, "session-aborted", t0, detail, pdu=info)
        else:  # ARU: the ACSE APDU inside says more
            self.add("presentation", None, "user-abort", t0, "presentation user abort (ARU)", pdu=info)
        return ppdu

    def acse_layer(self, ppdu: iso.PresentationResponse, t0: float) -> iso.AcsePdu | None:
        apdu = ppdu.apdu(iso.ACSE_CONTEXT_ID)
        if apdu is None:
            if ppdu.kind == "CPA":
                self.add("acse", False, "invalid-response", t0, "presentation CPA without an ACSE AARE")
            return None
        try:
            acse = iso.parse_acse(apdu)
        except IsoError as e:
            self.add(
                "acse", False, "invalid-response", t0, f"ACSE PDU not understood ({e})", received=_hex(apdu)
            )
            return None
        self.acse = acse
        info = acse.to_json()
        if acse.kind == "ABRT":
            src = iso.ACSE_ABORT_SOURCES.get(acse.abort_source or 0, "?")
            detail = f"ACSE ABRT from {src} ({acse.abort_source})"
            if acse.abort_diagnostic:
                detail += f", diagnostic {acse.abort_diagnostic} {acse.abort_diagnostic_name}"
            self.add("acse", False, "acse-aborted", t0, detail, pdu=info)
            return acse
        if acse.kind != "AARE":
            detail = f"unexpected ACSE {acse.kind} in reply to AARQ"
            self.add("acse", False, "invalid-response", t0, detail, pdu=info)
            return acse
        detail = f"ACSE AARE result {acse.result} {acse.result_name}"
        if acse.diagnostic is not None:
            detail += f", diagnostic acse-{acse.diagnostic_source} {acse.diagnostic} {acse.diagnostic_name}"
            if acse.diagnostic_source == "service-user":
                self.acse_diag = acse_diagnostic(acse.diagnostic)
        if acse.responding_ap_title:
            detail += f", responding AP title {acse.responding_ap_title}"
        if acse.responding_ae_qualifier is not None:
            detail += f" AE qualifier {acse.responding_ae_qualifier}"
        if acse.result == 0:
            self.add("acse", True, "accepted", t0, detail, pdu=info)
        else:
            outcome = {1: "acse-rejected-permanent", 2: "acse-rejected-transient"}.get(acse.result or -1)
            err = self.acse_diag if self.acse_diag and self.acse_diag.code else None
            self.add("acse", False, outcome or "invalid-response", t0, detail, err, pdu=info)
        return acse

    def mms_layer(self, acse: iso.AcsePdu, t0: float) -> None:
        if acse.kind != "AARE":
            return
        if acse.user_information is None:
            if acse.result == 0:
                detail = "AARE accepted without an MMS initiate-ResponsePDU"
                self.add("mms-initiate", False, "invalid-response", t0, detail)
            return
        try:
            pdu = iso.parse_mms(acse.user_information)
        except IsoError as e:
            detail, received = f"MMS PDU not understood ({e})", _hex(acse.user_information)
            self.add("mms-initiate", False, "invalid-response", t0, detail, received=received)
            return
        info = pdu.to_json()
        info["hex"] = _hex(acse.user_information)
        if pdu.number == 9 and pdu.initiate:
            p = self.initiate = pdu.initiate
            detail = (
                f"MMS initiate-ResponsePDU: localDetailCalled (max PDU) {p.max_pdu_size}, "
                f"maxServOutstanding calling {p.max_serv_outstanding_calling} / called {p.max_serv_outstanding_called}, "
                f"nesting level {p.data_structure_nesting_level}, version {p.version}, "
                f"parameter CBB {' '.join(p.parameter_cbb_names()) or '-'} ({p.parameter_cbb.hex()}), "
                f"{len(p.supported_services())} services ({p.services_supported.hex()})"
            )
            if acse.result == 0:
                self.add("mms-initiate", True, "accepted", t0, detail, pdu=info)
            else:
                self.add("mms-initiate", None, "response-in-rejection", t0, detail, pdu=info)
        elif pdu.number == 10:
            detail = (
                f"MMS initiate-ErrorPDU: error class {pdu.error_class} {pdu.error_class_name}, "
                f"code {pdu.error_code} {pdu.error_code_name}"
            )
            self.add("mms-initiate", False, "initiate-error", t0, detail, pdu=info)
        else:
            self.add(
                "mms-initiate", False, "invalid-response", t0, f"unexpected MMS {pdu.name} in AARE", pdu=info
            )


def iso_associate(
    host: str,
    port: int = 102,
    timeout: float = DEFAULT_TIMEOUT_S,
    local_ip: str | None = None,
    *,
    max_pdu_size: int = 65000,
    calling_ap_title: str | None = None,
    called_ap_title: str | None = None,
    params: AssociateParams | None = None,
    linger_s: float = DEFAULT_LINGER_S,
    release: bool = True,
) -> AssociationProbe:
    """A complete association attempt, decoded layer by layer, then ended cleanly.

    Sends COTP CR, then a session CONNECT carrying CP-type / AARQ / initiate-RequestPDU built from
    ``params`` (libiec61850's defaults unless overridden; ``calling_ap_title`` /
    ``called_ap_title`` override the AP titles, an empty string omits them). Parses the answer
    layer by layer; the most specific (deepest) rejection becomes the outcome.

    After acceptance it waits up to ``linger_s`` for the server to drop the connection
    (``accepted-then-closed``), then ends the association with MMS conclude + ACSE release
    (``release=True``) or an ACSE abort, falling back to abort if the release is not confirmed,
    and closes TCP. Never raises for network conditions.
    """
    p = params or AssociateParams()
    overrides: dict[str, Any] = {}
    if max_pdu_size != 65000:  # the default: keep whatever ``params`` says
        overrides["max_pdu_size"] = max_pdu_size
    if calling_ap_title is not None:
        overrides["calling_ap_title"] = calling_ap_title or None
    if called_ap_title is not None:
        overrides["called_ap_title"] = called_ap_title or None
    if overrides:
        p = AssociateParams(**{**p.__dict__, **overrides})
    t_start = time.monotonic()
    st = _Assoc()

    def done(ended_by: str | None = None, **kw: Any) -> AssociationProbe:
        duration = time.monotonic() - t_start
        return AssociationProbe(
            host, port, local_ip, st.outcome, st.failed_layer, st.steps, duration, st.initiate, st.acse_diag, st.acse,
            ended_by=ended_by, request=p, **kw,
        )  # fmt: skip

    sock, tcp = open_tcp(host, port, timeout, local_ip)
    st.steps.append(tcp)
    if sock is None:
        st.outcome, st.failed_layer = tcp.outcome, "network"
        return done()
    stream, cotp, cc = _cotp_exchange(sock, p, timeout)
    st.steps.append(cotp)
    if cc is None:
        st.fail("transport", cotp.outcome)
        stream.close()
        return done("server-closed" if cotp.outcome == "tcp-closed-immediately" else "client-closed")
    tpdu_size = min(cc.tpdu_size or 128, iso.tpdu_size_bytes(p.tpdu_size_code))

    # --- session CONNECT with the whole association request -------------------------------
    t0 = time.monotonic()
    sent: list[bytes] = []
    received: list[bytes] = []

    def io(**extra: Any) -> dict[str, Any]:
        return {"sent": [_hex(b) for b in sent], "received": [_hex(b) for b in received], **extra}

    try:
        _send_dt(stream, iso.association_request(p), tpdu_size, sent)
        pdu, payload = stream.recv_cotp(timeout, received)
    except _Timeout:
        detail = (
            f"no answer to the association request (session CONNECT / AARQ / initiate) within {timeout:g} s"
        )
        st.add("iso-session", False, "initiate-no-response", t0, detail, **io())
        stream.close()
        return done("client-closed")
    except _Closed as e:
        detail = f"{e.text} after the association request, without an answer (COTP was accepted)"
        st.add("iso-session", False, "initiate-no-response", t0, detail, **io(reset=e.reset))
        stream.close()
        return done("server-closed")
    except IsoError as e:
        received.append(bytes(stream.buf))
        st.add("iso-session", False, "invalid-response", t0, f"answer not understood ({e})", **io())
        stream.close()
        return done("client-closed")
    if pdu.code != iso.COTP_DT:
        detail = f"COTP {pdu.kind} received in reply to the association request"
        if pdu.reason is not None:
            detail += f", reason 0x{pdu.reason:02x} {pdu.reason_name}"
        st.add("cotp-disconnect", False, "cotp-rejected", t0, detail, **io(pdu=pdu.to_json()))
        stream.close()
        return done("server-closed")
    try:
        spdu = iso.parse_spdu(payload)
    except IsoError as e:
        st.add("iso-session", False, "invalid-response", t0, f"session PDU not understood ({e})", **io())
        stream.close()
        return done("client-closed")
    raw = io(spdu=spdu.to_json())
    abbr = {iso.SPDU_AC: "AC", iso.SPDU_RF: "RF", iso.SPDU_AB: "AB"}.get(spdu.si, "?")
    head = f"session {spdu.name} ({abbr} SPDU, SI {spdu.si})"
    if spdu.si == iso.SPDU_AC:
        st.add("iso-session", True, "accepted", t0, f"{head}, protocol version {spdu.version}", **raw)
    elif spdu.si == iso.SPDU_RF:
        detail = f"{head}, reason 0x{spdu.refuse_reason or 0:02x} {spdu.refuse_reason_name}"
        st.add("iso-session", False, "session-refused", t0, detail, **raw)
    elif spdu.si == iso.SPDU_AB:
        td = spdu.transport_disconnect
        detail = head + (f", transport-disconnect 0x{td:02x}" if td is not None else "")
        st.add("iso-session", False, "session-aborted", t0, detail, **raw)
    else:
        detail = f"unexpected session {spdu.name} SPDU (SI {spdu.si}) in reply to CONNECT"
        st.add("iso-session", False, "invalid-response", t0, detail, **raw)
        stream.close()
        return done("client-closed")

    if spdu.user_data:
        ppdu = st.presentation(spdu, t0)
        acse = st.acse_layer(ppdu, t0) if ppdu is not None else None
        if acse is not None:
            st.mms_layer(acse, t0)
    elif spdu.si == iso.SPDU_AC:
        st.add("presentation", False, "invalid-response", t0, "session ACCEPT without presentation user data")

    accepted = spdu.si == iso.SPDU_AC and st.failed_layer is None and st.initiate is not None
    if not accepted:
        if st.failed_layer is None:
            st.fail("mms", "invalid-response")
        if spdu.si == iso.SPDU_AC:  # the server thinks it may be connected: abort it
            _end(stream, st, timeout, release=False)
            return done("abort")
        stream.close()
        return done("server-closed")

    st.outcome, st.failed_layer = "accepted", None
    # --- did the server drop us right after accepting? -----------------------------------
    t0 = time.monotonic()
    closed, how, extra = stream.wait_closed(linger_s)
    if closed:
        after = time.monotonic() - stream.t_open
        detail = f"association accepted, then {how} within {linger_s:g} s"
        st.add("linger", False, "accepted-then-closed", t0, detail, received=_hex(extra))
        stream.close()
        return done("server-closed", closed_after_accept=True, closed_after_s=round(after, 6))
    detail = f"connection still open {linger_s:g} s after acceptance"
    st.add("linger", True, "still-open", t0, detail, **({"unexpected_data": _hex(extra)} if extra else {}))
    ended = _end(stream, st, timeout, release=release)
    return done(ended, closed_after_accept=False)


def _end(stream: _Stream, st: _Assoc, timeout: float, *, release: bool) -> str:
    """End an accepted association: conclude + release, or abort. Returns how it ended."""
    t0 = time.monotonic()
    sent: list[bytes] = []
    received: list[bytes] = []
    steps: list[str] = []
    ok = False
    try:
        if release:
            _send_dt(stream, iso.conclude_request(), 8192, sent)
            _, payload = stream.recv_cotp(timeout, received)
            mms = iso.parse_mms(iso.parse_presentation_data(iso.parse_spdu(payload).user_data)[0][1])
            steps.append(f"conclude -> {mms.name}")
            if mms.number == 12:
                _send_dt(stream, iso.release_request(), 8192, sent)
                pdu, payload = stream.recv_cotp(timeout, received)
                spdu = iso.parse_spdu(payload)
                rlre = ""
                if spdu.user_data:
                    acse = iso.parse_acse(iso.parse_presentation_data(spdu.user_data)[0][1])
                    rlre = f" ({acse.kind})"
                steps.append(f"release (FINISH / RLRQ) -> {spdu.name}{rlre}")
                ok = spdu.si == iso.SPDU_DN
    except (_Timeout, _Closed, IsoError, IndexError) as e:
        steps.append(f"release not confirmed: {type(e).__name__.strip('_')} {e}")
    how = "release"
    if not ok:
        try:
            pkt = iso.tpkt(iso.cotp_dt(iso.abort_request()))
            sent.append(pkt)
            stream.send(pkt)
            steps.append("abort (ABORT SPDU / ARU / ABRT) sent")
            how = "abort"
        except _Closed as e:
            steps.append(f"abort not sent: {e.text}")
            how = "server-closed"
    closed, text, _ = stream.wait_closed(min(timeout, 1.0))
    steps.append(f"server {text}" if closed else "closing our side")
    stream.close()
    # Recorded directly (not via st.add): a failed release does not change the classification.
    raw = {"sent": [_hex(b) for b in sent], "received": [_hex(b) for b in received]}
    duration = time.monotonic() - t0
    st.steps.append(
        ProbeResult("mms", "release", ok, "released" if ok else how, duration, "; ".join(steps), None, raw)
    )
    return how


# ---------------------------------------------------------------------------
# Convenience: the probe sequence used by diagnose (DIA-1 network -> MMS initiate)
# ---------------------------------------------------------------------------
# Association outcomes after which the TLS port is worth checking (DIA-6).
TLS_CHECK_OUTCOMES = frozenset(
    {
        "tcp-refused",
        "tcp-timeout",
        "tcp-closed-immediately",
        "cotp-rejected",
        "cotp-invalid-response",
        "cotp-no-response",
    }
)


@dataclass
class LayeredProbes:
    """Everything :func:`run_layered_probes` found, ready for :func:`classify.classify_association`."""

    host: str
    port: int
    icmp: ProbeResult | None
    tcp: ProbeResult
    association: AssociationProbe | None
    tls: ProbeResult | None

    @property
    def cotp(self) -> ProbeResult | None:
        return self.association.cotp if self.association else None

    def results(self) -> list[ProbeResult]:
        """All probe results in layer order (association steps expanded)."""
        out = [r for r in (self.icmp, self.tcp) if r is not None]
        if self.association:
            out += [s for s in self.association.steps if not s.name.startswith("tcp-")]
        if self.tls:
            out.append(self.tls)
        return out

    def to_json(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "port": self.port,
            "icmp": self.icmp.to_json() if self.icmp else None,
            "tcp": self.tcp.to_json(),
            "association": self.association.to_json() if self.association else None,
            "tls": self.tls.to_json() if self.tls else None,
        }


def run_layered_probes(
    host: str,
    port: int = 102,
    *,
    timeout: float = DEFAULT_TIMEOUT_S,
    local_ip: str | None = None,
    icmp: bool = True,
    tls: bool = True,
    params: AssociateParams | None = None,
    linger_s: float = DEFAULT_LINGER_S,
) -> LayeredProbes:
    """ICMP (informational) -> TCP -> full association; TCP 3782 when 102 looks closed (DIA-6).

    Uses two TCP connections to the MMS port (a bare connect, then the association attempt) and
    one to 3782 only when needed.
    """
    icmp_r = ping(host, min(timeout, 2.0), local_ip=local_ip) if icmp else None
    tcp_r = tcp_connect(host, port, timeout, local_ip)
    assoc = None
    if tcp_r.ok:
        assoc = iso_associate(host, port, timeout, local_ip, params=params, linger_s=linger_s)
    tls_r = None
    check = (tcp_r.ok is False and tcp_r.outcome in TLS_CHECK_OUTCOMES) or (
        assoc is not None and assoc.outcome in TLS_CHECK_OUTCOMES
    )
    if tls and check and port != TLS_PORT:
        tls_r = tls_port_open(host, timeout, local_ip=local_ip)
    return LayeredProbes(host, port, icmp_r, tcp_r, assoc, tls_r)
