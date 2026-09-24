"""Layered probes against tiny scripted servers on loopback (outcomes the simulator cannot produce)."""

from __future__ import annotations

import shutil
import socket
import ssl
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest

from mms_client import codes
from mms_client.diagnosis import iso, probes
from mms_client.diagnosis.probes import iso_associate

from . import fakes as f
from .fakes import FakeServer

T = 0.5  # per-step timeout for fake servers


def closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def assoc(port: int, **kw):
    kw.setdefault("linger_s", 0.2)
    return iso_associate("127.0.0.1", port, T, **kw)


# --- TCP -----------------------------------------------------------------------------------------
def test_tcp_refused_and_accepted():
    r = probes.tcp_connect("127.0.0.1", closed_port(), T)
    assert (r.ok, r.outcome, r.layer, r.error) == (
        False,
        "tcp-refused",
        "network",
        codes.association("tcp-refused"),
    )
    assert "ECONNREFUSED" in r.detail
    with FakeServer(lambda c: c.wait_closed(1)) as srv:
        r = probes.tcp_connect("127.0.0.1", srv.port, T)
        assert (r.ok, r.outcome, r.name) == (True, "accepted", f"tcp-{srv.port}")
        assert r.raw["local_address"].startswith("127.0.0.1:")


def test_tcp_timeout_when_backlog_is_full():
    """A listener whose backlog is full drops SYNs: the connect times out (no network access needed)."""
    ls = socket.socket()
    ls.bind(("127.0.0.1", 0))
    ls.listen(0)
    fillers = []
    try:
        for _ in range(8):  # fill the accept queue (never accepted)
            s = socket.socket()
            s.setblocking(False)
            s.connect_ex(ls.getsockname())
            fillers.append(s)
        time.sleep(0.05)
        r = probes.tcp_connect("127.0.0.1", ls.getsockname()[1], 0.3)
        if r.outcome == "accepted":
            pytest.skip("kernel accepted despite the full backlog")
        assert (r.ok, r.outcome) == (False, "tcp-timeout")
    finally:
        for s in fillers:
            s.close()
        ls.close()


def test_bind_to_missing_local_address_is_undetermined():
    r = probes.tcp_connect("127.0.0.1", closed_port(), T, local_ip="203.0.113.77")
    assert r.ok is None and r.outcome == "unknown" and r.error == codes.tool("local-address-missing")
    a = iso_associate("127.0.0.1", closed_port(), T, local_ip="203.0.113.77")
    assert a.outcome == "unknown" and a.failed_layer == "network"


def test_bind_to_other_loopback_address():
    with FakeServer(lambda c: c.wait_closed(1)) as srv:
        r = probes.tcp_connect("127.0.0.1", srv.port, T, local_ip="127.0.0.2")
        assert r.ok and r.raw["local_address"].startswith("127.0.0.2:")


# --- COTP ------------------------------------------------------------------------------------------
def _answer_cr(data: bytes):
    def handler(c):
        if c.recv_tpkt() is not None:
            c.send(data)
            c.wait_closed(1)

    return handler


def test_cotp_accepted_and_bytes_recorded():
    with FakeServer(_answer_cr(f.CAP_CC)) as srv:
        r = probes.cotp_connect("127.0.0.1", srv.port, T)
    assert (r.ok, r.outcome, r.layer) == (True, "accepted", "transport")
    assert r.raw["sent"] == [f.CAP_CR.hex(" ")] and r.raw["received"] == [f.CAP_CC.hex(" ")]
    assert r.raw["pdu"]["tpdu_size"] == 8192
    assert srv.conns[0].received == [f.CAP_CR]


@pytest.mark.parametrize(
    ("reply", "detail"),
    [
        (f.cotp_dr(0x81), "reason 0x81 remote-transport-entity-congestion"),
        (f.cotp_er(1), "cause 1 invalid-parameter-code"),
    ],
)
def test_cotp_rejected(reply, detail):
    with FakeServer(_answer_cr(reply)) as srv:
        r = probes.cotp_connect("127.0.0.1", srv.port, T)
    assert (r.ok, r.outcome, r.error) == (False, "cotp-rejected", codes.association("cotp-rejected"))
    assert detail in r.detail


def test_cotp_no_response():
    with FakeServer(lambda c: c.wait_closed(2)) as srv:
        t0 = time.monotonic()
        r = probes.cotp_connect("127.0.0.1", srv.port, 0.3)
        assert r.outcome == "cotp-no-response" and time.monotonic() - t0 < 1.5


@pytest.mark.parametrize("how", ["close", "reset"])
def test_tcp_closed_immediately(how):
    with FakeServer(lambda c: getattr(c, how)()) as srv:
        r = probes.cotp_connect("127.0.0.1", srv.port, T)
    assert r.outcome == "tcp-closed-immediately" and r.layer == "transport"
    assert r.raw["closed_after_s"] < 0.5


def test_cotp_invalid_and_tls_on_mms_port():
    with FakeServer(_answer_cr(b"HTTP/1.0 400 Bad\r\n\r\n")) as srv:
        r = probes.cotp_connect("127.0.0.1", srv.port, T)
    assert r.outcome == "cotp-invalid-response" and not r.raw["looks_like_tls"]
    with FakeServer(_answer_cr(bytes.fromhex("15030300020228"))) as srv:  # TLS alert record
        r = probes.cotp_connect("127.0.0.1", srv.port, T)
    assert r.outcome == "cotp-invalid-response" and r.raw["looks_like_tls"] and "TLS" in r.detail


def test_cotp_probe_reports_tcp_failure():
    r = probes.cotp_connect("127.0.0.1", closed_port(), T)
    assert r.name == "cotp" and r.outcome == "tcp-refused" and r.ok is False


# --- full association ---------------------------------------------------------------------------
def test_association_accepted_and_released():
    with FakeServer(f.full_server) as srv:
        a = assoc(srv.port)
        srv.join()
    assert a.ok and a.outcome == "accepted" and a.failed_layer is None and a.ended_by == "release"
    assert a.initiate.max_pdu_size == 65000 and a.closed_after_accept is False
    assert [s.name for s in a.steps] == [f"tcp-{srv.port}", "cotp", "iso-session", "presentation", "acse",
                                         "mms-initiate", "linger", "release"]  # fmt: skip
    assert a.step("release").ok and "DISCONNECT (RLRE)" in a.step("release").detail
    assert srv.conns[0].received == [f.CAP_CR, f.CAP_CN, f.CAP_CONCLUDE_REQ, f.CAP_FINISH]
    assert a.result.name == "iso-association" and a.result.ok
    j = a.to_json()
    assert j["classification"] == {"domain": "association", "code": None, "name": "accepted"}
    assert j["steps"][2]["raw"]["sent"][0] == f.CAP_CN.hex(" ")


def test_association_abort_when_release_not_answered():
    def handler(c):
        c.recv_tpkt()
        c.send(f.CAP_CC)
        c.recv_tpkt()
        c.send(f.CAP_AC)
        c.recv_tpkt()  # conclude: ignored
        c.recv_tpkt(2)  # the abort

    with FakeServer(handler) as srv:
        a = assoc(srv.port, release=True)
        srv.join()
    assert a.ok and a.ended_by == "abort"
    assert srv.conns[0].received[-1] == f.CAP_ABORT
    rel = a.step("release")
    assert rel.ok is False and "abort" in rel.detail


def test_release_false_sends_abort():
    with FakeServer(f.full_server) as srv:
        a = assoc(srv.port, release=False)
        srv.join()
    assert a.ok and a.ended_by == "abort" and srv.conns[0].received[-1] == f.CAP_ABORT


def test_accepted_then_closed():
    with FakeServer(f.cotp_then(f.CAP_AC)) as srv:
        a = assoc(srv.port, linger_s=1.0)
    assert a.outcome == "accepted-then-closed" and not a.ok and a.failed_layer == "mms"
    assert a.closed_after_accept is True and a.closed_after_s < 1.0
    assert a.initiate is not None  # parameters were still parsed


def test_segmented_and_fragmented_response():
    """AC split over two COTP DT TPDUs, each TPKT sent in small TCP pieces."""
    payload = iso.parse_cotp(f.CAP_AC[4:]).user_data
    parts = f.dt(payload[:50], eot=False) + f.dt(payload[50:])

    def handler(c):
        c.recv_tpkt()
        c.send(f.CAP_CC)
        c.recv_tpkt()
        for i in range(0, len(parts), 7):
            c.send(parts[i : i + 7])
            time.sleep(0.001)
        f.serve_release(c)

    with FakeServer(handler) as srv:
        a = assoc(srv.port)
    assert a.outcome == "accepted" and a.initiate.max_serv_outstanding_called == 5 and a.ended_by == "release"


def test_small_tpdu_size_segments_the_request():
    cc = bytearray(f.CAP_CC)
    cc[13] = 0x07  # TPDU size 128

    def handler(c):
        c.recv_tpkt()
        c.send(bytes(cc))
        pkts = [c.recv_tpkt()]
        while pkts[-1] is not None and not (pkts[-1][6] & 0x80):
            pkts.append(c.recv_tpkt())
        c.received_dts = pkts
        c.send(f.CAP_AC)
        c.wait_closed(1)

    with FakeServer(handler) as srv:
        a = assoc(srv.port, release=False)
        srv.join()
    dts = srv.conns[0].received_dts
    assert len(dts) == 2 and all(len(p) <= 128 + 4 for p in dts)
    assert b"".join(p[7:] for p in dts) == f.CAP_CN[7:]
    assert a.ok


def test_session_refused():
    with FakeServer(f.cotp_then(f.refuse(0x81))) as srv:
        a = assoc(srv.port)
    assert (a.outcome, a.failed_layer) == ("session-refused", "session")
    assert "reason 0x81 session-selector-unknown" in a.step("iso-session").detail


def test_session_aborted_without_user_data():
    with FakeServer(f.cotp_then(f.session_abort())) as srv:
        a = assoc(srv.port)
    assert a.outcome == "session-aborted" and "transport-disconnect 0x03" in a.step("iso-session").detail


def test_presentation_rejected_with_congestion():
    with FakeServer(f.cotp_then(f.refuse(0x02, f.cpr(1)))) as srv:
        a = assoc(srv.port)
    assert a.outcome == "presentation-rejected" and a.failed_layer == "session"
    assert "provider-reason 1 temporary-congestion" in a.step("presentation").detail


@pytest.mark.parametrize("carrier", ["accept", "refuse"])
def test_acse_rejected_permanent_authentication_required(carrier):
    rejection = f.aare(1, 14)
    reply = f.accept(f.cpa(rejection)) if carrier == "accept" else f.refuse(0x02, f.cpr(None, rejection))
    with FakeServer(f.cotp_then(reply, linger=1)) as srv:
        a = assoc(srv.port)
        srv.join()
    assert a.outcome == "acse-rejected-permanent" and a.failed_layer == "mms"
    assert a.acse_diagnostic == codes.info(codes.Domain.ACSE_DIAG, 14)
    step = a.step("acse")
    assert step.error.key == "acse-diag:authentication-required"
    assert (
        "result 1 rejected-permanent, diagnostic acse-service-user 14 authentication-required" in step.detail
    )
    if carrier == "accept":  # the server's session is up: the probe aborts it
        assert a.ended_by == "abort" and srv.conns[0].received[-1] == f.CAP_ABORT


def test_acse_rejected_transient():
    with FakeServer(f.cotp_then(f.accept(f.cpa(f.aare(2, 1))))) as srv:
        a = assoc(srv.port)
    assert a.outcome == "acse-rejected-transient"


def test_acse_abort_with_authentication_diagnostic():
    with FakeServer(f.cotp_then(f.session_abort(iso.presentation_aru(f.abrt(0, 6))))) as srv:
        a = assoc(srv.port)
    assert a.outcome == "acse-aborted" and a.acse.abort_diagnostic == 6
    assert "diagnostic 6 authentication-required" in a.step("acse").detail


def test_initiate_error():
    reply = f.accept(f.cpa(f.aare(1, 1, mms=f.initiate_error(8, 2))))
    with FakeServer(f.cotp_then(reply)) as srv:
        a = assoc(srv.port)
    assert a.outcome == "initiate-error" and a.failed_layer == "mms"
    assert "error class 8 initiate, code 2 max-segment-insufficient" in a.step("mms-initiate").detail


def test_initiate_no_response_and_close_after_cotp():
    def silent(c):
        c.recv_tpkt()
        c.send(f.CAP_CC)
        c.wait_closed(2)

    with FakeServer(silent) as srv:
        a = iso_associate("127.0.0.1", srv.port, 0.3)
    assert a.outcome == "initiate-no-response" and "within 0.3 s" in a.step("iso-session").detail

    def closer(c):
        c.recv_tpkt()
        c.send(f.CAP_CC)
        c.recv_tpkt()

    with FakeServer(closer) as srv:
        a = assoc(srv.port)
    assert a.outcome == "initiate-no-response" and "closed by the server" in a.step("iso-session").detail


def test_cotp_disconnect_in_reply_to_association():
    with FakeServer(f.cotp_then(f.cotp_dr(0x80))) as srv:
        a = assoc(srv.port)
    assert a.outcome == "cotp-rejected" and a.failed_layer == "transport"
    assert "COTP DR received in reply to the association request, reason 0x80 normal-disconnect" in (
        a.step("cotp-disconnect").detail
    )


@pytest.mark.parametrize(
    "reply",
    [
        f.dt(b"\x01\x00\x01\x00garbage"),
        f.dt(bytes([iso.SPDU_DN, 0])),
        f.accept(b"\x04\x00"),
        f.accept(f.cpa(b"\x30\x00")),
    ],
)
def test_invalid_responses(reply):
    with FakeServer(f.cotp_then(reply)) as srv:
        a = assoc(srv.port)
    assert a.outcome == "invalid-response" and not a.ok


def test_association_never_raises_on_refused_port():
    a = assoc(closed_port())
    assert a.outcome == "tcp-refused" and a.failed_layer == "network" and a.cotp is None
    assert a.result.error == codes.association("tcp-refused")


def test_ap_title_overrides_reach_the_wire():
    with FakeServer(f.full_server) as srv:
        assoc(srv.port, calling_ap_title="1.3.9999.1", called_ap_title="")
        srv.join()
    cn = srv.conns[0].received[1]
    assert bytes.fromhex("06042bce0f01") in cn and bytes.fromhex("06052901876701") not in cn


# --- TLS port --------------------------------------------------------------------------------------
def test_tls_port_closed_and_plain_open():
    r = probes.tls_port_open("127.0.0.1", T, port=closed_port())
    assert (r.ok, r.outcome) == (False, "tcp-refused")
    with FakeServer(lambda c: c.close()) as srv:
        r = probes.tls_port_open("127.0.0.1", T, port=srv.port)
    assert r.ok is True and r.outcome == "open"


@pytest.fixture
def tls_server(tmp_path):
    if shutil.which("openssl") is None:
        pytest.skip("openssl not available to make a test certificate")
    key, cert = tmp_path / "k.pem", tmp_path / "c.pem"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", cert, "-days", "1",
         "-subj", "/CN=test-ied"],
        check=True, capture_output=True,
    )  # fmt: skip
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    ls = socket.socket()
    ls.bind(("127.0.0.1", 0))
    ls.listen(2)

    def run():
        try:
            s, _ = ls.accept()
            with ctx.wrap_socket(s, server_side=True) as t:
                t.recv(10)
        except OSError:
            pass

    th = threading.Thread(target=run, daemon=True)
    th.start()
    yield ls.getsockname()[1]
    ls.close()
    th.join(2)


def test_tls_handshake_detected(tls_server):
    r = probes.tls_port_open("127.0.0.1", 2.0, port=tls_server)
    assert r.ok and r.outcome == "tls" and r.raw["tls_version"].startswith("TLS")
    assert len(r.raw["peer_certificate_sha256"]) == 64


# --- ICMP ------------------------------------------------------------------------------------------
def _fake_run(returncode: int, stdout: str = "", stderr: str = ""):
    def run(cmd, **kw):
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr, args=cmd)

    return run


def test_ping_outcomes(monkeypatch):
    monkeypatch.setattr(probes.shutil, "which", lambda name: "/bin/ping")
    monkeypatch.setattr(
        probes.subprocess, "run", _fake_run(0, "64 bytes from 10.0.0.1: icmp_seq=1 ttl=64 time=0.412 ms")
    )
    r = probes.ping("10.0.0.1", 1.0)
    assert (r.ok, r.outcome, r.raw["rtt_ms"]) == (True, "reply", 0.412)
    monkeypatch.setattr(probes.subprocess, "run", _fake_run(1, "1 packets transmitted, 0 received"))
    r = probes.ping("10.0.0.1", 1.0)
    assert (r.ok, r.outcome) == (False, "no-reply") and "many IEDs do not answer ping" in r.detail
    monkeypatch.setattr(
        probes.subprocess, "run", _fake_run(1, "From 10.0.0.2 icmp_seq=1 Destination Host Unreachable")
    )
    assert probes.ping("10.0.0.1").outcome == "host-unreachable"
    monkeypatch.setattr(probes.subprocess, "run", _fake_run(2, "", "ping: socket: Operation not permitted"))
    r = probes.ping("10.0.0.1")
    assert (r.ok, r.outcome) == (None, "not-permitted")
    monkeypatch.setattr(probes.shutil, "which", lambda name: None)
    assert probes.ping("10.0.0.1").ok is None


def test_ping_command_line(monkeypatch):
    seen = {}

    def run(cmd, **kw):
        seen["cmd"] = cmd
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(probes.shutil, "which", lambda name: "/bin/ping")
    monkeypatch.setattr(probes.subprocess, "run", run)
    probes.ping("10.0.0.1", 0.3, local_ip="10.0.0.50")
    assert seen["cmd"] == ["/bin/ping", "-n", "-c", "1", "-W", "1", "-I", "10.0.0.50", "10.0.0.1"]


def test_ping_loopback_really():
    r = probes.ping("127.0.0.1", 1.0)
    assert r.ok in (True, None)  # None where ping is not permitted (containers)


# --- the probe sequence ------------------------------------------------------------------------------
def test_run_layered_probes_checks_tls_port_only_when_needed(monkeypatch):
    calls = []
    monkeypatch.setattr(probes, "tls_port_open", lambda *a, **k: calls.append(a) or probes.ProbeResult(
        "network", "tcp-3782", True, "open", 0.0, "open"))  # fmt: skip
    with FakeServer(f.full_server) as srv:
        lp = probes.run_layered_probes("127.0.0.1", srv.port, timeout=T, icmp=False, linger_s=0.1)
    assert lp.association.ok and lp.tls is None and calls == []
    assert [r.name for r in lp.results()][:2] == [f"tcp-{srv.port}", "cotp"]
    lp = probes.run_layered_probes("127.0.0.1", closed_port(), timeout=T, icmp=False)
    assert lp.tcp.outcome == "tcp-refused" and lp.association is None and lp.tls.outcome == "open"
    assert lp.to_json()["tls"]["outcome"] == "open"
