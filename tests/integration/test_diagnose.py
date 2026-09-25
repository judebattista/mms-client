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


def _snapshot_of(sim):
    from mms_client.verify.snapshot import capture

    s = Session(Target("127.0.0.1", sim.port, "sim"))
    try:
        s.connect()
        return capture(s)
    finally:
        s.close()


def test_snapshot_reference_is_compared_in_the_model_layer(sim, tmp_path):
    """DIA-1 (Version-1.01): with a snapshot reference the model layer compares, it does not silently pass."""
    from mms_client.verify.reference import Reference

    snap = _snapshot_of(sim)
    s = Session(Target("127.0.0.1", sim.port, "sim"))
    try:
        s.reference = Reference("snapshot", tmp_path / "same.json", snapshot=snap)
        rep = diagnose(s, timeout_s=2)
        model = next(lay for lay in rep.layers if lay.layer == "model")
        assert [r.status.value for r in model.results if r.id == "snapshot-structure"] == ["pass"]
    finally:
        s.close()

    changed = _snapshot_of(sim)
    key = next(k for k in changed.structure["model"] if ".SPCSO1." in k)
    changed.structure["model"][key.replace("SPCSO1", "SPCSO9")] = changed.structure["model"].pop(key)
    s = Session(Target("127.0.0.1", sim.port, "sim"))
    try:
        s.reference = Reference("snapshot", tmp_path / "changed.json", snapshot=changed)
        rep = diagnose(s, timeout_s=2)
        assert rep.stopped_at == "model" and rep.verdict.certainty == "fact"
        assert "differs from the snapshot" in rep.verdict.text
    finally:
        s.close()


def test_client_rcb_named_in_iec_form_is_found(sim):
    """Version-1.01: diagnose accepts the same RCB names as `rcb` / `subscribe` (LD/LLN0.urcbEvents02, no FC)."""
    from mms_client.inventory import ClientRelation, Device, Inventory

    inv = Inventory(devices=[Device("sim", "127.0.0.1", sim.port)],
                    clients=[ClientRelation("gw", "gateway-1", "sim", "127.0.0.9", ["SIMCTRL/LLN0.urcbEvents02"])])
    s = Session(Target("127.0.0.1", sim.port, "sim", inv.devices[0]), inventory=inv)
    try:
        rep = diagnose(s, timeout_s=2)
        rpt = next(lay for lay in rep.layers if lay.layer == "reports")
        assert not any(r.id == "client-rcb-missing" for r in rpt.results), [r.to_json() for r in rpt.results]
        assert any(r.subject == "SIMCTRL/LLN0.urcbEvents02 for gateway-1" and r.status.value == "pass" for r in rpt.results)
    finally:
        s.close()
