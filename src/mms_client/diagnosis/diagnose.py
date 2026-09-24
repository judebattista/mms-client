"""Layered diagnosis (DIA-1, DIA-2, DIA-4, DIA-6, IDN-8).

Runs the layers in order and stops at the first one that fails::

    bind address (with --as) → network (ICMP, TCP 102) → transport/session (COTP, ISO session,
    presentation, ACSE) → MMS initiate (PDU size, services) → association through libiec61850 →
    identity → model (vs the reference) → reports (the client's RCBs) → controls (authority, if
    requested)

and ends with a verdict (the most likely cause, with its certainty) and next steps.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from mms_client import codes
from mms_client.adapter import ConnectError, ServiceError
from mms_client.core.identity import collect_identity
from mms_client.core.results import Category, CheckResult, Status
from mms_client.core.safety import ConfirmationDeclined, PolicyError
from mms_client.core.session import Session

from .classify import (
    AssociationFailure,
    Finding,
    assess_association_limits,
    classify_association,
    explain_ied_connect_error,
)
from .net import check_bind_address
from .probes import ProbeResult, run_layered_probes

COMM = Category.COMMUNICATION
CONF = Category.CONFIGURATION

LAYERS = ("bind", "network", "transport-session", "mms-initiate", "association", "identity", "model", "reports", "controls")

NEXT_STEPS: dict[str, list[str]] = {
    "bind": [
        "Add the source address shown above to this machine's interface, then run diagnose again.",
    ],
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
    "identity": ["Check the identity sources below; disagreements often mean a firmware update changed a field."],
    "model": [
        "Run `check` for the full list of differences, and confirm the SCD/CID matches the loaded configuration.",
        "If the device was re-configured, export its CID again or take a new snapshot.",
    ],
    "reports": [
        "See which client holds the RCB (rcb), and whether that client should have it.",
        "Stale reservations from a client that disconnected abruptly clear after the device's timeout (ResvTms).",
    ],
    "controls": [
        "Read Loc / LocSta / LocKey and Mod/Beh (they are shown in the matrix) and compare with the orCat the "
        "real client sends.",
    ],
}


@dataclass(slots=True)
class LayerOutcome:
    layer: str
    status: Status
    results: list[CheckResult] = field(default_factory=list)
    summary: str = ""

    def to_json(self) -> dict:
        return {
            "layer": self.layer,
            "status": self.status.value,
            "summary": self.summary,
            "results": [r.to_json() for r in self.results],
        }


@dataclass(slots=True)
class Verdict:
    text: str
    certainty: str  # fact | likely | check
    key: str | None = None
    layer: str | None = None

    def to_json(self) -> dict:
        return {"text": self.text, "certainty": self.certainty, "key": self.key, "layer": self.layer}


@dataclass
class DiagnosisReport:
    device: str
    host: str
    port: int
    local_ip: str | None
    as_client: str | None
    layers: list[LayerOutcome] = field(default_factory=list)
    stopped_at: str | None = None
    verdict: Verdict | None = None
    next_steps: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    probes: dict[str, Any] | None = None
    association_failure: dict[str, Any] | None = None
    started_at: float = field(default_factory=time.time)
    duration_s: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def results(self) -> list[CheckResult]:
        return [r for lay in self.layers for r in lay.results]

    def to_json(self) -> dict:
        return {
            "device": self.device,
            "host": self.host,
            "port": self.port,
            "local_ip": self.local_ip,
            "as_client": self.as_client,
            "layers": [lay.to_json() for lay in self.layers],
            "stopped_at": self.stopped_at,
            "verdict": self.verdict.to_json() if self.verdict else None,
            "next_steps": self.next_steps,
            "findings": [f.to_json() for f in self.findings],
            "association_failure": self.association_failure,
            "probes": self.probes,
            "duration_s": round(self.duration_s, 3),
            "notes": self.notes,
        }


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


def _quirks_for(session: Session):
    try:
        from mms_client.quirks import load_quirks
    except ImportError:  # pragma: no cover
        return None
    ident = session.identity
    ov = getattr(session.target.device, "identity", None)
    vendor = (ident.vendor.value if ident else None) or getattr(ov, "vendor", None)
    model = (ident.model.value if ident else None) or getattr(ov, "model", None)
    fw = (ident.firmware.value if ident else None) or getattr(ov, "firmware", None)
    if not vendor and not model:
        return None
    try:
        # resolve(): a firmware-specific entry must not hide model-wide limits
        return load_quirks().resolve(vendor, model, fw)
    except Exception:
        return None


def diagnose(
    session: Session,
    *,
    controls: bool = False,
    control_ref: str | None = None,
    timeout_s: float = 3.0,
    icmp: bool = True,
) -> DiagnosisReport:
    """Run the layers in order (DIA-1). The session must not be connected yet (the probes use their
    own short-lived associations); it is connected by the association layer and left connected."""
    t0 = time.time()
    t = session.target
    rep = DiagnosisReport(
        session.device_name, t.host, t.port, t.local_ip, t.as_client.client if t.as_client else None
    )
    try:
        _run(session, rep, controls=controls, control_ref=control_ref, timeout_s=timeout_s, icmp=icmp)
    finally:
        rep.duration_s = time.time() - t0
        if rep.verdict is None:
            rep.verdict = Verdict("diagnosis did not complete", "check")
        rep.notes.append(
            "GOOSE and Sampled Values are not MMS: paths that use them (e.g. bay-level interlocking between "
            "devices) cannot be observed by this tool."
        )
        session.last_results["diagnose"] = rep
        session.log.write("diagnose", report=rep.to_json())
    return rep


def _stop(rep: DiagnosisReport, layer: str, verdict: Verdict, extra_steps: list[str] | None = None) -> None:
    rep.stopped_at = layer
    rep.verdict = verdict
    rep.next_steps = list(extra_steps or []) + NEXT_STEPS.get(layer, [])


def _run(session: Session, rep: DiagnosisReport, *, controls: bool, control_ref: str | None, timeout_s: float, icmp: bool) -> None:
    t = session.target
    # -- 0. bind address (NBR-2)
    if t.local_ip:
        bc = check_bind_address(t.local_ip, t.host)
        lay = LayerOutcome("bind", Status.PASS if bc.ok else Status.FAIL)
        lay.results.append(
            CheckResult("bind-address", "Source address present on this machine", lay.status, COMM, subject=t.local_ip,
                         message=bc.message, error=None if bc.ok else codes.tool("local-address-missing"), certainty="fact")
        )
        rep.layers.append(lay)
        if not bc.ok:
            _stop(rep, "bind", Verdict(bc.message, "fact", "tool:local-address-missing", "bind"))
            return
    # -- 1-3. raw probes
    probes = run_layered_probes(t.host, t.port, timeout=timeout_s, local_ip=t.local_ip, icmp=icmp)
    rep.probes = probes.to_json()
    failure: AssociationFailure = classify_association(probes.tcp, probes.cotp, probes.association, probes.tls, icmp=probes.icmp)
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
    quirk = _quirks_for(session)
    rep.findings.extend(failure.findings)
    if not failure.ok:
        layer = {"network": "network", "transport": "transport-session", "session": "transport-session",
                 "presentation": "transport-session", "acse": "transport-session", "mms": "mms-initiate"}.get(failure.layer or "", "network")
        for lay in (net, ts, mms):
            if lay.layer == layer:
                lay.status = Status.FAIL
        if layer != "network":
            rep.layers.append(ts)
        if layer == "mms-initiate":
            rep.layers.append(mms)
        rep.findings.extend(assess_association_limits(failure, quirks=quirk, tool_holds_association=False))
        best = _best_finding(rep.findings)
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
        _stop(rep, layer, Verdict(text, cert, failure.error.key, layer), extra)
        return
    rep.layers.extend([ts, mms])
    # -- 4. association through libiec61850
    assoc = LayerOutcome("association", Status.PASS)
    rep.layers.append(assoc)
    t_assoc = time.monotonic()
    try:
        session.connect()
        params = session.require_client().connection_params()
        assoc.results.append(
            CheckResult("association", "Association through libiec61850", Status.PASS, COMM,
                        message=f"associated; max PDU {params.max_pdu_size}, services: {', '.join(params.supported_services())}",
                        evidence=params.to_json())
        )
        rep.findings.extend(
            f for f in assess_association_limits(failure, quirks=quirk, tool_holds_association=True) if f.key != "association:accepted"
        )
    except (ConnectError, PolicyError) as e:
        err = getattr(e, "error", None)
        assoc.status = Status.FAIL
        assoc.results.append(CheckResult("association", "Association through libiec61850", Status.FAIL, COMM, error=err, message=str(e)))
        note = (
            explain_ied_connect_error(err, elapsed_s=time.monotonic() - t_assoc, timeout_s=session.connect_timeout_ms / 1000)
            if err is not None and err.domain is codes.Domain.IED
            else str(e)
        )
        _stop(rep, "association", Verdict(
            f"The raw association probe was accepted, but libiec61850's association failed ({err}). {note}", "likely",
            err.key if err else None, "association"))
        return
    # -- 5. identity
    ident_l = LayerOutcome("identity", Status.PASS)
    rep.layers.append(ident_l)
    try:
        model = session.model()
        ident = collect_identity(
            session.require_client(), model, override=getattr(t.device, "identity", None),
            scl_edition=getattr(session.reference, "scl_edition", lambda: None)(), log=session.log,
        )
        session.identity = ident
    except ServiceError as e:
        ident_l.status = Status.FAIL
        ident_l.results.append(CheckResult("identity", "Device identity", Status.FAIL, COMM, error=e.error, message=str(e)))
        _stop(rep, "identity", Verdict(f"The device associated but could not be browsed ({e.error}).", "fact", e.error.key, "identity"))
        return
    ident_l.summary = f"{ident.vendor} / {ident.model} / firmware {ident.firmware} / edition {ident.edition}"
    ident_l.results.append(CheckResult("identity", "Device identity", Status.INFO, COMM, message=ident_l.summary, evidence=ident.to_json()))
    for d in ident.disagreements:
        ident_l.results.append(CheckResult("identity-disagreement", "Identity sources agree", Status.WARN, CONF, message=d,
                                           error=codes.check("identity-disagreement")))
    quirk = _quirks_for(session) or quirk
    if quirk is not None:
        rep.findings.extend(f for f in assess_association_limits(failure, quirks=quirk, tool_holds_association=True)
                            if f.key not in {x.key for x in rep.findings})
    _edition_compat(session, rep, ident_l)
    # -- 6. model (vs reference)
    model_l = LayerOutcome("model", Status.PASS)
    rep.layers.append(model_l)
    model_l.summary = f"{len(model.lds)} logical device(s), {sum(len(x.lns) for x in model.lds.values())} logical nodes, {model.count_attributes()} attributes"
    if model.errors:
        model_l.status = Status.WARN
        model_l.results.append(CheckResult("model-browse", "Model can be browsed", Status.WARN, CONF,
                                           message=f"{len(model.errors)} part(s) of the model could not be read", evidence={"errors": model.errors}))
    ref = session.reference
    if ref is not None and getattr(ref, "expected", None) is not None:
        from mms_client.verify.reference import _scl_model_checks

        res = list(_scl_model_checks(session, ref.expected, None))
        model_l.results.extend(res)
        bad = [r for r in res if r.status is Status.FAIL]
        if bad:
            model_l.status = Status.FAIL
            _stop(rep, "model", Verdict(
                f"The device's model differs from the reference ({ref.label}): {len(bad)} difference(s), e.g. "
                f"{bad[0].subject}: {bad[0].message}. A client configured from that reference will fail on these objects.",
                "fact", bad[0].error.key if bad[0].error else None, "model"))
            return
    elif ref is None:
        model_l.results.append(CheckResult("model-reference", "Model compared with a reference", Status.NOT_RUN, CONF,
                                           reason="no reference (SCD/CID/snapshot) for this device"))
    # -- 7. reports (the client's RCBs)
    rpt = LayerOutcome("reports", Status.PASS)
    rep.layers.append(rpt)
    rpt.results.extend(_client_rcb_results(session))
    bad = [r for r in rpt.results if r.status is Status.FAIL]
    if bad:
        rpt.status = Status.FAIL
        _stop(rep, "reports", Verdict(
            f"{bad[0].subject}: {bad[0].message}. The client that needs this RCB cannot get its reports.",
            "fact", bad[0].error.key if bad[0].error else None, "reports"))
        return
    # -- 8. controls (authority), only when asked
    if controls:
        from mms_client.core.controls import authority_probe

        ctl = LayerOutcome("controls", Status.PASS)
        rep.layers.append(ctl)
        try:
            rows = authority_probe(session, control_ref)
        except (ConfirmationDeclined, PolicyError) as e:
            ctl.status = Status.NOT_RUN
            ctl.results.append(CheckResult("authority", "Control authority", Status.NOT_RUN, COMM, reason=str(e)))
            rows = []
        for row in rows:
            probed = [c for c in row.cells if c.accepted is not None]
            if not probed:
                ctl.results.append(CheckResult("authority", "Control authority", Status.INFO, COMM, subject=row.ref.iec(), message=row.note))
                continue
            accepted = [c.or_cat for c in probed if c.accepted]
            status = Status.PASS if accepted else Status.WARN
            ctl.results.append(CheckResult(
                "authority", "Control authority", status, COMM, subject=row.ref.iec(),
                message=("accepted for orCat " + ", ".join(str(x) for x in accepted)) if accepted else "refused for every orCat tried",
                evidence=row.to_json()))
        if any(r.status is Status.WARN for r in ctl.results):
            ctl.status = Status.WARN
    rep.verdict = Verdict(
        "No MMS problem found at any tested layer" + (f" while acting as {rep.as_client}" if rep.as_client else "")
        + ". If a function still fails, check parts this tool cannot see (GOOSE / Sampled Values, the client's own configuration).",
        "fact", None, None)
    rep.next_steps = [
        "Run `check` with the SCD for a full configuration comparison.",
        "If the problem is intermittent, `subscribe` to the client's RCB and watch for SqNum gaps.",
    ]


REMINDER_KEYS = frozenset({"tool:association-slot-occupied-by-tool", "tool:known-association-limit"})
VERDICT_PRIORITY = (
    "association:tls-suspected",
    "association:authentication-suspected",
    "tool:security-not-supported",
    "tool:slots-probably-exhausted",
)


def _best_finding(findings: list[Finding]) -> Finding | None:
    """The finding that best states the most likely cause (reminders are never the verdict)."""
    cands = [f for f in findings if f.key not in REMINDER_KEYS]
    if not cands:
        return None
    order = {"fact": 0, "likely": 1, "check": 2}

    def rank(f: Finding) -> tuple[int, int]:
        pri = VERDICT_PRIORITY.index(f.key) if f.key in VERDICT_PRIORITY else len(VERDICT_PRIORITY)
        return (pri, order.get(str(f.certainty), 3))

    return min(cands, key=rank)


def _client_rcb_results(session: Session) -> list[CheckResult]:
    """The RCBs that clients of this device need (SCD ClientLN or inventory), and whether they can get them."""
    ref = session.reference
    if ref is not None and getattr(ref, "expected", None) is not None:
        from mms_client.core.reports import list_rcbs
        from mms_client.verify.reference import _communication_checks

        return [r for r in _communication_checks(session, ref, list_rcbs(session), None) if r.id.startswith("client-")]
    out: list[CheckResult] = []
    inv = session.inventory
    rels = inv.clients_of(session.target.name or "") if inv is not None else []
    if session.target.as_client is not None and session.target.as_client not in rels:
        rels.append(session.target.as_client)
    if not rels:
        return [CheckResult("client-rcbs", "The clients' RCBs are available", Status.NOT_RUN, COMM,
                            reason="no reference or inventory says which RCBs the clients use")]
    from mms_client.core.reports import free_reason, list_rcbs

    states = list_rcbs(session)
    for rel in rels:
        for name in rel.rcbs:
            t = name.replace("$", ".")
            st = next((s for s in states if s.cb.reference == t or s.cb.mms_reference == name), None)
            subject = f"{name} for {rel.client}"
            if st is None or st.values is None:
                out.append(CheckResult("client-rcb-missing", "The client's RCBs exist", Status.FAIL, COMM, subject=subject,
                                       message="RCB not found on the device", error=codes.check("client-rcb-missing")))
                continue
            if st.owner_ip and rel.source_ip and st.owner_ip == rel.source_ip:
                out.append(CheckResult("client-rcb-not-available", "The client's RCBs are available to it", Status.PASS, COMM,
                                       subject=subject, message=f"{st.state} by the client itself"))
                continue
            reason = free_reason(st, me=rel.client)
            out.append(CheckResult("client-rcb-not-available", "The client's RCBs are available to it",
                                   Status.PASS if reason is None else Status.FAIL, COMM, subject=subject,
                                   message="free" if reason is None else reason,
                                   error=None if reason is None else codes.check("client-rcb-not-available")))
    return out


def _edition_compat(session: Session, rep: DiagnosisReport, lay: LayerOutcome) -> None:
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
