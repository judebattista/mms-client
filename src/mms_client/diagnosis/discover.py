"""``discover <subnet>`` (DIA-5): find MMS servers and draft an inventory.

Strictly sequential and rate-limited: one host at a time, a TCP connect to the MMS port with a
short timeout, then ``delay_s`` before the next host. For each responder, one brief association
through the adapter reads MMS Identify and the logical device (domain) names, and is **released
before moving on**: :meth:`IedClient.release` is called explicitly and its result recorded (it
returns only when the device has confirmed the conclude/release, or reports why not), then the
connection is closed. A responder whose association fails is probed layer by layer (one more
attempt, also ended cleanly) so that the draft says why.

Each scanned host costs at most three short TCP connections (scan, association, and the probe on
failure); on a device whose slots are all in use, each of them is refused without effect.
"""

from __future__ import annotations

import ipaddress
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from mms_client.adapter import ConnectError, IedClient, ServerIdentity, ServiceError
from mms_client.codes import ErrorInfo

from .classify import AssociationFailure, classify_association
from .probes import TLS_CHECK_OUTCOMES, ProbeResult, iso_associate, tcp_connect, tls_port_open

MAX_PREFIX_DEFAULT = 22  # refuse networks larger than a /22 (1024 addresses) unless allow_large


@dataclass
class DiscoveredDevice:
    """One scanned address. ``identity`` is from MMS Identify; ``release`` is the release result."""

    ip: str
    port: int
    tcp_open: bool
    identity: ServerIdentity | None = None
    logical_devices: list[str] = field(default_factory=list)
    error: ErrorInfo | None = None
    classification: AssociationFailure | None = None
    tcp: ProbeResult | None = None
    release: ErrorInfo | None = None
    notes: list[str] = field(default_factory=list)
    duration_s: float = 0.0

    @property
    def associated(self) -> bool:
        """An association was established (identity or logical devices were read)."""
        return self.identity is not None or bool(self.logical_devices)

    def default_name(self) -> str:
        return "ied-" + self.ip.replace(".", "-")

    def to_inventory_entry(self, name: str | None = None) -> dict[str, Any]:
        """A draft inventory device entry (identity marked ``source: discover``)."""
        ident = self.identity
        return {
            "name": name or self.default_name(),
            "ip": self.ip,
            "port": self.port,
            "role": "unknown",
            "identity": {
                "vendor": ident.vendor if ident else None,
                "model": ident.model if ident else None,
                "firmware": ident.revision if ident else None,
                "source": "discover",
            },
            "logical_devices": list(self.logical_devices),
        }

    def to_json(self) -> dict[str, Any]:
        return {
            "ip": self.ip,
            "port": self.port,
            "tcp_open": self.tcp_open,
            "identity": self.identity.to_json() if self.identity else None,
            "logical_devices": list(self.logical_devices),
            "error": self.error.to_json() if self.error else None,
            "classification": self.classification.to_json() if self.classification else None,
            "tcp": self.tcp.to_json() if self.tcp else None,
            "release": self.release.to_json() if self.release else None,
            "notes": list(self.notes),
            "duration_s": round(self.duration_s, 6),
        }


@dataclass(frozen=True)
class DiscoverProgress:
    """Passed to the ``progress`` callback after each host."""

    index: int  # 1-based
    total: int
    ip: str
    device: DiscoveredDevice


def scan_targets(subnet: str, *, allow_large: bool = False) -> list[str]:
    """The IPv4 host addresses of ``subnet`` (``10.0.0.0/24``, ``10.0.0.7/32`` or a bare address).

    Raises ValueError for IPv6, invalid input, or networks larger than /22 without ``allow_large``.
    """
    try:
        net = ipaddress.ip_network(subnet.strip(), strict=False)
    except ValueError as e:
        raise ValueError(f"{subnet!r} is not an IPv4 network (e.g. 10.0.0.0/24): {e}") from e
    if net.version != 4:
        raise ValueError(f"{subnet!r}: only IPv4 networks can be scanned")
    if net.prefixlen < MAX_PREFIX_DEFAULT and not allow_large:
        raise ValueError(
            f"{subnet} has {net.num_addresses} addresses; networks larger than /{MAX_PREFIX_DEFAULT} are refused "
            "(the scan is sequential and rate-limited: narrow it down, or allow a large scan explicitly)"
        )
    hosts = list(net.hosts()) if net.prefixlen < 31 else list(net)
    return [str(h) for h in hosts]


def _associate(
    ip: str,
    port: int,
    dev: DiscoveredDevice,
    timeout_s: float,
    request_timeout_s: float,
    local_ip: str | None,
) -> None:
    """One brief association: Identify, domain names, then a confirmed release (or an abort)."""
    c = IedClient(connect_timeout_ms=int(timeout_s * 1000), request_timeout_ms=int(request_timeout_s * 1000))
    try:
        c.connect(ip, port, local_ip=local_ip)
    except ConnectError as e:
        dev.error = e.error
        c.close()
        return
    released = False
    try:
        try:
            dev.identity = c.identify()
        except ServiceError as e:
            dev.error = e.error
            dev.notes.append(f"identify failed: {e}")
        try:
            dev.logical_devices = c.get_domain_names()
        except ServiceError as e:
            dev.error = dev.error or e.error
            dev.notes.append(f"reading the logical devices failed: {e}")
        # release() returns only after the conclude and the ACSE release have been answered
        # (conclude-Response, then DISCONNECT with RLRE).
        dev.release = c.release()
        released = dev.release.code == 0
        if not released:
            abort = c.abort()
            dev.notes.append(f"release not confirmed ({dev.release}); association aborted ({abort})")
    finally:
        # After a confirmed release the connection is already closed on the wire; a graceful
        # close would try to release a second time and wait for the request timeout.
        c.close(graceful=not released)


def discover(
    subnet: str,
    *,
    port: int = 102,
    timeout_s: float = 1.0,
    delay_s: float = 0.2,
    local_ip: str | None = None,
    associate: bool = True,
    progress: Callable[[DiscoverProgress], None] | None = None,
    allow_large: bool = False,
    include_closed: bool = False,
    request_timeout_s: float = 3.0,
    probe_failures: bool = True,
) -> list[DiscoveredDevice]:
    """Scan ``subnet`` sequentially for TCP ``port``; associate briefly with each responder.

    Returns the responders (every scanned address with ``include_closed``), in address order.
    ``delay_s`` is waited between hosts. With ``associate=False`` only TCP is tested.
    """
    targets = scan_targets(subnet, allow_large=allow_large)
    found: list[DiscoveredDevice] = []
    for i, ip in enumerate(targets, 1):
        t0 = time.monotonic()
        tcp = tcp_connect(ip, port, timeout_s, local_ip)
        dev = DiscoveredDevice(ip, port, tcp.ok is True, tcp=tcp)
        if tcp.ok is None and tcp.error is not None:
            dev.error = tcp.error  # e.g. tool:local-address-missing: nothing was sent
        if dev.tcp_open and associate:
            _associate(ip, port, dev, timeout_s, request_timeout_s, local_ip)
            if dev.error is not None and dev.identity is None and not dev.logical_devices and probe_failures:
                assoc = iso_associate(ip, port, max(timeout_s, 1.0), local_ip, linger_s=0.2)
                tls = (
                    tls_port_open(ip, timeout_s, local_ip=local_ip)
                    if assoc.outcome in TLS_CHECK_OUTCOMES
                    else None
                )
                dev.classification = classify_association(tcp, None, assoc, tls)
        dev.duration_s = time.monotonic() - t0
        if dev.tcp_open or include_closed:
            found.append(dev)
        if progress is not None:
            progress(DiscoverProgress(i, len(targets), ip, dev))
        if i < len(targets) and delay_s > 0:
            time.sleep(delay_s)
    return found


def draft_inventory(devices: Iterable[DiscoveredDevice]) -> dict[str, Any]:
    """A draft inventory from discovered devices (only those with TCP open), names ``ied-a-b-c-d``."""
    entries: list[dict[str, Any]] = []
    used: set[str] = set()
    for d in devices:
        if not d.tcp_open:
            continue
        name = d.default_name()
        if name in used:
            name = f"{name}-{d.port}"
        used.add(name)
        entries.append(d.to_inventory_entry(name))
    return {"schema_version": 1, "safety": "strict", "devices": entries, "clients": []}
