"""Layered probes against the simulated IED (a real libiec61850 1.6.1 server), incl. spike RSK-3.

What the real server does when its association slots are exhausted is recorded here and in the
docstring of ``mms_client.diagnosis.classify``: TCP accepted by the kernel, then the socket is
closed without any ISO PDU; libiec61850's client reports only ``ied:connection-rejected (5)``.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time

import pytest

from mms_client import codes
from mms_client.adapter import ConnectError, IedClient
from mms_client.diagnosis import iso, probes
from mms_client.diagnosis.classify import (
    assess_association_limits,
    classify_association,
    explain_ied_connect_error,
)
from mms_client.diagnosis.probes import iso_associate
from tests.conftest import FIXTURES
from tests.sim.fixture import REPO, SimProcess

pytestmark = pytest.mark.integration
CFG = FIXTURES / "sim" / "basic.cfg"
T = 2.0


def ours(ev: dict, local: str) -> bool:
    return ev.get("peer") == local


@pytest.fixture(scope="module")
def sim1():
    """A simulated IED with a single association slot."""
    with SimProcess(
        config=CFG, options={"max_connections": 1, "identity": ["SimVendor", "SimIED", "1.0"]}
    ) as p:
        yield p


@pytest.fixture
def free_slot(sim1):
    """Wait until the single slot is free (a previous test's association may still be closing)."""
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if iso_associate("127.0.0.1", sim1.port, T, linger_s=0.05).ok:
            return sim1
        time.sleep(0.05)
    pytest.fail("the simulator's slot did not become free")


# --- an accepted association, decoded ----------------------------------------------------------
def test_association_accepted_with_negotiated_parameters(sim):
    a = iso_associate("127.0.0.1", sim.port, T, linger_s=0.3)
    assert a.ok and a.outcome == "accepted" and a.failed_layer is None, a.to_json()
    assert [s.outcome for s in a.steps[1:6]] == ["accepted"] * 5
    p = a.initiate
    assert (p.max_pdu_size, p.max_serv_outstanding_calling, p.max_serv_outstanding_called) == (65000, 5, 5)
    assert (p.data_structure_nesting_level, p.version) == (10, 1)
    assert p.parameter_cbb_names() == ["str1", "str2", "vnam", "valt", "vlis"]
    for service in ("getNameList", "identify", "read", "write", "getVariableAccessAttributes", "fileDirectory",
                    "informationReport", "conclude"):  # fmt: skip
        assert p.supports(service), service
    assert a.acse.result == 0 and a.acse.application_context == iso.MMS_APPLICATION_CONTEXT
    assert a.closed_after_accept is False and a.ended_by == "release"
    rel = a.step("release")
    assert rel.ok and "conclude-ResponsePDU" in rel.detail and "DISCONNECT (RLRE)" in rel.detail

    # the same values libiec61850's own client negotiates
    c = IedClient(connect_timeout_ms=2000, request_timeout_ms=2000)
    c.connect("127.0.0.1", sim.port)
    try:
        cp = c.connection_params()
    finally:
        c.close()
    assert (cp.max_pdu_size, cp.max_serv_outstanding_calling, cp.max_serv_outstanding_called) == (
        p.max_pdu_size, p.max_serv_outstanding_calling, p.max_serv_outstanding_called,
    )  # fmt: skip
    assert cp.data_structure_nesting_level == p.data_structure_nesting_level
    assert cp.services_supported == p.services_supported

    local = a.tcp.raw["local_address"]
    assert sim.wait_event(lambda e: e.get("event") == "disconnect" and ours(e, local), 3)


def test_proposed_pdu_size_is_negotiated(sim):
    assert (
        iso_associate("127.0.0.1", sim.port, T, max_pdu_size=1000, linger_s=0.05).initiate.max_pdu_size
        == 1000
    )
    assert (
        iso_associate("127.0.0.1", sim.port, T, max_pdu_size=100000, linger_s=0.05).initiate.max_pdu_size
        == 65000
    )


def test_cotp_probe_and_layered_sequence(sim):
    r = probes.cotp_connect("127.0.0.1", sim.port, T)
    assert r.ok and r.outcome == "accepted" and r.raw["pdu"]["tpdu_size"] == 8192
    lp = probes.run_layered_probes("127.0.0.1", sim.port, timeout=T, linger_s=0.1)
    assert lp.icmp is not None and lp.icmp.ok in (True, None)
    assert lp.tcp.ok and lp.association.ok and lp.tls is None
    c = classify_association(lp.tcp, lp.cotp, lp.association, lp.tls, icmp=lp.icmp)
    assert c.ok and c.layer is None


def test_bound_to_another_local_address(sim):
    """NBR-2: the probe binds its source address (all of 127/8 is local on Linux)."""
    a = iso_associate("127.0.0.1", sim.port, T, local_ip="127.0.0.2", linger_s=0.05)
    assert a.ok and a.tcp.raw["local_address"].startswith("127.0.0.2:")
    assert sim.wait_event(
        lambda e: e.get("event") == "connect" and str(e.get("peer", "")).startswith("127.0.0.2:"), 3
    )


# --- RSK-3: association slots exhausted -------------------------------------------------------------
def test_slots_exhausted_behaviour_and_classification(free_slot):
    sim = free_slot
    held = IedClient(connect_timeout_ms=2000, request_timeout_ms=2000)
    held.connect("127.0.0.1", sim.port)
    try:
        # the kernel still completes the TCP handshake ...
        assert probes.tcp_connect("127.0.0.1", sim.port, T).outcome == "accepted"
        # ... then the server closes the socket without any ISO PDU
        cotp = probes.cotp_connect("127.0.0.1", sim.port, T)
        assert cotp.outcome == "tcp-closed-immediately" and cotp.raw["received"] == []
        assert cotp.raw["closed_after_s"] < 0.5
        a = iso_associate("127.0.0.1", sim.port, T)
        assert (a.outcome, a.failed_layer, a.ended_by) == (
            "tcp-closed-immediately",
            "transport",
            "server-closed",
        )

        # libiec61850's client only says "connection rejected"
        c = IedClient(connect_timeout_ms=2000, request_timeout_ms=2000)
        t0 = time.monotonic()
        with pytest.raises(ConnectError) as ei:
            c.connect("127.0.0.1", sim.port)
        elapsed = time.monotonic() - t0
        assert ei.value.error == codes.ied_error(5)
        note = explain_ied_connect_error(ei.value.error, elapsed_s=elapsed, timeout_s=2.0)
        assert "does not say which layer failed" in note and "before the 2 s timeout" in note

        failure = classify_association(a.tcp, None, a, None)
        assert failure.outcome == "tcp-closed-immediately" and failure.layer == "transport"
        findings = assess_association_limits(failure, quirks=None, tool_holds_association=True)
        slots = next(f for f in findings if f.key == "tool:slots-probably-exhausted")
        assert slots.certainty == "likely"
        assert (
            findings[-1].key == "tool:association-slot-occupied-by-tool" and findings[-1].certainty == "fact"
        )
    finally:
        held.close()
    # the held association was released: the slot is free again
    deadline = time.monotonic() + 3
    while not iso_associate("127.0.0.1", sim.port, T, linger_s=0.05).ok:
        assert time.monotonic() < deadline
        time.sleep(0.05)


def test_probe_releases_its_slot(free_slot):
    """With one slot, back-to-back probes all succeed: each one ends its association properly."""
    sim = free_slot
    for release in (True, True, False, True):
        a = iso_associate("127.0.0.1", sim.port, T, linger_s=0.05, release=release)
        assert a.ok, a.detail
        assert a.ended_by == ("release" if release else "abort")
        time.sleep(0.05)  # the server's connection thread notices the close


def test_killed_client_frees_its_slot_quickly(free_slot):
    """RSK-3: when a client process dies, its kernel closes the socket and the slot is freed."""
    sim = free_slot
    code = (
        f"import sys, time; sys.path.insert(0, {str(REPO / 'src')!r})\n"
        "from mms_client.adapter import IedClient\n"
        f"c = IedClient(); c.connect('127.0.0.1', {sim.port}); print('held', flush=True); time.sleep(60)\n"
    )
    child = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True, cwd=REPO)
    try:
        assert child.stdout.readline().strip() == "held"
        assert iso_associate("127.0.0.1", sim.port, T, linger_s=0.05).outcome == "tcp-closed-immediately"
    finally:
        child.kill()
        child.wait(5)
    t0 = time.monotonic()
    while not iso_associate("127.0.0.1", sim.port, T, linger_s=0.05).ok:
        assert time.monotonic() - t0 < 2.0, "slot not freed after the client was killed"
        time.sleep(0.02)


def test_silent_client_keeps_its_slot(free_slot):
    """RSK-3: a client that holds its TCP connection open but says nothing keeps the slot."""
    sim = free_slot
    s = socket.create_connection(("127.0.0.1", sim.port), timeout=2)
    try:
        s.sendall(iso.tpkt(iso.connection_request()))
        s.recv(100)
        s.sendall(iso.tpkt(iso.cotp_dt(iso.association_request())))
        s.recv(4000)
        time.sleep(1.0)
        a = iso_associate("127.0.0.1", sim.port, T, linger_s=0.05)
        assert a.outcome == "tcp-closed-immediately"
    finally:
        s.close()
    t0 = time.monotonic()
    while not iso_associate("127.0.0.1", sim.port, T, linger_s=0.05).ok:
        assert time.monotonic() - t0 < 2.0
        time.sleep(0.02)
