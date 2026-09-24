"""Local address checks for binding the association to a given source IP (NBR-2). Linux only.

Some IEDs accept MMS clients only from known IP addresses. To act as a neighbour, the operator
adds that neighbour's address to the test PC's interface (``sudo ip addr add …``) and the tool
binds the association to it. These helpers check that the address is present and, if it is not,
produce the exact command to run.

Sources, in order of preference: ``ip -j`` (iproute2 JSON), ``ip -o`` text output, then
``/proc/net/fib_trie`` + ``/proc/net/route`` (which know every local address but not always its
interface). Nothing here needs privileges or changes the system.
"""

from __future__ import annotations

import ipaddress
import json
import re
import shutil
import socket
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mms_client import codes
from mms_client.codes import ErrorInfo

DEFAULT_PREFIX_LEN = 24
_TIMEOUT_S = 3.0


@dataclass(frozen=True, slots=True)
class InterfaceAddress:
    """One IPv4 address configured on an interface."""

    interface: str
    ip: str
    prefix_len: int

    @property
    def network(self) -> ipaddress.IPv4Network:
        return ipaddress.IPv4Network(f"{self.ip}/{self.prefix_len}", strict=False)


def _run(args: list[str]) -> str | None:
    exe = shutil.which(args[0])
    if exe is None:
        return None
    try:
        proc = subprocess.run(
            [exe, *args[1:]],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_S,
            check=False,
            env={"LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout if proc.returncode == 0 else None


def _from_ip_json(text: str) -> list[InterfaceAddress]:
    out = []
    for link in json.loads(text):
        name = link.get("ifname", "?")
        for a in link.get("addr_info", []):
            if a.get("family") == "inet" and "local" in a:
                out.append(InterfaceAddress(name, a["local"], int(a.get("prefixlen", 32))))
    return out


_IP_O_RE = re.compile(r"^\d+:\s+(\S+)\s+inet\s+(\d+\.\d+\.\d+\.\d+)/(\d+)")


def _from_ip_text(text: str) -> list[InterfaceAddress]:
    out = []
    for line in text.splitlines():
        m = _IP_O_RE.match(line.strip())
        if m:
            out.append(InterfaceAddress(m.group(1), m.group(2), int(m.group(3))))
    return out


def _proc_routes() -> list[tuple[str, ipaddress.IPv4Network]]:
    """(interface, network) from /proc/net/route (little-endian hex fields)."""
    routes = []
    try:
        lines = Path("/proc/net/route").read_text().splitlines()[1:]
    except OSError:
        return routes
    for line in lines:
        f = line.split()
        if len(f) < 8:
            continue
        try:
            dest = socket.inet_ntoa(int(f[1], 16).to_bytes(4, "little"))
            mask = socket.inet_ntoa(int(f[7], 16).to_bytes(4, "little"))
            routes.append((f[0], ipaddress.IPv4Network(f"{dest}/{mask}", strict=False)))
        except (ValueError, OSError):
            continue
    return routes


def _from_fib_trie(text: str) -> list[InterfaceAddress]:
    """Local (``/32 host LOCAL``) addresses from /proc/net/fib_trie, mapped to interfaces via routes."""
    ips: list[str] = []
    last_ip: str | None = None
    in_local = False
    for line in text.splitlines():
        if line and not line.startswith(" "):  # table header: "Main:" / "Local:"
            in_local = line.startswith("Local:")
            continue
        if not in_local:
            continue
        m = re.search(r"\|--\s+(\d+\.\d+\.\d+\.\d+)", line)
        if m:
            last_ip = m.group(1)
        elif "/32 host LOCAL" in line and last_ip and last_ip not in ips:
            ips.append(last_ip)
    routes = [r for r in _proc_routes() if r[1].prefixlen > 0]
    out = []
    for ip in ips:
        addr = ipaddress.IPv4Address(ip)
        if addr.is_loopback:
            out.append(InterfaceAddress("lo", ip, 8))
            continue
        best = max((r for r in routes if addr in r[1]), key=lambda r: r[1].prefixlen, default=None)
        out.append(
            InterfaceAddress(best[0], ip, best[1].prefixlen) if best else InterfaceAddress("?", ip, 32)
        )
    return out


def interface_addresses() -> list[InterfaceAddress]:
    """All IPv4 addresses on this host with their interface and prefix length."""
    text = _run(["ip", "-j", "-4", "addr", "show"])
    if text:
        try:
            return _from_ip_json(text)
        except (ValueError, TypeError, AttributeError):
            pass
    text = _run(["ip", "-4", "-o", "addr", "show"])
    if text:
        found = _from_ip_text(text)
        if found:
            return found
    try:
        return _from_fib_trie(Path("/proc/net/fib_trie").read_text())
    except OSError:
        return []


def local_addresses() -> dict[str, list[str]]:
    """Interface name -> IPv4 addresses configured on it."""
    out: dict[str, list[str]] = {}
    for a in interface_addresses():
        out.setdefault(a.interface, []).append(a.ip)
    return out


def _ipv4(ip: str) -> ipaddress.IPv4Address:
    try:
        return ipaddress.IPv4Address(ip.strip())
    except ValueError as e:
        raise ValueError(f"{ip!r} is not an IPv4 address") from e


def is_local_address(ip: str) -> bool:
    """Is ``ip`` configured on this host? (127.0.0.0/8 always counts as local.)"""
    addr = _ipv4(ip)
    if addr.is_loopback:
        return True
    return any(a.ip == str(addr) for a in interface_addresses())


def _resolve(host: str) -> str | None:
    try:
        return str(ipaddress.IPv4Address(host))
    except ValueError:
        pass
    try:
        return socket.getaddrinfo(host, None, socket.AF_INET)[0][4][0]
    except (OSError, IndexError):
        return None


def route_interface(dest_ip: str, *, source_ip: str | None = None) -> str | None:
    """The interface the kernel would use to reach ``dest_ip`` (optionally from ``source_ip``)."""
    dest = _resolve(dest_ip)
    if dest is None:
        return None
    args = ["ip", "-j", "route", "get", dest]
    if source_ip:
        args += ["from", source_ip]
    text = _run(args)
    if text:
        try:
            routes = json.loads(text)
            if routes and routes[0].get("dev"):
                return str(routes[0]["dev"])
        except (ValueError, TypeError, AttributeError):
            pass
    text = _run(["ip", "route", "get", dest] + (["from", source_ip] if source_ip else []))
    if text:
        m = re.search(r"\bdev\s+(\S+)", text)
        if m:
            return m.group(1)
    addr = ipaddress.IPv4Address(dest)
    if addr.is_loopback:
        return "lo"
    for a in interface_addresses():
        if a.ip == dest:
            return a.interface
    routes = _proc_routes()
    best = max((r for r in routes if addr in r[1]), key=lambda r: r[1].prefixlen, default=None)
    return best[0] if best else None


@dataclass(frozen=True)
class AddrSuggestion:
    """The command that adds an address, and how its parts were chosen."""

    command: str
    interface: str
    prefix_len: int
    notes: tuple[str, ...] = ()


def suggest_ip_addr_add(ip: str, dest_ip: str | None = None, prefix_len: int | None = None) -> AddrSuggestion:
    """Work out ``sudo ip addr add <ip>/<prefix> dev <interface>`` (see :func:`ip_addr_add_command`)."""
    addr = _ipv4(ip)
    addrs = interface_addresses()
    notes: list[str] = []
    iface: str | None = None
    if dest_ip:
        iface = route_interface(dest_ip)
        if iface:
            notes.append(f"interface {iface} is the one this host uses to reach {dest_ip}")
        else:
            notes.append(f"no route to {dest_ip} found")
    if iface is None or iface == "lo":
        containing = [a for a in addrs if addr in a.network and a.interface != "lo"]
        if containing:
            iface = containing[0].interface
            notes.append(
                f"interface {iface} already has {containing[0].ip}/{containing[0].prefix_len}, whose subnet contains {ip}"
            )
    if iface is None or iface == "lo":
        iface = "<interface>"
        notes.append(
            "could not determine the interface: replace <interface> with the one facing the IED (see `ip addr`)"
        )
    if prefix_len is None:
        on_iface = [a for a in addrs if a.interface == iface]
        inside = [a for a in on_iface if addr in a.network]
        if inside:
            prefix_len = inside[0].prefix_len
            notes.append(f"prefix /{prefix_len} taken from {inside[0].ip}/{inside[0].prefix_len} on {iface}")
        else:
            prefix_len = DEFAULT_PREFIX_LEN
            why = (
                f"{ip} is outside the subnets already on {iface} ("
                + ", ".join(f"{a.ip}/{a.prefix_len}" for a in on_iface)
                + ")"
                if on_iface
                else f"no existing address on {iface}"
            )
            notes.append(
                f"prefix /{DEFAULT_PREFIX_LEN} assumed because {why}: check the prefix used in the rack"
            )
    return AddrSuggestion(
        f"sudo ip addr add {addr}/{prefix_len} dev {iface}", iface, prefix_len, tuple(notes)
    )


def ip_addr_add_command(ip: str, dest_ip: str | None = None, prefix_len: int | None = None) -> str:
    """The exact command the operator must run to add ``ip``, e.g. ``sudo ip addr add 10.0.0.50/24 dev eth0``.

    The interface is the one routing to ``dest_ip`` (else the one whose subnet contains ``ip``);
    the prefix comes from an existing address on that interface where possible, else /24 (the
    assumption is stated by :func:`suggest_ip_addr_add` and :func:`check_bind_address`).
    """
    return suggest_ip_addr_add(ip, dest_ip, prefix_len).command


@dataclass(frozen=True)
class BindCheck:
    """Result of :func:`check_bind_address`. ``command`` is set when the address must be added."""

    ok: bool
    ip: str
    message: str
    command: str | None = None
    interface: str | None = None
    warnings: tuple[str, ...] = ()
    error: ErrorInfo | None = None
    notes: tuple[str, ...] = field(default=())

    def to_json(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "ip": self.ip,
            "message": self.message,
            "command": self.command,
            "interface": self.interface,
            "warnings": list(self.warnings),
            "notes": list(self.notes),
            "error": self.error.to_json() if self.error else None,
        }


def check_bind_address(ip: str, dest_ip: str | None = None) -> BindCheck:
    """NBR-2: is ``ip`` configured here, so that the association can be bound to it?

    If not, ``message`` contains the exact ``ip addr add`` command (also in ``command``). If it is
    present but the kernel would reach ``dest_ip`` through another interface, the check still
    passes with a warning. Never raises.
    """
    try:
        addr = _ipv4(ip)
    except ValueError as e:
        return BindCheck(
            False,
            ip,
            f"{e}: give the local address as a dotted IPv4 address",
            error=codes.tool("local-address-missing"),
        )
    addrs = interface_addresses()
    here = [a for a in addrs if a.ip == str(addr)]
    if addr.is_loopback and not here:
        here = [InterfaceAddress("lo", str(addr), 8)]
    if here:
        iface = here[0].interface
        warnings: list[str] = []
        if dest_ip:
            out_iface = route_interface(dest_ip, source_ip=str(addr))
            if out_iface and out_iface != iface and iface != "lo":
                warnings.append(
                    f"{addr} is on {iface}, but traffic to {dest_ip} leaves through {out_iface}: "
                    f"the IED may never see {addr} as the source — add the address to {out_iface} instead"
                )
        msg = f"local address {addr} is present on {iface}"
        if warnings:
            msg += "; warning: " + "; ".join(warnings)
        return BindCheck(True, str(addr), msg, interface=iface, warnings=tuple(warnings))
    s = suggest_ip_addr_add(str(addr), dest_ip)
    msg = (
        f"local address {addr} is not configured on this host, so the association cannot be bound to it. "
        f"Add it with: {s.command}"
    )
    if s.notes:
        msg += " (" + "; ".join(s.notes) + ")"
    return BindCheck(
        False, str(addr), msg, s.command, None if s.interface == "<interface>" else s.interface,
        error=codes.tool("local-address-missing"), notes=s.notes,
    )  # fmt: skip
