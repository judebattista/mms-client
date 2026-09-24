"""Layered diagnosis (DIA-1 … DIA-4, NBR-2) against the simulator."""

from __future__ import annotations

import pytest

from mms_client.core.session import Session, Target
from mms_client.diagnosis.diagnose import diagnose
from tests.conftest import FIXTURES

pytestmark = pytest.mark.integration


def test_healthy_device_all_layers_pass(sim):
    s = Session(Target("127.0.0.1", sim.port, "sim"))
    try:
        rep = diagnose(s, timeout_s=2)
        assert rep.stopped_at is None
        assert [lay.layer for lay in rep.layers] == [
            "network", "transport-session", "mms-initiate", "association", "identity", "model", "reports",
        ]
        assert rep.verdict.certainty == "fact" and "GOOSE" in rep.verdict.text
        assert any(f.key == "tool:association-slot-occupied-by-tool" for f in rep.findings)  # DIA-4c
        assert s.connected and s.last_results["diagnose"] is rep
        assert any("GOOSE" in n for n in rep.notes)
    finally:
        s.close()


def test_slots_exhausted_hint_is_likely_not_fact():
    from tests.sim.fixture import SimProcess

    with SimProcess(config=FIXTURES / "sim" / "basic.cfg", options={"max_connections": 1}) as p:
        holder = Session(Target("127.0.0.1", p.port, "sim"))
        holder.connect()
        try:
            rep = diagnose(Session(Target("127.0.0.1", p.port, "sim")), timeout_s=2)
        finally:
            holder.close()
    assert rep.stopped_at in ("transport-session", "mms-initiate", "association")
    assert rep.verdict.certainty == "likely"
    assert "slots" in rep.verdict.text
    assert any("Close other MMS clients" in s for s in rep.next_steps)


def test_nothing_listening_stops_at_network():
    rep = diagnose(Session(Target("127.0.0.1", 1, "nothing")), timeout_s=1)
    assert rep.stopped_at == "network"
    assert rep.verdict.key == "association:tcp-refused" and rep.verdict.certainty == "fact"


def test_missing_bind_address_gives_exact_command(sim):
    rep = diagnose(Session(Target("127.0.0.1", sim.port, "sim", local_ip="10.99.99.99")), timeout_s=1)
    assert rep.stopped_at == "bind"
    assert "ip addr add 10.99.99.99/" in rep.verdict.text and rep.verdict.certainty == "fact"
