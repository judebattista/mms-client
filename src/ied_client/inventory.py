"""Rack inventory: one YAML file per experiment (INV-1 … INV-6).

```yaml
schema_version: 1
experiment: feeder-trip-2026-09       # free text
safety: strict                        # strict (default) | lab           (SAF-1, INV-3)
devices:
  - name: relay-F12
    ip: 10.0.0.12
    protocol: mms                     # the protocol module that drives it (default: mms) (INV-6)
    port: 102                         # default: the protocol's port
    role: bcu                         # station-manager | bcu | ied | gateway | other
    reference:                        # optional; one of scd / cid / snapshot (VER-1)
      scd: ../scd/rack.scd
      ied: F12                        # IED name inside the SCD/CID
    identity:                         # optional operator overrides (IDN-2 step 1)
      vendor: ACME
      model: BCU-9000
      firmware: "2.4.1"
      edition: Ed2                    # Ed1 | Ed2 | Ed2.1
    security: {}                      # reserved for ACSE authentication / TLS; ignored by v1
    mms: {}                           # settings for the device's protocol module go under its name
clients:                              # client relationships (INV-2); orCat is NOT stored here
  - name: sm1-f12
    client: station-manager-1         # a device name above, or any label for an external client
    server: relay-F12
    source_ip: 10.0.0.5               # used by --as (NBR-1/NBR-2)
    rcbs: [CTRL/LLN0.BR.brcbA01]
    security: {}                      # reserved
```
Paths in ``reference`` are relative to the inventory file.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ied_client.protocol.registry import DEFAULT_PROTOCOL, UnknownProtocolError
from ied_client.protocol.registry import get as get_protocol

SCHEMA_VERSION = 1
ROLES = ("station-manager", "bcu", "ied", "gateway", "client", "other", "unknown")
EDITIONS = ("Ed1", "Ed2", "Ed2.1")


class InventoryError(ValueError):
    pass


@dataclass(slots=True)
class Reference:
    kind: str  # "scd" | "cid" | "snapshot"
    path: Path
    ied: str | None = None

    def to_yaml(self, base: Path | None) -> dict:
        p = self.path
        if base is not None:
            try:
                p = self.path.relative_to(base)
            except ValueError:
                pass
        d: dict[str, Any] = {self.kind: str(p)}
        if self.ied:
            d["ied"] = self.ied
        return d


@dataclass(slots=True)
class IdentityOverride:
    vendor: str | None = None
    model: str | None = None
    firmware: str | None = None
    edition: str | None = None

    def is_empty(self) -> bool:
        return not any((self.vendor, self.model, self.firmware, self.edition))

    def to_yaml(self) -> dict:
        return {k: getattr(self, k) for k in ("vendor", "model", "firmware", "edition") if getattr(self, k)}


@dataclass(slots=True)
class Device:
    name: str
    ip: str
    port: int
    role: str = "unknown"
    reference: Reference | None = None
    identity: IdentityOverride = field(default_factory=IdentityOverride)
    security: dict[str, Any] = field(default_factory=dict)  # reserved (INV-1)
    extra: dict[str, Any] = field(default_factory=dict)
    protocol: str = DEFAULT_PROTOCOL  # INV-6

    def to_yaml(self, base: Path | None) -> dict:
        d: dict[str, Any] = {"name": self.name, "ip": self.ip}
        if self.protocol != DEFAULT_PROTOCOL:
            d["protocol"] = self.protocol
        d.update(port=self.port, role=self.role)
        if self.reference:
            d["reference"] = self.reference.to_yaml(base)
        if not self.identity.is_empty():
            d["identity"] = self.identity.to_yaml()
        d["security"] = self.security or {}
        d.update(self.extra)
        return d


@dataclass(slots=True)
class ClientRelation:
    name: str
    client: str
    server: str
    source_ip: str | None = None
    rcbs: list[str] = field(default_factory=list)
    security: dict[str, Any] = field(default_factory=dict)  # reserved

    def to_yaml(self) -> dict:
        d: dict[str, Any] = {"name": self.name, "client": self.client, "server": self.server}
        if self.source_ip:
            d["source_ip"] = self.source_ip
        d["rcbs"] = list(self.rcbs)
        d["security"] = self.security or {}
        return d


@dataclass(slots=True)
class Inventory:
    path: Path | None = None
    experiment: str | None = None
    safety: str = "strict"
    devices: list[Device] = field(default_factory=list)
    clients: list[ClientRelation] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------ lookup (INV-5)
    def device(self, name_or_ip: str) -> Device | None:
        for d in self.devices:
            if d.name == name_or_ip:
                return d
        for d in self.devices:
            if d.ip == name_or_ip:
                return d
        return None

    def client(self, name: str, server: str | None = None) -> ClientRelation | None:
        """A client relationship by relation name, or by client name (+ server)."""
        for c in self.clients:
            if c.name == name and (server is None or c.server == server):
                return c
        for c in self.clients:
            if c.client == name and (server is None or c.server == server):
                return c
        return None

    def name_for_ip(self, ip: str) -> str | None:
        """Resolve an IP (e.g. from an RCB Owner) to an inventory name (RPT-1)."""
        for c in self.clients:
            if c.source_ip == ip:
                return c.client
        for d in self.devices:
            if d.ip == ip:
                return d.name
        return None

    def clients_of(self, server: str) -> list[ClientRelation]:
        return [c for c in self.clients if c.server == server]

    @property
    def base_dir(self) -> Path | None:
        return self.path.parent if self.path else None

    # ------------------------------------------------------------------ persistence
    def to_yaml(self) -> dict:
        d: dict[str, Any] = {"schema_version": SCHEMA_VERSION}
        if self.experiment:
            d["experiment"] = self.experiment
        d["safety"] = self.safety
        d["devices"] = [x.to_yaml(self.base_dir) for x in self.devices]
        d["clients"] = [c.to_yaml() for c in self.clients]
        return d

    def dump(self) -> str:
        return yaml.safe_dump(self.to_yaml(), sort_keys=False, allow_unicode=True)

    def save(self, path: Path | None = None) -> Path:
        target = path or self.path
        if target is None:
            raise InventoryError("no path to save the inventory to")
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(self.dump(), encoding="utf-8")
        tmp.replace(target)
        self.path = target
        return target


def _ip(value: Any, where: str) -> str:
    try:
        return str(ipaddress.ip_address(str(value)))
    except ValueError as exc:
        raise InventoryError(f"{where}: {value!r} is not an IP address") from exc


def parse_inventory(data: dict[str, Any], path: Path | None = None) -> Inventory:
    """Validate the YAML structure (schema level only; `inventory validate` checks the network)."""
    if not isinstance(data, dict):
        raise InventoryError("inventory must be a mapping")
    ver = data.get("schema_version", SCHEMA_VERSION)
    if ver != SCHEMA_VERSION:
        raise InventoryError(f"unsupported inventory schema_version {ver!r} (expected {SCHEMA_VERSION})")
    inv = Inventory(path=path, experiment=data.get("experiment"))
    safety = data.get("safety", "strict")
    if safety not in ("strict", "lab"):
        raise InventoryError(f"safety must be 'strict' or 'lab', not {safety!r}")
    inv.safety = safety
    base = path.parent if path else Path.cwd()
    names: set[str] = set()
    for i, d in enumerate(data.get("devices") or []):
        where = f"devices[{i}]"
        if not isinstance(d, dict) or "name" not in d or "ip" not in d:
            raise InventoryError(f"{where}: each device needs at least name and ip")
        name = str(d["name"])
        if name in names:
            raise InventoryError(f"{where}: duplicate device name {name!r}")
        names.add(name)
        ref = None
        if d.get("reference"):
            r = d["reference"]
            kinds = [k for k in ("scd", "cid", "snapshot") if k in r]
            if len(kinds) != 1:
                raise InventoryError(f"{where}.reference: give exactly one of scd, cid, snapshot")
            p = Path(str(r[kinds[0]]))
            ref = Reference(kinds[0], p if p.is_absolute() else (base / p), r.get("ied"))
        ident = d.get("identity") or {}
        if ident.get("edition") and ident["edition"] not in EDITIONS:
            raise InventoryError(f"{where}.identity.edition must be one of {EDITIONS}")
        role = str(d.get("role", "unknown"))
        if role not in ROLES:
            inv.warnings.append(f"{where}: unusual role {role!r} (known: {', '.join(ROLES)})")
        security = d.get("security") or {}
        if security:
            inv.warnings.append(f"{where}: 'security' settings are reserved and ignored by v1")
        protocol = str(d.get("protocol") or DEFAULT_PROTOCOL)
        try:
            module = get_protocol(protocol)
        except UnknownProtocolError as e:
            raise InventoryError(f"{where}.protocol: {e}") from e
        known = ("name", "ip", "port", "role", "reference", "identity", "security", "protocol")
        extra = {k: v for k, v in d.items() if k not in known}
        inv.devices.append(
            Device(
                name=name,
                ip=_ip(d["ip"], where + ".ip"),
                port=int(d.get("port", module.default_port)),
                protocol=protocol,
                role=role,
                reference=ref,
                identity=IdentityOverride(
                    vendor=ident.get("vendor"),
                    model=ident.get("model"),
                    firmware=None if ident.get("firmware") is None else str(ident.get("firmware")),
                    edition=ident.get("edition"),
                ),
                security=security,
                extra=extra,
            )
        )
    for i, c in enumerate(data.get("clients") or []):
        where = f"clients[{i}]"
        if not isinstance(c, dict) or "client" not in c or "server" not in c:
            raise InventoryError(f"{where}: each client relationship needs client and server")
        if "orcat" in {k.lower() for k in c}:
            raise InventoryError(f"{where}: orCat is chosen per command, not stored in the inventory (§10.3)")
        server = str(c["server"])
        if server not in names:
            inv.warnings.append(f"{where}: server {server!r} is not a device in this inventory")
        inv.clients.append(
            ClientRelation(
                name=str(c.get("name") or f"{c['client']}-{server}"),
                client=str(c["client"]),
                server=server,
                source_ip=_ip(c["source_ip"], where + ".source_ip") if c.get("source_ip") else None,
                rcbs=[str(x) for x in (c.get("rcbs") or [])],
                security=c.get("security") or {},
            )
        )
    return inv


def load_inventory(path: str | Path) -> Inventory:
    p = Path(path)
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except OSError as exc:
        raise InventoryError(f"cannot read inventory {p}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise InventoryError(f"{p}: invalid YAML: {exc}") from exc
    return parse_inventory(data, p.resolve())
