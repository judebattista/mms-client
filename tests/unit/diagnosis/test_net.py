"""Local address checks (NBR-2): parsing of iproute2 / procfs output and the suggested command."""

from __future__ import annotations

import json

import pytest

from ied_client import codes
from ied_client.diagnosis import net
from ied_client.diagnosis.net import InterfaceAddress

IP_JSON = json.dumps(
    [
        {"ifname": "lo", "addr_info": [{"family": "inet", "local": "127.0.0.1", "prefixlen": 8}]},
        {
            "ifname": "eth0",
            "addr_info": [
                {"family": "inet", "local": "10.0.0.100", "prefixlen": 24},
                {"family": "inet", "local": "10.0.0.50", "prefixlen": 24},
                {"family": "inet6", "local": "fe80::1", "prefixlen": 64},
            ],
        },
        {"ifname": "eth1", "addr_info": [{"family": "inet", "local": "192.168.5.2", "prefixlen": 16}]},
    ]
)
IP_TEXT = """\
1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever preferred_lft forever
2: eth0    inet 10.0.0.100/24 brd 10.0.0.255 scope global eth0\\       valid_lft forever preferred_lft forever
2: eth0    inet 10.0.0.50/24 scope global secondary eth0\\       valid_lft forever preferred_lft forever
"""
FIB_TRIE = """\
Main:
  +-- 0.0.0.0/0 3 0 5
     |-- 10.0.0.100
        /32 host LOCAL
Local:
  +-- 0.0.0.0/0 3 0 5
     +-- 10.0.0.0/24 2 0 2
        |-- 10.0.0.0
           /24 link UNICAST
        |-- 10.0.0.100
           /32 host LOCAL
        |-- 10.0.0.255
           /32 link BROADCAST
     +-- 127.0.0.0/8 2 0 2
        |-- 127.0.0.1
           /32 host LOCAL
"""
ADDRS = [
    InterfaceAddress("lo", "127.0.0.1", 8),
    InterfaceAddress("eth0", "10.0.0.100", 24),
    InterfaceAddress("eth1", "192.168.5.2", 16),
]


def test_parse_ip_json_and_text():
    got = net._from_ip_json(IP_JSON)
    assert [(a.interface, a.ip, a.prefix_len) for a in got] == [
        ("lo", "127.0.0.1", 8), ("eth0", "10.0.0.100", 24), ("eth0", "10.0.0.50", 24), ("eth1", "192.168.5.2", 16),
    ]  # fmt: skip
    got = net._from_ip_text(IP_TEXT)
    assert [a.ip for a in got] == ["127.0.0.1", "10.0.0.100", "10.0.0.50"] and got[2].interface == "eth0"


def test_parse_fib_trie(monkeypatch):
    monkeypatch.setattr(net, "_proc_routes", lambda: [("eth0", net.ipaddress.IPv4Network("10.0.0.0/24"))])
    got = net._from_fib_trie(FIB_TRIE)
    assert got == [InterfaceAddress("eth0", "10.0.0.100", 24), InterfaceAddress("lo", "127.0.0.1", 8)]


def test_fallback_chain(monkeypatch):
    outputs = {"-j": None, "-o": IP_TEXT}
    monkeypatch.setattr(net, "_run", lambda args: outputs["-j" if "-j" in args else "-o"])
    assert net.local_addresses() == {"lo": ["127.0.0.1"], "eth0": ["10.0.0.100", "10.0.0.50"]}
    outputs["-o"] = None
    monkeypatch.setattr(net.Path, "read_text", lambda self: FIB_TRIE if "fib_trie" in str(self) else "")
    assert {a.ip for a in net.interface_addresses()} == {"10.0.0.100", "127.0.0.1"}


def test_live_host_has_loopback():
    addrs = net.local_addresses()
    assert any("127.0.0.1" in v for v in addrs.values())
    assert net.is_local_address("127.0.0.1") and net.is_local_address("127.1.2.3")
    assert not net.is_local_address("203.0.113.77")
    assert net.route_interface("127.0.0.1") == "lo"
    with pytest.raises(ValueError):
        net.is_local_address("not-an-ip")


@pytest.fixture
def fake_host(monkeypatch):
    routes = {"10.0.0.11": "eth0", "192.168.9.9": "eth1", "172.16.0.1": None}
    monkeypatch.setattr(net, "interface_addresses", lambda: list(ADDRS))
    monkeypatch.setattr(net, "route_interface", lambda dest, source_ip=None: routes.get(dest))
    return routes


def test_command_uses_route_interface_and_existing_prefix(fake_host):
    assert net.ip_addr_add_command("10.0.0.50", "10.0.0.11") == "sudo ip addr add 10.0.0.50/24 dev eth0"
    s = net.suggest_ip_addr_add("10.0.0.50", "10.0.0.11")
    assert "prefix /24 taken from 10.0.0.100/24 on eth0" in s.notes


def test_command_assumes_24_outside_existing_subnets(fake_host):
    s = net.suggest_ip_addr_add("192.169.1.1", "192.168.9.9")
    assert s.command == "sudo ip addr add 192.169.1.1/24 dev eth1"
    assert any("/24 assumed" in n and "192.168.5.2/16" in n for n in s.notes)
    assert net.ip_addr_add_command("10.0.0.60", prefix_len=26) == "sudo ip addr add 10.0.0.60/26 dev eth0"


def test_command_without_route_or_subnet(fake_host):
    s = net.suggest_ip_addr_add("172.16.0.50", "172.16.0.1")
    assert s.command == "sudo ip addr add 172.16.0.50/24 dev <interface>"
    assert any("replace <interface>" in n for n in s.notes)


def test_check_bind_address_missing(fake_host):
    c = net.check_bind_address("10.0.0.50", "10.0.0.11")
    assert not c.ok and c.command == "sudo ip addr add 10.0.0.50/24 dev eth0"
    assert "sudo ip addr add 10.0.0.50/24 dev eth0" in c.message
    assert c.error == codes.tool("local-address-missing") and c.interface == "eth0"
    assert c.to_json()["command"] == c.command


def test_check_bind_address_present_and_wrong_interface(fake_host):
    c = net.check_bind_address("10.0.0.100", "10.0.0.11")
    assert c.ok and c.interface == "eth0" and not c.warnings and c.command is None
    c = net.check_bind_address("10.0.0.100", "192.168.9.9")
    assert c.ok and c.warnings and "leaves through eth1" in c.message


def test_check_bind_address_loopback_and_invalid():
    assert net.check_bind_address("127.0.0.1", "127.0.0.1").ok
    assert net.check_bind_address("127.0.0.2", "127.0.0.1").ok  # all of 127/8 is local on Linux
    c = net.check_bind_address("10.0.0.300", "10.0.0.1")
    assert not c.ok and "not an IPv4 address" in c.message
