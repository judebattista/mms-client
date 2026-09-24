"""Layered connectivity probes below libiec61850 (DIA-3, DIA-6).

libiec61850 reports every failed association attempt as one generic code (see
:mod:`mms_client.diagnosis.classify` for the observed behaviour), so these probes speak the lower
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

import errno
import hashlib
import math
import os
import re
import shutil
import socket
import ssl
import subprocess
import time
import warnings
from dataclasses import dataclass, field
from typing import Any

from mms_client import codes
from mms_client.codes import ErrorInfo

from . import iso
from .iso import AssociateParams, IsoError

LAYERS = ("network", "transport", "session", "mms")
TLS_PORT = 3782
DEFAULT_TIMEOUT_S = 3.0
DEFAULT_LINGER_S = 1.0


@dataclass
class ProbeResult:
    """The outcome of one probe (or one step of the association probe).

    ``layer`` is the layer the probe tests (for the ``iso-association`` summary: the layer where
    the attempt stopped). ``ok`` is None when the probe could not decide (e.g. ping not permitted).
    For association-related probes ``outcome`` is a key of :data:`codes.ASSOCIATION_OUTCOMES`.
    """

    layer: str
    name: str
    ok: bool | None
    outcome: str
    duration_s: float
    detail: str
    error: ErrorInfo | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "layer": self.layer,
            "name": self.name,
            "ok": self.ok,
            "outcome": self.outcome,
            "duration_s": round(self.duration_s, 6),
            "detail": self.detail,
            "error": self.error.to_json() if self.error else None,
            "raw": _jsonable(self.raw),
        }


def _jsonable(v: Any) -> Any:
    if isinstance(v, bytes):
        return v.hex()
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, list | tuple):
        return [_jsonable(x) for x in v]
    if hasattr(v, "to_json"):
        return v.to_json()
    return v


def _hex(data: bytes) -> str:
    return data.hex(" ")


# ---------------------------------------------------------------------------
# ICMP
# ---------------------------------------------------------------------------
_NOT_CONCLUSIVE = (
    "not conclusive on its own: many IEDs do not answer ping; the TCP probe decides reachability"
)


def ping(host: str, timeout: float = 1.0, *, local_ip: str | None = None) -> ProbeResult:
    """One ICMP echo using the system ``ping`` (no raw sockets, no privileges).

    ``ok`` is None when ping is missing or not permitted. A missing reply is reported, but callers
    must not treat it as fatal (many IEDs drop ICMP).
    """
    t0 = time.monotonic()
    exe = shutil.which("ping")
    if exe is None:
        return ProbeResult(
            "network", "icmp", None, "unavailable", 0.0, "ICMP not tested: no 'ping' command found"
        )
    wait = max(1, math.ceil(timeout))
    cmd = [exe, "-n", "-c", "1", "-W", str(wait)]
    if local_ip:
        cmd += ["-I", local_ip]
    cmd.append(host)
    env = dict(os.environ, LC_ALL="C", LANG="C")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=wait + 3, env=env, check=False)
    except subprocess.TimeoutExpired:
        return ProbeResult(
            "network",
            "icmp",
            None,
            "error",
            time.monotonic() - t0,
            "ICMP not tested: ping did not finish in time",
        )
    except OSError as e:
        return ProbeResult(
            "network", "icmp", None, "unavailable", time.monotonic() - t0, f"ICMP not tested: {e}"
        )
    dt = time.monotonic() - t0
    out = (proc.stdout + "\n" + proc.stderr).strip()
    raw = {"command": " ".join(cmd), "returncode": proc.returncode, "output": out[-2000:]}
    low = out.lower()
    if proc.returncode == 0:
        m = re.search(r"time[=<]([\d.]+)\s*ms", out)
        rtt = float(m.group(1)) if m else None
        raw["rtt_ms"] = rtt
        rtt_s = f" in {rtt} ms" if rtt is not None else ""
        return ProbeResult(
            "network", "icmp", True, "reply", dt, f"ICMP echo reply from {host}{rtt_s}", raw=raw
        )
    if "not permitted" in low or "permission denied" in low or "operation not permitted" in low:
        return ProbeResult(
            "network",
            "icmp",
            None,
            "not-permitted",
            dt,
            f"ICMP not tested: ping not permitted here ({out[:120]})",
            raw=raw,
        )
    if "unknown host" in low or "name or service not known" in low or "temporary failure in name" in low:
        return ProbeResult(
            "network", "icmp", None, "unknown-host", dt, f"ICMP not tested: cannot resolve {host}", raw=raw
        )
    if "unreachable" in low:
        return ProbeResult(
            "network",
            "icmp",
            False,
            "host-unreachable",
            dt,
            f"ICMP: destination unreachable reported for {host} (ping exit {proc.returncode}); {_NOT_CONCLUSIVE}",
            raw=raw,
        )
    if proc.returncode == 1:
        return ProbeResult(
            "network",
            "icmp",
            False,
            "no-reply",
            dt,
            f"ICMP: no echo reply from {host} within {wait} s; {_NOT_CONCLUSIVE}",
            raw=raw,
        )
    return ProbeResult(
        "network",
        "icmp",
        None,
        "error",
        dt,
        f"ICMP not tested: ping exit {proc.returncode} ({out[:120]})",
        raw=raw,
    )


# ---------------------------------------------------------------------------
# TCP
# ---------------------------------------------------------------------------
_UNREACHABLE_ERRNOS = {errno.EHOSTUNREACH, errno.ENETUNREACH, errno.EHOSTDOWN, errno.ENETDOWN}


def _errno_text(e: OSError) -> str:
    if e.errno is None:
        return str(e)
    return f"errno {e.errno} {errno.errorcode.get(e.errno, '?')}"


def _open_tcp(
    host: str, port: int, timeout: float, local_ip: str | None
) -> tuple[socket.socket | None, ProbeResult]:
    """Connect; return the socket (or None) and the ``tcp-<port>`` ProbeResult."""
    name = f"tcp-{port}"
    raw: dict[str, Any] = {"host": host, "port": port, "local_ip": local_ip, "timeout_s": timeout}
    t0 = time.monotonic()
    try:
        infos = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
        addr = infos[0][4]
    except (OSError, IndexError) as e:
        return None, ProbeResult(
            "network", name, None, "unknown", time.monotonic() - t0, f"cannot resolve {host!r}: {e}", raw=raw
        )
    raw["address"] = addr[0]
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if local_ip:
            try:
                sock.bind((local_ip, 0))
            except OSError as e:
                sock.close()
                raw["errno"] = e.errno
                return None, ProbeResult(
                    "network",
                    name,
                    None,
                    "unknown",
                    time.monotonic() - t0,
                    f"cannot bind to local address {local_ip} ({_errno_text(e)}): the address is not configured on this "
                    "host, so the device was not contacted",
                    error=codes.tool("local-address-missing"),
                    raw=raw,
                )
        sock.settimeout(timeout)
        sock.connect(addr)
    except TimeoutError:
        sock.close()
        dt = time.monotonic() - t0
        return None, ProbeResult(
            "network",
            name,
            False,
            "tcp-timeout",
            dt,
            f"TCP {addr[0]}:{port}: no answer to the connection request within {timeout:g} s (SYN unanswered: host "
            "down, filtered, or on an unreachable network)",
            error=codes.association("tcp-timeout"),
            raw=raw,
        )
    except ConnectionRefusedError as e:
        sock.close()
        raw["errno"] = e.errno
        return None, ProbeResult(
            "network",
            name,
            False,
            "tcp-refused",
            time.monotonic() - t0,
            f"TCP {addr[0]}:{port}: connection refused (RST, {_errno_text(e)}): nothing listens on this port",
            error=codes.association("tcp-refused"),
            raw=raw,
        )
    except OSError as e:
        sock.close()
        raw["errno"] = e.errno
        dt = time.monotonic() - t0
        if e.errno in _UNREACHABLE_ERRNOS:
            return None, ProbeResult(
                "network",
                name,
                False,
                "host-unreachable",
                dt,
                f"TCP {addr[0]}:{port}: host unreachable ({_errno_text(e)}; no ARP reply or no route)",
                error=codes.association("host-unreachable"),
                raw=raw,
            )
        return None, ProbeResult(
            "network",
            name,
            False,
            "unknown",
            dt,
            f"TCP {addr[0]}:{port}: connect failed ({_errno_text(e)})",
            error=codes.association("unknown"),
            raw=raw,
        )
    dt = time.monotonic() - t0
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    local = sock.getsockname()
    raw["local_address"] = f"{local[0]}:{local[1]}"
    return sock, ProbeResult(
        "network",
        name,
        True,
        "accepted",
        dt,
        f"TCP {addr[0]}:{port}: connection accepted in {dt * 1000:.1f} ms (from {local[0]}:{local[1]})",
        raw=raw,
    )


def tcp_connect(
    host: str, port: int = 102, timeout: float = DEFAULT_TIMEOUT_S, local_ip: str | None = None
) -> ProbeResult:
    """TCP connect and close. Outcomes: accepted, tcp-refused, tcp-timeout, host-unreachable (or unknown)."""
    sock, result = _open_tcp(host, port, timeout, local_ip)
    if sock is not None:
        sock.close()
    return result


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
    sock, tcp = _open_tcp(host, port, timeout, local_ip)
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
            raise _Closed(True, f"send failed ({_errno_text(e)})") from e

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
            raise _Closed(True, f"receive failed ({_errno_text(e)})") from e
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
    sent, received = [cr], []
    raw: dict[str, Any] = {"sent": [_hex(b) for b in sent], "received": []}
    t0 = time.monotonic()

    def result(ok: bool, outcome: str, detail: str, pdu: iso.CotpPdu | None = None) -> ProbeResult:
        raw["received"] = [_hex(b) for b in received]
        if pdu is not None:
            raw["pdu"] = pdu.to_json()
        err = None if ok else codes.association(outcome)
        return ProbeResult("transport", "cotp", ok, outcome, time.monotonic() - t0, detail, err, raw)

    try:
        stream.send(cr)
        pdu, _ = stream.recv_cotp(timeout, received)
    except _Timeout:
        return (
            stream,
            result(
                False,
                "cotp-no-response",
                f"no answer to the COTP connection request (CR) within {timeout:g} s",
            ),
            None,
        )
    except _Closed as e:
        after = time.monotonic() - stream.t_open
        raw["closed_after_s"] = round(after, 6)
        raw["reset"] = e.reset
        if stream.buf:
            received.append(bytes(stream.buf))
        return (
            stream,
            result(
                False,
                "tcp-closed-immediately",
                f"TCP accepted, then {e.text} {after:.3f} s after connect, before any COTP response",
            ),
            None,
        )
    except IsoError as e:
        received.append(bytes(stream.buf))
        raw["looks_like_tls"] = iso.looks_like_tls(bytes(stream.buf[:3]))
        extra = " — the reply looks like TLS" if raw["looks_like_tls"] else ""
        return (
            stream,
            result(False, "cotp-invalid-response", f"COTP response not understood ({e}){extra}"),
            None,
        )
    if pdu.code == iso.COTP_CC:
        size = pdu.tpdu_size
        return (
            stream,
            result(
                True,
                "accepted",
                f"COTP CC: connection confirmed (class {pdu.class_option}, TPDU size {size or 'not stated'}, "
                f"src-ref 0x{pdu.src_ref or 0:04x})",
                pdu,
            ),
            pdu,
        )
    if pdu.code == iso.COTP_DR:
        return (
            stream,
            result(
                False,
                "cotp-rejected",
                f"COTP DR: disconnect request, reason 0x{pdu.reason:02x} {pdu.reason_name}",
                pdu,
            ),
            None,
        )
    if pdu.code == iso.COTP_ER:
        bad = pdu.params.get(iso.COTP_PARAM_INVALID_TPDU, b"")
        return (
            stream,
            result(
                False,
                "cotp-rejected",
                f"COTP ER: TPDU error, cause {pdu.reason} {pdu.reason_name}"
                + (f", rejected TPDU header {_hex(bad)}" if bad else ""),
                pdu,
            ),
            None,
        )
    return (
        stream,
        result(False, "cotp-invalid-response", f"unexpected COTP {pdu.kind} TPDU in reply to CR", pdu),
        None,
    )


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
    sock, tcp = _open_tcp(host, port, timeout, local_ip)
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


class _Assoc:
    """State of one association attempt (keeps iso_associate readable)."""

    def __init__(self, host: str, port: int, local_ip: str | None, params: AssociateParams) -> None:
        self.host, self.port, self.local_ip, self.params = host, port, local_ip, params
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
        layer: str,
        name: str,
        ok: bool | None,
        outcome: str,
        t0: float,
        detail: str,
        error: ErrorInfo | None = None,
        **raw: Any,
    ) -> ProbeResult:
        if ok is False and error is None and outcome in codes.ASSOCIATION_OUTCOMES:
            error = codes.association(outcome)
        r = ProbeResult(layer, name, ok, outcome, time.monotonic() - t0, detail, error, raw)
        self.steps.append(r)
        if ok is False:
            self.fail(layer, outcome)
        return r

    # -- layers above the session ---------------------------------------------------------
    def presentation(self, spdu: iso.Spdu, t0: float) -> iso.PresentationResponse | None:
        abort = spdu.si == iso.SPDU_AB
        try:
            ppdu = iso.parse_presentation(spdu.user_data, abort=abort)
        except IsoError as e:
            self.add("session", "presentation", False, "invalid-response", t0, f"presentation PDU not understood ({e})",
                     received=_hex(spdu.user_data))  # fmt: skip
            return None
        info = ppdu.to_json()
        if ppdu.kind == "CPA":
            results = ", ".join(
                iso.PRESENTATION_CONTEXT_RESULTS.get(r, str(r)) for r, _ in ppdu.context_results
            )
            bad = [r for r, _ in ppdu.context_results if r != 0]
            if bad:
                self.add("session", "presentation", False, "presentation-rejected", t0,
                         f"presentation CPA, but a context was rejected: [{results}]", pdu=info)  # fmt: skip
            else:
                self.add("session", "presentation", True, "accepted", t0,
                         f"presentation CPA: contexts [{results}]", pdu=info)  # fmt: skip
        elif ppdu.kind == "CPR":
            reason = (
                f"provider-reason {ppdu.provider_reason} {ppdu.provider_reason_name}"
                if ppdu.provider_reason is not None
                else "no provider-reason (rejected by the user: see ACSE)"
            )
            self.add(
                "session",
                "presentation",
                False,
                "presentation-rejected",
                t0,
                f"presentation CPR: {reason}",
                pdu=info,
            )
        elif ppdu.kind == "ARP":
            name = iso.PRESENTATION_ABORT_REASONS.get(ppdu.abort_reason or 0, "?")
            self.add("session", "presentation", False, "session-aborted", t0,
                     f"presentation provider abort (ARP), reason {ppdu.abort_reason} {name}", pdu=info)  # fmt: skip
        else:  # ARU
            self.add(
                "session", "presentation", None, "user-abort", t0, "presentation user abort (ARU)", pdu=info
            )
        return ppdu

    def acse_layer(self, ppdu: iso.PresentationResponse, t0: float) -> iso.AcsePdu | None:
        apdu = ppdu.apdu(iso.ACSE_CONTEXT_ID)
        if apdu is None:
            if ppdu.kind == "CPA":
                self.add(
                    "mms", "acse", False, "invalid-response", t0, "presentation CPA without an ACSE AARE"
                )
            return None
        try:
            acse = iso.parse_acse(apdu)
        except IsoError as e:
            self.add(
                "mms",
                "acse",
                False,
                "invalid-response",
                t0,
                f"ACSE PDU not understood ({e})",
                received=_hex(apdu),
            )
            return None
        self.acse = acse
        info = acse.to_json()
        if acse.kind == "ABRT":
            diag = (
                f", diagnostic {acse.abort_diagnostic} {acse.abort_diagnostic_name}"
                if acse.abort_diagnostic
                else ""
            )
            src = iso.ACSE_ABORT_SOURCES.get(acse.abort_source or 0, "?")
            self.add(
                "mms",
                "acse",
                False,
                "acse-aborted",
                t0,
                f"ACSE ABRT from {src} ({acse.abort_source}){diag}",
                pdu=info,
            )
            return acse
        if acse.kind != "AARE":
            self.add(
                "mms",
                "acse",
                False,
                "invalid-response",
                t0,
                f"unexpected ACSE {acse.kind} in reply to AARQ",
                pdu=info,
            )
            return acse
        diag_text = ""
        if acse.diagnostic is not None:
            diag_text = f", diagnostic acse-{acse.diagnostic_source} {acse.diagnostic} {acse.diagnostic_name}"
            if acse.diagnostic_source == "service-user":
                self.acse_diag = codes.info(codes.Domain.ACSE_DIAG, acse.diagnostic)
        responder = f", responding AP title {acse.responding_ap_title}" if acse.responding_ap_title else ""
        if acse.responding_ae_qualifier is not None:
            responder += f" AE qualifier {acse.responding_ae_qualifier}"
        text = f"ACSE AARE result {acse.result} {acse.result_name}{diag_text}{responder}"
        if acse.result == 0:
            self.add("mms", "acse", True, "accepted", t0, text, pdu=info)
        else:
            outcome = {1: "acse-rejected-permanent", 2: "acse-rejected-transient"}.get(
                acse.result or -1, "invalid-response"
            )
            err = self.acse_diag if self.acse_diag and self.acse_diag.code else None
            self.add("mms", "acse", False, outcome, t0, text, err, pdu=info)
        return acse

    def mms_layer(self, acse: iso.AcsePdu, t0: float) -> None:
        if acse.kind != "AARE":
            return
        if acse.user_information is None:
            if acse.result == 0:
                self.add(
                    "mms",
                    "mms-initiate",
                    False,
                    "invalid-response",
                    t0,
                    "AARE accepted without an MMS initiate-ResponsePDU",
                )
            return
        try:
            pdu = iso.parse_mms(acse.user_information)
        except IsoError as e:
            self.add("mms", "mms-initiate", False, "invalid-response", t0, f"MMS PDU not understood ({e})",
                     received=_hex(acse.user_information))  # fmt: skip
            return
        info = pdu.to_json()
        info["hex"] = _hex(acse.user_information)
        if pdu.number == 9 and pdu.initiate:
            p = pdu.initiate
            self.initiate = p
            services = p.supported_services()
            text = (
                f"MMS initiate-ResponsePDU: localDetailCalled (max PDU) {p.max_pdu_size}, "
                f"maxServOutstanding calling {p.max_serv_outstanding_calling} / called {p.max_serv_outstanding_called}, "
                f"nesting level {p.data_structure_nesting_level}, version {p.version}, "
                f"parameter CBB {' '.join(p.parameter_cbb_names()) or '-'} ({p.parameter_cbb.hex()}), "
                f"{len(services)} services ({p.services_supported.hex()})"
            )
            if acse.result == 0:
                self.add("mms", "mms-initiate", True, "accepted", t0, text, pdu=info)
            else:
                self.add("mms", "mms-initiate", None, "response-in-rejection", t0, text, pdu=info)
        elif pdu.number == 10:
            text = (
                f"MMS initiate-ErrorPDU: error class {pdu.error_class} {pdu.error_class_name}, "
                f"code {pdu.error_code} {pdu.error_code_name}"
            )
            self.add("mms", "mms-initiate", False, "initiate-error", t0, text, pdu=info)
        else:
            self.add(
                "mms",
                "mms-initiate",
                False,
                "invalid-response",
                t0,
                f"unexpected MMS {pdu.name} in AARE",
                pdu=info,
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
    st = _Assoc(host, port, local_ip, p)

    def done(ended_by: str | None = None, **kw: Any) -> AssociationProbe:
        return AssociationProbe(
            host, port, local_ip, st.outcome, st.failed_layer, st.steps, time.monotonic() - t_start,
            st.initiate, st.acse_diag, st.acse, ended_by=ended_by, request=p, **kw,
        )  # fmt: skip

    sock, tcp = _open_tcp(host, port, timeout, local_ip)
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
    try:
        _send_dt(stream, iso.association_request(p), tpdu_size, sent)
        pdu, payload = stream.recv_cotp(timeout, received)
    except _Timeout:
        st.add("session", "iso-session", False, "initiate-no-response", t0,
               f"no answer to the association request (session CONNECT / AARQ / initiate) within {timeout:g} s",
               sent=[_hex(b) for b in sent])  # fmt: skip
        stream.close()
        return done("client-closed")
    except _Closed as e:
        st.add("session", "iso-session", False, "initiate-no-response", t0,
               f"{e.text} after the association request, without an answer (COTP was accepted)",
               sent=[_hex(b) for b in sent], received=[_hex(b) for b in received], reset=e.reset)  # fmt: skip
        stream.close()
        return done("server-closed")
    except IsoError as e:
        st.add("session", "iso-session", False, "invalid-response", t0, f"answer not understood ({e})",
               sent=[_hex(b) for b in sent], received=[_hex(b) for b in received] + [_hex(bytes(stream.buf))])  # fmt: skip
        stream.close()
        return done("client-closed")
    raw_io = {"sent": [_hex(b) for b in sent], "received": [_hex(b) for b in received]}
    if pdu.code != iso.COTP_DT:
        reason = f", reason 0x{pdu.reason:02x} {pdu.reason_name}" if pdu.reason is not None else ""
        st.add("transport", "cotp-disconnect", False, "cotp-rejected", t0,
               f"COTP {pdu.kind} received in reply to the association request{reason}", pdu=pdu.to_json(), **raw_io)  # fmt: skip
        stream.close()
        return done("server-closed")
    try:
        spdu = iso.parse_spdu(payload)
    except IsoError as e:
        st.add(
            "session",
            "iso-session",
            False,
            "invalid-response",
            t0,
            f"session PDU not understood ({e})",
            **raw_io,
        )
        stream.close()
        return done("client-closed")
    sinfo = spdu.to_json()
    if spdu.si == iso.SPDU_AC:
        st.add("session", "iso-session", True, "accepted", t0,
               f"session ACCEPT (AC SPDU, SI {spdu.si}), protocol version {spdu.version}", spdu=sinfo, **raw_io)  # fmt: skip
    elif spdu.si == iso.SPDU_RF:
        st.add("session", "iso-session", False, "session-refused", t0,
               f"session REFUSE (RF SPDU, SI {spdu.si}), reason 0x{spdu.refuse_reason or 0:02x} {spdu.refuse_reason_name}",
               spdu=sinfo, **raw_io)  # fmt: skip
    elif spdu.si == iso.SPDU_AB:
        td = spdu.transport_disconnect
        st.add("session", "iso-session", False, "session-aborted", t0,
               f"session ABORT (AB SPDU, SI {spdu.si})" + (f", transport-disconnect 0x{td:02x}" if td is not None else ""),
               spdu=sinfo, **raw_io)  # fmt: skip
    else:
        st.add("session", "iso-session", False, "invalid-response", t0,
               f"unexpected session {spdu.name} SPDU (SI {spdu.si}) in reply to CONNECT", spdu=sinfo, **raw_io)  # fmt: skip
        stream.close()
        return done("client-closed")

    if spdu.user_data:
        ppdu = st.presentation(spdu, t0)
        if ppdu is not None:
            acse = st.acse_layer(ppdu, t0)
            if acse is not None:
                st.mms_layer(acse, t0)
    elif spdu.si == iso.SPDU_AC:
        st.add(
            "session",
            "presentation",
            False,
            "invalid-response",
            t0,
            "session ACCEPT without presentation user data",
        )

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
        st.add("mms", "linger", False, "accepted-then-closed", t0,
               f"association accepted, then {how} within {linger_s:g} s", received=_hex(extra))  # fmt: skip
        stream.close()
        return done("server-closed", closed_after_accept=True, closed_after_s=round(after, 6))
    st.add("mms", "linger", True, "still-open", t0, f"connection still open {linger_s:g} s after acceptance",
           **({"unexpected_data": _hex(extra)} if extra else {}))  # fmt: skip
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
    st.steps.append(
        ProbeResult(
            "mms", "release", ok, "released" if ok else how, time.monotonic() - t0, "; ".join(steps), None,
            {"sent": [_hex(b) for b in sent], "received": [_hex(b) for b in received]},
        )
    )  # fmt: skip
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
