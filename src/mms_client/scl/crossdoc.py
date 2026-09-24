"""Cross-IED helpers: which client IED an RCB is assigned to, and who is a client of whom.

These answer VER-3 / INV-2 questions from an SCD: "which client uses this RCB", "what is its
IP address", and "which RCBs does each client expect on each server".
"""

from __future__ import annotations

from dataclasses import dataclass, field

from mms_client.scl.errors import SclError
from mms_client.scl.expand import expand_server
from mms_client.scl.expected import ExpectedRCB, ExpectedServer
from mms_client.scl.model import IED, LN, AccessPoint, ClientLN, ConnectedAP, SclDocument


@dataclass(frozen=True, slots=True)
class ResolvedClient:
    """A ClientLN resolved against the document.

    ``ied`` is None when the client IED is not in the document (typical for a CID, which
    only describes one IED). ``ln_found`` says whether the client LN exists on that IED.
    ``connected_ap`` is the client's ConnectedAP (on ``apRef`` if given, else the AP that
    holds the LN, else the IED's default), which gives its IP address.
    """

    client: ClientLN
    ied: IED | None
    access_point: AccessPoint | None
    ln_found: bool
    connected_ap: ConnectedAP | None
    problems: tuple[str, ...] = ()

    @property
    def ied_name(self) -> str:
        return self.client.ied_name

    @property
    def ip(self) -> str | None:
        return self.connected_ap.ip if self.connected_ap else None

    @property
    def resolved(self) -> bool:
        return self.ied is not None and self.ln_found


def _find_ln(ap: AccessPoint, client: ClientLN) -> LN | None:
    candidates: list[LN] = list(ap.lns)
    if ap.server is not None:
        for ld in ap.server.ldevices:
            if client.ld_inst and ld.inst != client.ld_inst:
                continue
            candidates.extend(ld.all_lns)
    for ln in candidates:
        if client.ln_class == "LLN0" and ln.is_ln0:
            return ln
        if (not ln.is_ln0 and ln.ln_class == client.ln_class and ln.prefix == client.prefix
                and ln.inst == client.ln_inst):
            return ln
    return None


def resolve_client_ln(doc: SclDocument, client: ClientLN) -> ResolvedClient:
    """Resolve a ClientLN (``iedName`` + LN, optional ``apRef``) to the client IED."""
    problems: list[str] = []
    ied = doc.ied(client.ied_name)
    if ied is None:
        return ResolvedClient(client, None, None, False, None,
                              (f"client IED {client.ied_name!r} is not in {doc.source}",))
    aps = ied.access_points
    if client.ap_ref:
        aps = [ap for ap in aps if ap.name == client.ap_ref]
        if not aps:
            problems.append(f"client IED {ied.name!r} has no access point {client.ap_ref!r}")
    found_ap: AccessPoint | None = None
    for ap in aps:
        if _find_ln(ap, client) is not None:
            found_ap = ap
            break
    if found_ap is None:
        problems.append(f"client LN {client.label} not found on IED {ied.name!r}")
    ap = found_ap or (aps[0] if aps else None)
    cap = doc.connected_ap(ied.name, ap.name) if ap else None
    if cap is None:
        cap = doc.connected_ap(ied.name)
    if cap is None:
        problems.append(f"client IED {ied.name!r} has no ConnectedAP")
    return ResolvedClient(client, ied, ap, found_ap is not None, cap, tuple(problems))


_CLIENT_LN_CLASSES = frozenset({"IHMI", "ITCI", "IARC", "ITMI"})
_BAY_LN_CLASSES = frozenset({"CSWI", "CILO"})


def _ln_classes(ied: IED) -> set[str]:
    out: set[str] = set()
    for ap in ied.access_points:
        out.update(ln.ln_class for ln in ap.lns)
        if ap.server is not None:
            for ld in ap.server.ldevices:
                out.update(ln.ln_class for ln in ld.lns)
    return out


def role_hint(doc: SclDocument, ied_name: str) -> tuple[str, str]:
    """A guess at an IED's rack role for an inventory draft (INV-1 / INV-4): ``(role, reason)``.

    ``station-manager`` if other IEDs assign RCBs to it or it has HMI/telecontrol LNs,
    ``bcu`` if it has switchgear control / interlocking LNs (CSWI, CILO), else ``ied``. This
    is a hint for the operator to confirm, not a fact.
    """
    ied = doc.ied(ied_name)
    if ied is None:
        raise SclError(f"IED {ied_name!r} not found", source=doc.source)
    classes = _ln_classes(ied)
    referenced = any(
        cl.ied_name == ied_name
        for other in doc.ieds
        if other.name != ied_name
        for ap in other.access_points
        if ap.server is not None
        for ld in ap.server.ldevices
        for ln in ld.all_lns
        for rc in ln.report_controls
        for cl in rc.client_lns
    )
    if referenced:
        return "station-manager", "other IEDs assign report control blocks to it (ClientLN)"
    client_lns = sorted(classes & _CLIENT_LN_CLASSES)
    if client_lns:
        return "station-manager", f"has client-side logical nodes {', '.join(client_lns)}"
    bay_lns = sorted(classes & _BAY_LN_CLASSES)
    if bay_lns:
        return "bcu", f"has bay control logical nodes {', '.join(bay_lns)}"
    return "ied", "no station-manager or bay-control logical nodes"


@dataclass(slots=True)
class ClientRelationship:
    """One client IED's expectations on one server IED (from the server's ClientLNs)."""

    client_ied: str
    server_ied: str
    server_ap: str
    client_ip: str | None
    server_ip: str | None
    client_lns: list[str] = field(default_factory=list)
    rcbs: list[ExpectedRCB] = field(default_factory=list)
    client_in_document: bool = True


def client_relationships(doc: SclDocument, servers: list[ExpectedServer] | None = None) -> list[ClientRelationship]:
    """Every (client IED, server IED) pair found in ClientLN assignments, in document order.

    ``servers`` may pass already-expanded servers; otherwise every IED with a server is
    expanded non-strictly (IEDs whose server cannot be expanded are skipped).
    """
    if servers is None:
        servers = []
        for ied in doc.ieds:
            try:
                servers.append(expand_server(doc, ied.name, strict=False))
            except SclError:
                continue
    out: dict[tuple[str, str], ClientRelationship] = {}
    for srv in servers:
        for rcb in srv.rcbs:
            for cl in rcb.clients:
                key = (cl.ied_name, srv.ied_name)
                rel = out.get(key)
                if rel is None:
                    res = resolve_client_ln(doc, cl)
                    rel = ClientRelationship(client_ied=cl.ied_name, server_ied=srv.ied_name,
                                             server_ap=srv.ap_name, client_ip=res.ip, server_ip=srv.ip,
                                             client_in_document=res.ied is not None)
                    out[key] = rel
                if cl.label not in rel.client_lns:
                    rel.client_lns.append(cl.label)
                rel.rcbs.append(rcb)
    return list(out.values())
