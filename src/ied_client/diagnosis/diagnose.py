"""Layered diagnosis (DIA-1, DIA-2, DIA-4, DIA-6).

Runs the layers in order and stops at the first one that fails::

    bind address (with --as) → the protocol module's layers, from the network up to an open
    association (PROTO-9) → identity → model (vs the reference) → reports (the client's RCBs) →
    controls (authority, if requested)

and ends with a verdict (the most likely cause, with its certainty) and next steps.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Literal

from ied_client import codes
from ied_client.core.identity import identify
from ied_client.core.results import Category, CheckResult, Status
from ied_client.core.safety import ConfirmationDeclined, PolicyError
from ied_client.core.session import Session
from ied_client.protocol.errors import ServiceError

from .net import check_bind_address

COMM = Category.COMMUNICATION
CONF = Category.CONFIGURATION

# Layers of every diagnosis; the protocol module's layers come between "bind" and "identity".
GENERIC_LAYERS = ("bind", "identity", "model", "reports", "controls")

NEXT_STEPS: dict[str, list[str]] = {
    "bind": [
        "Add the source address shown above to this machine's interface, then run diagnose again.",
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

Certainty = Literal["fact", "likely", "check"]


@dataclass
class Finding:
    """One conclusion for the operator. ``key`` is a code key (e.g. ``tool:slots-probably-exhausted``)
    for the explanation catalogue; ``certainty`` follows EXP-4."""

    key: str
    certainty: Certainty
    text: str
    evidence: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "certainty": self.certainty,
            "text": self.text,
            "evidence": list(self.evidence),
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
    # next steps per layer: the generic ones plus the protocol module's (not part of the JSON)
    next_steps_by_layer: dict[str, list[str]] = field(default_factory=lambda: dict(NEXT_STEPS), repr=False)

    @property
    def results(self) -> list[CheckResult]:
        return [r for lay in self.layers for r in lay.results]

    def stop(self, layer: str, verdict: Verdict, extra_steps: list[str] | None = None) -> None:
        """End the diagnosis at ``layer`` with ``verdict``; next steps are ``extra_steps`` then the layer's own."""
        self.stopped_at = layer
        self.verdict = verdict
        self.next_steps = list(extra_steps or []) + self.next_steps_by_layer.get(layer, [])

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
    rep.next_steps_by_layer.update(session.protocol.next_steps)
    try:
        _run(session, rep, controls=controls, control_ref=control_ref, timeout_s=timeout_s, icmp=icmp)
    finally:
        rep.duration_s = time.time() - t0
        if rep.verdict is None:
            rep.verdict = Verdict("diagnosis did not complete", "check")
        if session.protocol.blind_spots:
            rep.notes.append(session.protocol.blind_spots)
        session.last_results["diagnose"] = rep
        session.log.write("diagnose", report=rep.to_json())
    return rep


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
            rep.stop("bind", Verdict(bc.message, "fact", "tool:local-address-missing", "bind"))
            return
    # -- the protocol's layers, up to an open association (PROTO-9)
    conn = session.protocol.connection_diagnosis()
    if not conn.run(session, rep, timeout_s=timeout_s, icmp=icmp):
        return
    # -- 5. identity
    ident_l = LayerOutcome("identity", Status.PASS)
    rep.layers.append(ident_l)
    try:
        model = session.model()
        ident = identify(session)
    except ServiceError as e:
        ident_l.status = Status.FAIL
        ident_l.results.append(CheckResult("identity", "Device identity", Status.FAIL, COMM, error=e.error, message=str(e)))
        rep.stop("identity", Verdict(f"The device associated but could not be browsed ({e.error}).", "fact", e.error.key, "identity"))
        return
    ident_l.summary = f"{ident.vendor} / {ident.model} / firmware {ident.firmware} / edition {ident.edition}"
    ident_l.results.append(CheckResult("identity", "Device identity", Status.INFO, COMM, message=ident_l.summary, evidence=ident.to_json()))
    for d in ident.disagreements:
        ident_l.results.append(CheckResult("identity-disagreement", "Identity sources agree", Status.WARN, CONF, message=d,
                                           error=codes.check("identity-disagreement")))
    conn.after_identity(session, rep, ident_l)
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
        from ied_client.verify.reference import _scl_model_checks

        res = list(_scl_model_checks(session, ref.expected, None))
        model_l.results.extend(res)
        bad = [r for r in res if r.status is Status.FAIL]
        if bad:
            model_l.status = Status.FAIL
            rep.stop("model", Verdict(
                f"The device's model differs from the reference ({ref.label}): {len(bad)} difference(s), e.g. "
                f"{bad[0].subject}: {bad[0].message}. A client configured from that reference will fail on these objects.",
                "fact", bad[0].error.key if bad[0].error else None, "model"))
            return
    elif ref is not None and getattr(ref, "snapshot", None) is not None:
        from ied_client.verify.diff import model_differences

        diffs = model_differences(ref.snapshot, model)
        if diffs:
            model_l.status = Status.FAIL
            first = diffs[0]
            key = {"removed": "model-object-missing", "added": "model-object-unexpected"}.get(first.change, "model-type-mismatch")
            model_l.results.append(CheckResult(
                "snapshot-structure", "Model matches the snapshot", Status.FAIL, CONF, subject=first.key,
                message=f"{len(diffs)} difference(s) in the model tree, e.g. {first.key}: {first.change}",
                evidence={"differences": [d.to_json() for d in diffs[:100]], "count": len(diffs)},
                error=codes.check(key), certainty="fact"))
            rep.stop("model", Verdict(
                f"The device's model differs from the snapshot ({ref.label}): {len(diffs)} difference(s), e.g. "
                f"{first.key} was {first.change}. A client configured for the snapshotted model will fail on these objects.",
                "fact", f"check:{key}", "model"))
            return
        model_l.results.append(CheckResult("snapshot-structure", "Model matches the snapshot", Status.PASS, CONF,
                                           message=f"model tree matches {ref.label}"))
    elif ref is None:
        model_l.results.append(CheckResult("model-reference", "Model compared with a reference", Status.NOT_RUN, CONF,
                                           reason="no reference (SCD/CID/snapshot) for this device"))
    else:
        model_l.results.append(CheckResult("model-reference", "Model compared with a reference", Status.NOT_RUN, CONF,
                                           reason=f"the reference ({getattr(ref, 'label', ref)}) has no model to compare"))
    # -- 7. reports (the client's RCBs)
    rpt = LayerOutcome("reports", Status.PASS)
    rep.layers.append(rpt)
    rpt.results.extend(_client_rcb_results(session))
    bad = [r for r in rpt.results if r.status is Status.FAIL]
    if bad:
        rpt.status = Status.FAIL
        rep.stop("reports", Verdict(
            f"{bad[0].subject}: {bad[0].message}. The client that needs this RCB cannot get its reports.",
            "fact", bad[0].error.key if bad[0].error else None, "reports"))
        return
    # -- 8. controls (authority), only when asked
    if controls:
        from ied_client.core.controls import authority_probe

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
        f"No {session.protocol.display_name} problem found at any tested layer" + (f" while acting as {rep.as_client}" if rep.as_client else "")
        + ". If a function still fails, check parts this tool cannot see (GOOSE / Sampled Values, the client's own configuration).",
        "fact", None, None)
    rep.next_steps = [
        "Run `check` with the SCD for a full configuration comparison.",
        "If the problem is intermittent, `subscribe` to the client's RCB and watch for SqNum gaps.",
    ]


def _client_rcb_results(session: Session) -> list[CheckResult]:
    """The RCBs that clients of this device need (SCD ClientLN or inventory), and whether they can get them."""
    ref = session.reference
    if ref is not None and getattr(ref, "expected", None) is not None:
        from ied_client.core.reports import list_rcbs
        from ied_client.verify.reference import _communication_checks

        return [r for r in _communication_checks(session, ref, list_rcbs(session), None) if r.id.startswith("client-")]
    out: list[CheckResult] = []
    inv = session.inventory
    rels = inv.clients_of(session.target.name or "") if inv is not None else []
    if session.target.as_client is not None and session.target.as_client not in rels:
        rels.append(session.target.as_client)
    if not rels:
        return [CheckResult("client-rcbs", "The clients' RCBs are available", Status.NOT_RUN, COMM,
                            reason="no reference or inventory says which RCBs the clients use")]
    from ied_client.core.reports import free_reason, list_rcbs, same_rcb

    states = list_rcbs(session)
    for rel in rels:
        for name in rel.rcbs:
            st = next((s for s in states if same_rcb(name, s.cb, session.protocol.names)), None)
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
