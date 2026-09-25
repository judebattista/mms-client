"""discover (DIA-5) against the simulated IED."""

from __future__ import annotations

import time

import pytest

from ied_client.diagnosis.discover import discover, draft_inventory
from mms_protocol import codes as mms_codes
from mms_protocol import protocol as mms
from mms_protocol.adapter import IedClient
from tests.conftest import FIXTURES
from tests.sim.fixture import SimProcess

pytestmark = pytest.mark.integration
CFG = FIXTURES / "sim" / "basic.cfg"


@pytest.fixture(scope="module")
def sim1():
    """One association slot, bound to 127.0.0.1 only, custom identity."""
    with SimProcess(
        config=CFG,
        options={"max_connections": 1, "local_ip": "127.0.0.1", "identity": ["RackVendor", "BCU-9", "4.2.0"]},
    ) as p:
        yield p


def test_discover_reads_identity_and_logical_devices_and_releases(sim1):
    progress = []
    found = discover("127.0.0.1/32", protocol=mms, port=sim1.port, timeout_s=1.0, delay_s=0, progress=progress.append)
    (dev,) = found
    assert dev.tcp_open and dev.associated and dev.error is None and dev.classification is None
    assert (dev.identity.vendor, dev.identity.model, dev.identity.revision) == (
        "RackVendor",
        "BCU-9",
        "4.2.0",
    )
    assert dev.logical_devices == ["SIMCTRL"]
    assert dev.release == mms_codes.ied_error(0)  # conclude + ACSE release confirmed by the device
    assert dev.duration_s < 1.0  # no wait for a request timeout after the release
    assert [(p.index, p.total) for p in progress] == [(1, 1)]

    # the slot (there is only one) is free straight away: a second discover succeeds too
    (again,) = discover("127.0.0.1", protocol=mms, port=sim1.port, timeout_s=1.0, delay_s=0)
    assert again.associated and again.release == mms_codes.ied_error(0)

    inv = draft_inventory(found)
    assert inv == {
        "schema_version": 1,
        "safety": "strict",
        "devices": [
            {
                "name": "ied-127-0-0-1",
                "ip": "127.0.0.1",
                "port": sim1.port,
                "role": "unknown",
                "identity": {
                    "vendor": "RackVendor",
                    "model": "BCU-9",
                    "firmware": "4.2.0",
                    "source": "discover",
                },
                "logical_devices": ["SIMCTRL"],
            }
        ],
        "clients": [],
    }


def test_each_association_is_released_before_the_next_host(sim1):
    """Sequential: the device sees connect/disconnect pairs, never two associations at once."""
    start = len(sim1.events)
    discover("127.0.0.0/30", protocol=mms, port=sim1.port, timeout_s=0.5, delay_s=0.1)  # 127.0.0.1 and 127.0.0.2
    time.sleep(0.2)
    evs = [e["event"] for e in sim1.events[start:] if e.get("event") in ("connect", "disconnect")]
    assert evs[:4] == ["connect", "disconnect", "connect", "disconnect"]  # TCP scan, then the association
    depth = 0
    for e in evs:
        depth += 1 if e == "connect" else -1
        assert depth <= 1


def test_discover_classifies_a_device_with_no_free_slot(sim1):
    held = IedClient(connect_timeout_ms=2000, request_timeout_ms=2000)
    deadline = time.monotonic() + 3
    while True:
        try:
            held.connect("127.0.0.1", sim1.port)
            break
        except Exception:
            assert time.monotonic() < deadline
            time.sleep(0.05)
    try:
        (dev,) = discover("127.0.0.1", protocol=mms, port=sim1.port, timeout_s=1.0, delay_s=0)
    finally:
        held.close()
    assert dev.tcp_open and not dev.associated and dev.identity is None
    assert dev.error == mms_codes.ied_error(5)
    assert dev.classification.outcome == "tcp-closed-immediately"
    assert draft_inventory([dev])["devices"][0]["identity"]["vendor"] is None
