"""Inventory operations that need other packages: ``inventory from-scd`` (INV-4), ``inventory
validate`` (INV-4) and writing discovered identities back (IDN-6)."""

from __future__ import annotations

import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ied_client.inventory import ClientRelation, Device, IdentityOverride, Inventory, Reference
from ied_client.protocol.errors import ConnectError, ServiceError
from ied_client.protocol.registry import get as get_protocol
from ied_client.scl import client_relationships, edition_of, expand_server, load_scl, role_hint


def _slug(name: str) -> str:
    return "".join(ch.lower() if ch.isalnum() else "-" for ch in name).strip("-") or "device"


def from_scd(
    scd_path: str | Path, *, experiment: str | None = None, out_path: Path | None = None, protocol: str | None = None
) -> Inventory:
    """Draft inventory from an SCD: one device per IED with a server and an IP address, one client
    relationship per (client IED, server IED) pair from the ClientLN assignments. The devices get
    ``protocol`` (default: the default protocol) and its port."""
    module = get_protocol(protocol)
    path = Path(scd_path).resolve()
    doc = load_scl(path)
    inv = Inventory(path=out_path.resolve() if out_path else None, experiment=experiment or path.stem)
    names: dict[str, str] = {}
    servers = []
    for ied in doc.ieds:
        cap = doc.connected_ap(ied.name)
        try:
            srv = expand_server(doc, ied.name, strict=False)
            servers.append(srv)
        except Exception:
            srv = None
        if cap is None or not cap.ip:
            inv.warnings.append(f"IED {ied.name}: no IP address in the Communication section; not added as a device")
            continue
        role, why = role_hint(doc, ied.name)
        ed = edition_of(doc, ied.name)
        dev = Device(
            name=_slug(ied.name),
            ip=cap.ip,
            port=module.default_port,
            protocol=module.name,
            role=role if role in ("station-manager", "bcu", "ied", "gateway") else "ied",
            reference=Reference("scd", path, ied.name) if srv is not None and srv.lds else None,
            identity=IdentityOverride(vendor=ied.manufacturer, model=ied.type, edition=None),
            extra={"scl": {"ied": ied.name, "edition": ed.edition, "edition_reason": ed.reason, "role_reason": why}},
        )
        # Edition from the SCL is recorded as information, not as an operator override: the
        # identity pipeline takes it from the reference (IDN-2 step 2).
        names[ied.name] = dev.name
        inv.devices.append(dev)
    for rel in client_relationships(doc, servers):
        server = names.get(rel.server_ied)
        if server is None:
            continue
        inv.clients.append(
            ClientRelation(
                name=f"{_slug(rel.client_ied)}-to-{server}",
                client=names.get(rel.client_ied, _slug(rel.client_ied)),
                server=server,
                source_ip=rel.client_ip,
                rcbs=[r.ref for r in rel.rcbs],
            )
        )
    return inv


# --------------------------------------------------------------------------------- validate
@dataclass
class ValidationItem:
    subject: str
    check: str
    status: str  # pass | fail | warn | info
    message: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict:
        return {"subject": self.subject, "check": self.check, "status": self.status, "message": self.message, "evidence": self.evidence}


def validate(inv: Inventory, *, timeout_s: float = 2.0, associate: bool = True) -> list[ValidationItem]:
    """INV-4: check the inventory against the network. Sequential; each association is released
    before the next one (association slots are limited)."""
    out: list[ValidationItem] = []
    for w in inv.warnings:
        out.append(ValidationItem("inventory", "schema", "warn", w))
    for d in inv.devices:
        if d.reference is not None and not d.reference.path.exists():
            out.append(ValidationItem(d.name, "reference-file", "fail", f"reference file {d.reference.path} does not exist"))
        t0 = time.perf_counter()
        try:
            with socket.create_connection((d.ip, d.port), timeout=timeout_s):
                pass
            out.append(ValidationItem(d.name, "tcp", "pass", f"TCP {d.ip}:{d.port} open", {"ms": round((time.perf_counter() - t0) * 1000, 1)}))
        except OSError as e:
            out.append(ValidationItem(d.name, "tcp", "fail", f"TCP {d.ip}:{d.port}: {e.strerror or e}"))
            continue
        if not associate:
            continue
        try:
            c = get_protocol(d.protocol).open(
                d.ip, d.port, connect_timeout_ms=int(timeout_s * 1000), request_timeout_ms=int(timeout_s * 2000)
            )
        except ConnectError as e:
            out.append(ValidationItem(d.name, "associate", "fail", f"association failed: {e.error}"))
            continue
        try:
            ident = c.identify()
            lds = c.logical_devices()
            out.append(ValidationItem(d.name, "associate", "pass", f"identify: {ident.vendor} {ident.model} {ident.revision}",
                                      {"identity": ident.to_json(), "logical_devices": lds}))
            ov = d.identity
            for attr, got in (("vendor", ident.vendor), ("model", ident.model), ("firmware", ident.revision)):
                want = getattr(ov, attr)
                if want and got and str(want).strip().lower() != str(got).strip().lower():
                    out.append(ValidationItem(d.name, f"identity-{attr}", "warn", f"inventory says {want!r}, device says {got!r}"))
            if d.reference is not None and d.reference.kind in ("scd", "cid") and d.reference.path.exists():
                try:
                    doc = load_scl(d.reference.path)
                    exp = expand_server(doc, d.reference.ied or doc.ied_names[0], strict=False)
                    missing = sorted(set(exp.domains) - set(lds))
                    status = "fail" if missing else "pass"
                    out.append(ValidationItem(d.name, "logical-devices", status,
                                              "all reference logical devices present" if not missing else f"missing: {', '.join(missing)}"))
                except Exception as e:
                    out.append(ValidationItem(d.name, "reference-file", "fail", f"cannot use reference: {e}"))
        except ServiceError as e:
            out.append(ValidationItem(d.name, "associate", "warn", f"associated, but {e}"))
        finally:
            c.close()
    for rel in inv.clients:
        if rel.source_ip:
            try:
                from ied_client.diagnosis.net import is_local_address

                local = is_local_address(rel.source_ip)
            except ImportError:  # pragma: no cover
                local = None
            if local is False:
                out.append(ValidationItem(rel.name, "source-ip", "info",
                                          f"{rel.source_ip} is not on this machine; `--as {rel.client}` will ask you to add it"))
    return out


def write_back_identity(inv: Inventory, device_name: str, identity: Any, *, edition: str | None = None) -> Device:
    """IDN-6: store operator-supplied / discovered identity in the inventory (caller confirms and saves)."""
    dev = inv.device(device_name)
    if dev is None:
        raise KeyError(device_name)
    for attr in ("vendor", "model", "firmware"):
        a = getattr(identity, attr, None)
        if a is not None and getattr(a, "value", None) and not getattr(dev.identity, attr):
            setattr(dev.identity, attr, str(a.value))
    if edition and edition != "unknown":
        dev.identity.edition = edition
    return dev


def offer_to_save_edition(session: Any, attr: Any) -> str | None:
    """IDN-6: right after the operator has answered the edition question, offer to store the answer in the
    experiment inventory, so that the experiment is asked only once. Returns the inventory path if saved.

    Only offered interactively, with an inventory file that contains the device; declining changes nothing.
    """
    from ied_client.core.safety import ConfirmationDeclined

    inv = session.inventory
    dev = session.target.device
    edition = getattr(attr.value, "value", attr.value)
    if inv is None or inv.path is None or dev is None or not session.ui.interactive or dev.identity.edition == edition:
        return None
    try:
        session.policy.confirm_write(
            session.ui,
            f"Save edition {edition} for {dev.name} in the inventory {inv.path}, so that this experiment does not ask again?",
        )
    except ConfirmationDeclined:
        return None
    write_back_identity(inv, dev.name, None, edition=edition)
    inv.save()
    session.log.write("note", action="save-identity", inventory=str(inv.path), device=dev.name, edition=edition)
    return str(inv.path)
