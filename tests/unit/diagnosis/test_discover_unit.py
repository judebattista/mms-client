"""discover (DIA-5) without the simulator: target lists, rate limiting, the draft inventory."""

from __future__ import annotations

import socket
import time

import pytest

from ied_client.diagnosis import discover as disc
from ied_client.diagnosis.discover import DiscoveredDevice, discover, draft_inventory, scan_targets
from ied_client.protocol.types import ServerIdentity
from mms_protocol import protocol as mms

from .fakes import FakeServer


def test_scan_targets():
    assert scan_targets("10.0.0.0/30") == ["10.0.0.1", "10.0.0.2"]
    assert scan_targets("10.0.0.7") == ["10.0.0.7"] == scan_targets("10.0.0.7/32")
    assert scan_targets("10.0.0.6/31") == ["10.0.0.6", "10.0.0.7"]
    assert scan_targets("10.0.0.9/24")[0] == "10.0.0.1" and len(scan_targets("10.0.0.0/24")) == 254
    assert len(scan_targets("10.0.0.0/22")) == 1022
    with pytest.raises(ValueError, match="larger than /22"):
        scan_targets("10.0.0.0/21")
    assert len(scan_targets("10.0.0.0/21", allow_large=True)) == 2046
    for bad in ("fe80::/64", "10.0.0.0/33", "rack"):
        with pytest.raises(ValueError):
            scan_targets(bad)


def test_sequential_rate_limited_scan_without_association():
    starts: list[float] = []

    def handler(c):
        starts.append(time.monotonic())
        c.wait_closed(1)

    with FakeServer(handler) as srv:
        seen = []
        t0 = time.monotonic()
        # 127.0.0.0/30 -> 127.0.0.1 (listening) and 127.0.0.2 (refused: the fake binds 127.0.0.1 only)
        found = discover(
            "127.0.0.0/30", protocol=mms, port=srv.port, timeout_s=0.5, delay_s=0.3, associate=False,
            progress=seen.append
        )
        elapsed = time.monotonic() - t0
    assert [d.ip for d in found] == ["127.0.0.1"] and found[0].tcp_open and found[0].identity is None
    assert [(p.index, p.total, p.ip) for p in seen] == [(1, 2, "127.0.0.1"), (2, 2, "127.0.0.2")]
    assert seen[1].device.tcp.outcome == "tcp-refused"
    assert elapsed >= 0.3  # the delay between hosts
    everything = discover(
        "127.0.0.0/30", protocol=mms, port=srv.port, timeout_s=0.5, delay_s=0, associate=False, include_closed=True
    )
    assert [d.tcp_open for d in everything] == [False, False]  # server closed by now


def test_association_failure_is_classified():
    """A responder that accepts TCP and closes at once (what a full libiec61850 server does)."""
    with FakeServer(lambda c: c.close()) as srv:
        (dev,) = discover("127.0.0.1", protocol=mms, port=srv.port, timeout_s=0.5, delay_s=0)
    assert dev.tcp_open and not dev.associated and dev.error.key == "ied:connection-rejected"
    assert dev.classification.outcome == "tcp-closed-immediately"
    assert dev.to_json()["classification"]["outcome"] == "tcp-closed-immediately"
    entry = dev.to_inventory_entry()
    assert entry["identity"] == {"vendor": None, "model": None, "firmware": None, "source": "discover"}


def test_missing_local_address_is_reported(monkeypatch):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    (dev,) = discover("127.0.0.1", protocol=mms, port=port, local_ip="203.0.113.77", include_closed=True, delay_s=0)
    assert not dev.tcp_open and dev.error.key == "tool:local-address-missing"


def test_draft_inventory():
    a = DiscoveredDevice(
        "10.0.0.11", 102, True, ServerIdentity("Acme", "BCU-500", "2.3.1"), ["BCU1CTRL", "BCU1PROT"]
    )
    b = DiscoveredDevice("10.0.0.12", 102, False)
    c = DiscoveredDevice("10.0.0.13", 10102, True)
    inv = draft_inventory([a, b, c])
    assert inv["schema_version"] == 1 and inv["safety"] == "strict" and inv["clients"] == []
    assert inv["devices"][0] == {
        "name": "ied-10-0-0-11",
        "ip": "10.0.0.11",
        "port": 102,
        "role": "unknown",
        "identity": {"vendor": "Acme", "model": "BCU-500", "firmware": "2.3.1", "source": "discover"},
        "logical_devices": ["BCU1CTRL", "BCU1PROT"],
    }
    assert [d["name"] for d in inv["devices"]] == ["ied-10-0-0-11", "ied-10-0-0-13"]
    assert a.to_inventory_entry("bcu-1")["name"] == "bcu-1"
    dup = draft_inventory([a, DiscoveredDevice("10.0.0.11", 10102, True)])
    assert [d["name"] for d in dup["devices"]] == ["ied-10-0-0-11", "ied-10-0-0-11-10102"]
    assert disc.MAX_PREFIX_DEFAULT == 22
