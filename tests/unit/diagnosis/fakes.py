"""Reference captures and tiny scripted ISO servers for the diagnosis tests.

The ``CAP_*`` byte strings were captured on loopback by proxying the adapter's ``IedClient``
(libiec61850 1.6.1 client) to the simulated IED in ``tests/sim`` (libiec61850 1.6.1 server).
The probes' requests are checked against them byte for byte, and the fake servers replay the
server side where a real exchange is needed.
"""

from __future__ import annotations

import socket
import struct
import threading
from collections.abc import Callable

from mms_client.diagnosis import ber, iso

# --- client -> server (libiec61850 client) ---------------------------------------------------
CAP_CR = bytes.fromhex("0300001611e00000000100c0010dc2020001c1020001")
CAP_CN = bytes.fromhex(
    "030000bb02f0800db20506130100160102140200023302000134020001c19c318199a003800101a281918104000000018204"
    "00000001a423300f0201010604520100013004060251013010020103060528ca22020130040602510161 5e305c020101a057"
    "6055a10706052 8ca220203a20706052901876701a303020 10ca606060429018767a703020 10cbe2f282d020103a028a8"
    "26800300fde8810105820105830 10aa416800101810305f100820c03ee1c00000408000079ef18".replace(" ", "")
)
CAP_CONCLUDE_REQ = bytes.fromhex("0300001602f08001000100610930070201 03a0028b00".replace(" ", ""))
CAP_FINISH = bytes.fromhex("0300001902f0800910c10e610c300a020101a00562038001 00".replace(" ", ""))
CAP_ABORT = bytes.fromhex("0300001e02f0801915110 10bc110a00e610c300a020101a00564038001 00".replace(" ", ""))

# --- server -> client (libiec61850 server, the simulated IED) -------------------------------
CAP_CC = bytes.fromhex("0300001611d00001000100c0010dc2020001c1020001")
CAP_AC = bytes.fromhex(
    "0300008f02f0800e8605061301001601021402000234020001c17431 72a003800101a26b830400000001a512300780010081"
    "0251013007800100810251 01614f304d020101a0486146a10706052 8ca220203a203020100a305a103020100be2f282d02"
    "0103a028a926800300fde8810105820105830 10aa416800101810305f100820c03ee1c00000002000040ed18".replace(
        " ", ""
    )
)
CAP_CONCLUDE_RESP = bytes.fromhex("0300001602f08001000100610930070201 03a0028c00".replace(" ", ""))
CAP_DISCONNECT = bytes.fromhex("0300001602f0800a0dc10b6109300702 0101a0026300".replace(" ", ""))
CAP_IDENTIFY_RESP_SERVICES = bytes.fromhex("ee1c00000002000040ed18")


# --- building server responses ----------------------------------------------------------------
def spdu(si: int, params: bytes) -> bytes:
    return iso._spdu(si, params)


def sparam(code: int, value: bytes) -> bytes:
    return iso._session_param(code, value)


def dt(payload: bytes, eot: bool = True) -> bytes:
    """One TPKT-framed COTP DT."""
    return iso.tpkt(iso.cotp_dt(payload, eot=eot))


def cotp_dr(reason: int) -> bytes:
    return iso.tpkt(iso.cotp_dr(1, 1, reason))


def cotp_er(cause: int, bad: bytes = b"\xe0") -> bytes:
    body = bytes([0x70, 0x00, 0x01, cause, iso.COTP_PARAM_INVALID_TPDU, len(bad)]) + bad
    return iso.tpkt(bytes([len(body)]) + body)


def aare(result: int, diag: int | None = None, *, provider: bool = False, mms: bytes | None = None) -> bytes:
    body = ber.tlv(0xA1, ber.oid(iso.MMS_APPLICATION_CONTEXT))
    body += ber.tlv(0xA2, ber.integer(result))
    if diag is not None:
        body += ber.tlv(0xA3, ber.tlv(0xA2 if provider else 0xA1, ber.integer(diag)))
    if mms is not None:
        body += ber.tlv(0xBE, ber.tlv(0x28, ber.integer(3) + ber.tlv(0xA0, mms)))
    return ber.tlv(0x61, body)


def abrt(source: int = 0, diag: int | None = None) -> bytes:
    body = ber.integer(source, 0x80)
    if diag is not None:
        body += ber.integer(diag, 0x81)
    return ber.tlv(0x64, body)


def initiate_error(error_class: int, code: int) -> bytes:
    return ber.tlv(0xAA, ber.tlv(0xA0, ber.integer(code, 0x80 | error_class)))


def initiate_response(max_pdu: int = 65000) -> bytes:
    body = ber.integer(max_pdu, 0x80) + ber.integer(5, 0x81) + ber.integer(5, 0x82) + ber.integer(10, 0x83)
    detail = ber.integer(1, 0x80) + ber.bit_string(bytes.fromhex("f100"), 5, 0x81)
    detail += ber.bit_string(CAP_IDENTIFY_RESP_SERVICES, 3, 0x82)
    return ber.tlv(0xA9, body + ber.tlv(0xA4, detail))


def cpa(acse: bytes) -> bytes:
    ctx = ber.tlv(0x30, ber.integer(0, 0x80) + ber.tlv(0x81, bytes.fromhex("5101")))
    normal = (
        ber.tlv(0x83, b"\x00\x00\x00\x01") + ber.tlv(0xA5, ctx + ctx) + iso.presentation_user_data(1, acse)
    )
    return ber.tlv(0x31, ber.tlv(0xA0, ber.tlv(0x80, b"\x01")) + ber.tlv(0xA2, normal))


def cpr(provider_reason: int | None = None, acse: bytes | None = None) -> bytes:
    body = b""
    if provider_reason is not None:
        body += ber.integer(provider_reason, 0x8A)
    if acse is not None:
        body += iso.presentation_user_data(1, acse)
    return ber.tlv(0x30, body)


def accept(ppdu: bytes) -> bytes:
    """AC SPDU (as libiec61850 builds it) carrying ``ppdu``, framed as one DT."""
    params = sparam(5, bytes.fromhex("130100160102")) + sparam(20, b"\x00\x02") + sparam(52, b"\x00\x01")
    return dt(spdu(iso.SPDU_AC, params + sparam(iso.PGI_USER_DATA, ppdu)))


def refuse(reason: int, ppdu: bytes = b"") -> bytes:
    return dt(spdu(iso.SPDU_RF, sparam(iso.PI_REASON_CODE, bytes([reason]) + ppdu)))


def session_abort(ppdu: bytes = b"") -> bytes:
    params = sparam(iso.PI_TRANSPORT_DISCONNECT, b"\x03")
    if ppdu:
        params += sparam(iso.PGI_USER_DATA, ppdu)
    return dt(spdu(iso.SPDU_AB, params))


# --- scripted servers --------------------------------------------------------------------------
class FakeConn:
    """Server side of one accepted connection."""

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.buf = b""
        self.received: list[bytes] = []

    def recv_tpkt(self, timeout: float = 3.0) -> bytes | None:
        self.sock.settimeout(timeout)
        try:
            while True:
                if len(self.buf) >= 4:
                    n = (self.buf[2] << 8) | self.buf[3]
                    if len(self.buf) >= n:
                        pkt, self.buf = self.buf[:n], self.buf[n:]
                        self.received.append(pkt)
                        return pkt
                d = self.sock.recv(65536)
                if not d:
                    return None
                self.buf += d
        except OSError:
            return None

    def send(self, data: bytes) -> None:
        try:
            self.sock.sendall(data)
        except OSError:
            pass

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass

    def reset(self) -> None:
        """Close with RST instead of FIN."""
        try:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        except OSError:
            pass
        self.close()

    def wait_closed(self, timeout: float = 3.0) -> None:
        self.sock.settimeout(timeout)
        try:
            while self.sock.recv(65536):
                pass
        except OSError:
            pass


class FakeServer:
    """Listens on 127.0.0.1 and runs ``handler(conn)`` for each connection in its own thread."""

    def __init__(self, handler: Callable[[FakeConn], None]) -> None:
        self.handler = handler
        self.conns: list[FakeConn] = []
        self.errors: list[BaseException] = []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self._threads: list[threading.Thread] = []
        self._t = threading.Thread(target=self._accept, daemon=True)
        self._t.start()

    def _accept(self) -> None:
        while True:
            try:
                s, _ = self.sock.accept()
            except OSError:
                return
            conn = FakeConn(s)
            self.conns.append(conn)
            t = threading.Thread(target=self._run, args=(conn,), daemon=True)
            t.start()
            self._threads.append(t)

    def _run(self, conn: FakeConn) -> None:
        try:
            self.handler(conn)
        except BaseException as e:  # pragma: no cover - surfaced by the tests
            self.errors.append(e)
        finally:
            conn.close()

    def join(self, timeout: float = 3.0) -> None:
        for t in list(self._threads):
            t.join(timeout)

    def close(self) -> None:
        try:  # shutdown wakes the blocked accept(); close alone would leave the port listening
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass
        self._t.join(2)
        self.join()

    def __enter__(self) -> FakeServer:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def serve_release(conn: FakeConn) -> None:
    """Answer conclude and release like libiec61850 (captured responses); stop on anything else."""
    while True:
        pkt = conn.recv_tpkt()
        if pkt is None:
            return
        if pkt == CAP_CONCLUDE_REQ:
            conn.send(CAP_CONCLUDE_RESP)
        elif pkt == CAP_FINISH:
            conn.send(CAP_DISCONNECT)
            return
        else:  # abort or anything else: close
            return


def full_server(conn: FakeConn, response: bytes | None = None) -> None:
    """Accept COTP, send ``response`` (default: the captured AC), then serve conclude + release."""
    if conn.recv_tpkt() is None:
        return
    conn.send(CAP_CC)
    if conn.recv_tpkt() is None:
        return
    conn.send(response or CAP_AC)
    serve_release(conn)


def cotp_then(response: bytes, *, then_close: bool = True, linger: float = 0.0) -> Callable[[FakeConn], None]:
    """Handler: CC, then answer the association request with ``response`` and close."""

    def handler(conn: FakeConn) -> None:
        if conn.recv_tpkt() is None:
            return
        conn.send(CAP_CC)
        if conn.recv_tpkt() is None:
            return
        conn.send(response)
        if linger:  # record whatever the client sends next (e.g. an abort) until it closes
            while conn.recv_tpkt(linger) is not None:
                pass
        if not then_close:
            conn.wait_closed()

    return handler
