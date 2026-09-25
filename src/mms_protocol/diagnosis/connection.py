"""The MMS module's part of `diagnose` (PROTO-9, PROTO-10, PROTO-12, IDN-8).

Layers, in order, each stopping the diagnosis when it fails::

    network (ICMP, TCP 102, TLS port 3782 when relevant) → transport-session (COTP, ISO session,
    presentation, ACSE) → mms-initiate (PDU size, services) → association through libiec61850
"""

from __future__ import annotations

import time
from typing import Any

from ied_client import codes
from ied_client.core.results import Category, CheckResult, Status
from ied_client.core.safety import PolicyError
from ied_client.core.session import Session
from ied_client.diagnosis.diagnose import DiagnosisReport, Finding, LayerOutcome, Verdict
from ied_client.diagnosis.probe import ProbeResult
from ied_client.protocol.errors import ConnectError
from ied_client.quirks import QuirkInfo, load_quirks

from ..codes import MmsDomain
from .classify import (
    AssociationFailure,
    assess_association_limits,
    classify_association,
    explain_ied_connect_error,
)
from .probes import run_layered_probes

COMM = Category.COMMUNICATION
CONF = Category.CONFIGURATION

LAYERS = ("network", "transport-session", "mms-initiate", "association")

NEXT_STEPS: dict[str, list[str]] = {
    "network": [
        "Check the IP address in the inventory against the device's front panel / configuration tool.",
        "Check cabling, switch port, VLAN and that this machine is in the same subnet (ip addr, ip route).",
        "If the device answers ping but not TCP 102, its MMS server may be disabled or on another port.",
    ],
    "transport-session": [
        "Compare the device's OSI selectors / AP titles (in its SCD/CID Address section) with the client's.",
        "Check whether the device requires security (TLS or ACSE authentication), which v1 does not support.",
        "Save an incident file (export --incident) so the pattern can be added to the catalogue.",
    ],
    "mms-initiate": [
        "Look at the negotiated parameters: a very small max PDU size or missing services can break clients.",
        "Compare with what the real client (e.g. the Station Manager) negotiates, using a packet capture.",
    ],
    "association": [
        "Our raw probe was accepted but libiec61850's association was not: compare the parameters in the "
        "probe evidence with the device's requirements and save an incident file.",
    ],
}

REMINDER_KEYS = frozenset({"tool:association-slot-occupied-by-tool", "tool:known-association-limit"})
VERDICT_PRIORITY = (
    "association:tls-suspected",
    "association:authentication-suspected",
    "tool:security-not-supported",
    "tool:slots-probably-exhausted",
)


def _probe_result(p: ProbeResult, check_id: str, category: Category = COMM) -> CheckResult:
    status = Status.PASS if p.ok else (Status.INFO if p.ok is None else Status.FAIL)
    return CheckResult(
        check_id,
        p.name,
        status,
        category,
        subject=p.name,
        message=p.detail,
        evidence={"outcome": p.outcome, "duration_s": round(p.duration_s, 4), **({"raw": p.raw} if p.raw else {})},
        error=p.error,
    )


def quirks_for(session: Session) -> QuirkInfo | None:
    """The known quirks of the session's device (IDN-9), by the identity read so far or the inventory's."""
    from mms_protocol import protocol

    ident = session.identity
    ov = getattr(session.target.device, "identity", None)
    vendor = (ident.vendor.value if ident else None) or getattr(ov, "vendor", None)
    model = (ident.model.value if ident else None) or getattr(ov, "model", None)
    fw = (ident.firmware.value if ident else None) or getattr(ov, "firmware", None)
    if not vendor and not model:
        return None
    try:
        # resolve(): a firmware-specific entry must not hide model-wide limits
        return load_quirks(sources=protocol.quirks_sources()).resolve(vendor, model, fw)
    except Exception:
        return None


def best_finding(findings: list[Finding]) -> Finding | None:
    """The finding that best states the most likely cause (reminders are never the verdict)."""
    cands = [f for f in findings if f.key not in REMINDER_KEYS]
    if not cands:
        return None
    order = {"fact": 0, "likely": 1, "check": 2}

    def rank(f: Finding) -> tuple[int, int]:
        pri = VERDICT_PRIORITY.index(f.key) if f.key in VERDICT_PRIORITY else len(VERDICT_PRIORITY)
        return (pri, order.get(str(f.certainty), 3))

    return min(cands, key=rank)


class MmsConnectionDiagnosis:
    """One `diagnose` run's MMS layers. Keeps the probes' classification for :meth:`after_identity`."""

    def __init__(self) -> None:
        self.failure: AssociationFailure | None = None
        self.quirk: QuirkInfo | None = None

    def run(self, session: Session, rep: DiagnosisReport, *, timeout_s: float, icmp: bool) -> bool:
        t = session.target
        # -- raw probes (DIA-3)
        probes = run_layered_probes(t.host, t.port, timeout=timeout_s, local_ip=t.local_ip, icmp=icmp)
        rep.probes = probes.to_json()
        failure = classify_association(probes.tcp, probes.cotp, probes.association, probes.tls, icmp=probes.icmp)
        self.failure = failure
        rep.association_failure = failure.to_json()
        net = LayerOutcome("network", Status.PASS)
        if probes.icmp is not None:
            net.results.append(_probe_result(probes.icmp, "icmp"))
        net.results.append(_probe_result(probes.tcp, "tcp-102"))
        if probes.tls is not None:
            net.results.append(_probe_result(probes.tls, "tcp-3782"))
        rep.layers.append(net)
        steps = probes.association.steps if probes.association else []
        ts = LayerOutcome("transport-session", Status.PASS)
        mms = LayerOutcome("mms-initiate", Status.PASS)
        for s in steps:
            if s.name.startswith("tcp"):
                continue
            (mms if s.layer == "mms" else ts).results.append(_probe_result(s, s.name))
        if probes.association and probes.association.initiate is not None:
            mms.summary = probes.association.detail
        self.quirk = quirk = quirks_for(session)
        rep.findings.extend(failure.findings)
        if not failure.ok:
            layer = {"network": "network", "transport": "transport-session", "session": "transport-session",
                     "presentation": "transport-session", "acse": "transport-session", "mms": "mms-initiate"}.get(
                failure.layer or "", "network")
            for lay in (net, ts, mms):
                if lay.layer == layer:
                    lay.status = Status.FAIL
            if layer != "network":
                rep.layers.append(ts)
            if layer == "mms-initiate":
                rep.layers.append(mms)
            rep.findings.extend(assess_association_limits(failure, quirks=quirk, tool_holds_association=False))
            best = best_finding(rep.findings)
            label = codes.ASSOCIATION_OUTCOMES.get(failure.outcome, failure.outcome)
            text = best.text if best else (failure.detail if failure.detail.startswith(label) else f"{label}: {failure.detail}")
            cert = best.certainty if best else "fact"
            if failure.security:
                text = (
                    ("The device appears to require TLS (IEC 62351): TCP 102 is closed but TCP 3782 answers."
                     if failure.security == "tls"
                     else "The device appears to require ACSE authentication.")
                    + " mms-client v1 does not support MMS security, so it cannot associate with this device."
                )
                cert = "likely"
            extra = []
            if best is not None and best.key == "tool:slots-probably-exhausted":
                extra = [
                    "Close other MMS clients and tools connected to this device (each holds one association slot).",
                    "If a client disconnected abruptly (power loss, cable pulled), wait for the device to drop the stale "
                    "association (see the quirks file for the device's timeout), or restart that client cleanly.",
                    "Record the device's maximum association count in the quirks file once known (IDN-9).",
                ]
            rep.stop(layer, Verdict(text, cert, failure.error.key, layer), extra)
            return False
        rep.layers.extend([ts, mms])
        # -- association through libiec61850
        assoc = LayerOutcome("association", Status.PASS)
        rep.layers.append(assoc)
        t_assoc = time.monotonic()
        try:
            session.connect()
            params = session.require_client().client.connection_params()
            assoc.results.append(
                CheckResult("association", "Association through libiec61850", Status.PASS, COMM,
                            message=f"associated; max PDU {params.max_pdu_size}, services: {', '.join(params.supported_services())}",
                            evidence=params.to_json())
            )
            rep.findings.extend(
                f for f in assess_association_limits(failure, quirks=quirk, tool_holds_association=True)
                if f.key != "association:accepted"
            )
        except (ConnectError, PolicyError) as e:
            err = getattr(e, "error", None)
            assoc.status = Status.FAIL
            assoc.results.append(
                CheckResult("association", "Association through libiec61850", Status.FAIL, COMM, error=err, message=str(e))
            )
            note = (
                explain_ied_connect_error(err, elapsed_s=time.monotonic() - t_assoc, timeout_s=session.connect_timeout_ms / 1000)
                if err is not None and err.domain == MmsDomain.IED
                else str(e)
            )
            rep.stop("association", Verdict(
                f"The raw association probe was accepted, but libiec61850's association failed ({err}). {note}", "likely",
                err.key if err else None, "association"))
            return False
        return True

    def after_identity(self, session: Session, rep: DiagnosisReport, layer: LayerOutcome) -> None:
        quirk = quirks_for(session) or self.quirk
        if quirk is not None and self.failure is not None:
            rep.findings.extend(f for f in assess_association_limits(self.failure, quirks=quirk, tool_holds_association=True)
                                if f.key not in {x.key for x in rep.findings})
        _edition_compat(session, layer)


def _edition_compat(session: Session, lay: LayerOutcome) -> None:
    """IDN-8: when client and server editions differ, check object reference lengths. The limits must
    come from the standard (OI-10); until they are configured the check reports not-run."""
    client = session.target.as_client
    if client is None or session.inventory is None:
        return
    cdev = session.inventory.device(client.client)
    c_ed = cdev.identity.edition if cdev is not None else None
    s_ed = session.edition
    if not c_ed or s_ed == "unknown" or c_ed == s_ed:
        return
    lay.results.append(CheckResult(
        "object-reference-too-long", "Object references fit the client's edition", Status.NOT_RUN, CONF,
        reason=f"client {client.client} is {c_ed}, server is {s_ed}; reference-length limits per edition are not "
        "configured yet (SPEC OI-10: take them from the standard)"))


def classify_failed_association(host: str, port: int, tcp: ProbeResult, timeout_s: float, local_ip: str | None) -> Any:
    """Why an association with a device whose TCP port answers failed (`discover`): one more attempt,
    layer by layer, also ended cleanly; the TLS port is tried when the pattern suggests security."""
    from .probes import TLS_CHECK_OUTCOMES, iso_associate, tls_port_open

    assoc = iso_associate(host, port, max(timeout_s, 1.0), local_ip, linger_s=0.2)
    tls = tls_port_open(host, timeout_s, local_ip=local_ip) if assoc.outcome in TLS_CHECK_OUTCOMES else None
    return classify_association(tcp, None, assoc, tls)
