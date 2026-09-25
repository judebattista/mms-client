"""Classification of association failures (DIA-3, DIA-4, DIA-6).

Spike RSK-3 evidence (libiec61850 1.6.1 via the simulated IED in ``tests/sim``, loopback)
------------------------------------------------------------------------------------------
What libiec61850's *client* reports: ``IedConnection_connect`` returned ``ied:connection-rejected (5)``
for every failure mode tried: TCP refused, a listener that never answers, TCP closed immediately,
COTP DR, COTP CC followed by silence, CC followed by a close, session REFUSE, and exhausted
association slots. It never returned ``ied:timeout (20)``: a timeout is visible only as the elapsed
time (the full connect timeout) before the same code. So the library's error alone cannot separate
the layers; the probes in :mod:`.probes` do (DIA-3).

What libiec61850's *server* does when its association slots are exhausted
(``IedServerConfig_setMaxMmsConnections``): the kernel completes the TCP handshake, then the server
accepts the socket and closes it at once (FIN, ~0–20 ms after connect) without sending any ISO
PDU, even when a COTP CR has already arrived. No COTP DR, session REFUSE or ACSE rejection is sent.
The probes therefore report ``tcp-closed-immediately``, and :func:`assess_association_limits`
gives the "slots probably exhausted" hint for that pattern (as ``likely``, never as fact: an IP
filter that closes unknown clients looks the same). Further observations:

* A slot is taken by any open TCP connection, even one that never sends a COTP CR.
* A client that hangs with its TCP connection open (process alive, socket open, silent) keeps its
  slot for as long as the connection stays open (> 5 s observed; there is no idle timeout).
* When a client process is killed, the kernel closes its socket and the server frees the slot within
  ~10–60 ms; a probe made in that window is still refused. A peer that vanishes without closing
  (power loss, cable pulled) holds its slot until TCP keepalive or a retransmission timeout drops the
  connection; that case was not reproducible on loopback and is not verified here.
* Other servers refuse differently (ACSE rejected-transient, COTP DR "congestion", accept then close
  after the AARE, or no answer); the classification covers those patterns too, exercised with small
  fake servers in the unit tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from ied_client import codes
from ied_client.codes import ErrorInfo
from ied_client.diagnosis.diagnose import Certainty, Finding
from ied_client.diagnosis.probe import ProbeResult
from ied_client.quirks import QuirkInfo

from ..codes import MmsDomain
from . import iso
from .probes import TLS_CHECK_OUTCOMES, AssociationProbe

# The layer each outcome belongs to.
OUTCOME_LAYERS: dict[str, str | None] = {
    "accepted": None,
    "host-unreachable": "network",
    "tcp-refused": "network",
    "tcp-timeout": "network",
    "tcp-closed-immediately": "transport",
    "cotp-rejected": "transport",
    "cotp-no-response": "transport",
    "cotp-invalid-response": "transport",
    "session-refused": "session",
    "session-aborted": "session",
    "presentation-rejected": "session",
    "acse-rejected-permanent": "mms",
    "acse-rejected-transient": "mms",
    "acse-aborted": "mms",
    "initiate-error": "mms",
    "initiate-no-response": "session",
    "invalid-response": None,
    "accepted-then-closed": "mms",
    "tls-suspected": "network",
    "authentication-suspected": "mms",
    "timeout": None,
    "unknown": None,
}

# ACSE diagnostics (service-user) meaning "authentication is expected".
AUTH_DIAGNOSTICS_REQUIRED = frozenset({12, 14})  # mechanism-name-required, authentication-required
AUTH_DIAGNOSTICS_OTHER = frozenset({11, 13})  # mechanism-name-not-recognized, authentication-failure
AUTH_ABORT_DIAGNOSTICS = frozenset({3, 4, 5, 6})
AP_TITLE_DIAGNOSTICS = frozenset({3, 4, 5, 6, 7, 8, 9, 10})

# Outcomes that fit "all association slots in use" (DIA-4 b).
SLOT_PATTERNS = frozenset(
    {
        "tcp-closed-immediately",
        "acse-rejected-transient",
        "accepted-then-closed",
        "cotp-no-response",
        "initiate-no-response",
    }
)

SLOTS_HINT = "association slots probably exhausted — possibly stale associations from a client that disconnected abruptly"


@dataclass
class AssociationFailure:
    """The single most specific classification of an association attempt.

    ``outcome`` is a key of :data:`codes.ASSOCIATION_OUTCOMES` (``accepted`` when nothing failed);
    ``observed`` is the raw pattern the probes saw before any interpretation (e.g. ``outcome`` may be
    ``tls-suspected`` while ``observed`` is ``tcp-refused``). ``layer`` is where it failed.
    """

    outcome: str
    layer: str | None
    observed: str
    detail: str
    evidence: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    acse_diagnostic: ErrorInfo | None = None
    security: Literal["tls", "authentication"] | None = None
    reachable: bool | None = None  # the device answered at some layer (ICMP or TCP)
    congestion: bool = False  # the refusal carried an explicit "busy / congestion" reason
    closed_after_s: float | None = None

    @property
    def ok(self) -> bool:
        return self.outcome == "accepted"

    @property
    def error(self) -> ErrorInfo:
        return codes.association(self.outcome)

    def to_json(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "outcome": self.outcome,
            "error": self.error.to_json(),
            "layer": self.layer,
            "observed": self.observed,
            "detail": self.detail,
            "evidence": list(self.evidence),
            "findings": [f.to_json() for f in self.findings],
            "acse_diagnostic": self.acse_diagnostic.to_json() if self.acse_diagnostic else None,
            "security": self.security,
            "reachable": self.reachable,
            "congestion": self.congestion,
            "closed_after_s": self.closed_after_s,
        }


def _pdu(step: ProbeResult | None) -> dict[str, Any]:
    if step is None:
        return {}
    p = step.raw.get("pdu") or step.raw.get("spdu") or {}
    return p if isinstance(p, dict) else {}


def _congestion(assoc: AssociationProbe | None, cotp: ProbeResult | None) -> bool:
    """Did a rejection carry an explicit temporary-congestion reason at any layer?"""
    steps = list(assoc.steps) if assoc else []
    if cotp is not None and cotp not in steps:
        steps.append(cotp)
    for s in steps:
        if s.ok is not False:
            continue
        p = _pdu(s)
        if (
            s.name in ("cotp", "cotp-disconnect")
            and p.get("type") == "DR"
            and p.get("reason") in iso.COTP_CONGESTION_REASONS
        ):
            return True
        if s.name == "iso-session" and p.get("refuse_reason") in iso.SESSION_CONGESTION_REASONS:
            return True
        if s.name == "presentation" and p.get("provider_reason") in iso.PRESENTATION_CONGESTION_REASONS:
            return True
        if s.name == "mms-initiate" and p.get("error_class") == 3:  # resource
            return True
    return False


def _layer_findings(outcome: str, assoc: AssociationProbe | None, cotp: ProbeResult | None) -> list[Finding]:
    """Addressing mismatches that the reason codes point to (selectors, AP titles)."""
    out: list[Finding] = []
    steps = {s.name: s for s in (assoc.steps if assoc else [])}
    if cotp is not None:
        steps.setdefault("cotp", cotp)
    c = _pdu(steps.get("cotp"))
    if c.get("type") == "DR" and c.get("reason") in (0x02, 0x03):
        out.append(
            Finding(
                codes.association("cotp-rejected").key,
                "check",
                "the device did not accept the transport selector (TSEL): compare OSI-TSEL in the SCD "
                "(ConnectedAP address) with what the tool sends (0001 by default)",
                [steps["cotp"].detail],
            )
        )
    s = _pdu(steps.get("iso-session"))
    if s.get("refuse_reason") in (0x81, 0x82):
        out.append(
            Finding(
                codes.association("session-refused").key,
                "check",
                "the device did not accept the session selector (SSEL): compare OSI-SSEL in the SCD with what the "
                "tool sends (0001 by default)",
                [steps["iso-session"].detail],
            )
        )
    p = _pdu(steps.get("presentation"))
    if p.get("provider_reason") in (3, 7):
        out.append(
            Finding(
                codes.association("presentation-rejected").key,
                "check",
                "the device did not accept the presentation selector (PSEL): compare OSI-PSEL in the SCD with what the "
                "tool sends (00000001 by default)",
                [steps["presentation"].detail],
            )
        )
    a = _pdu(steps.get("acse"))
    diag = a.get("diagnostic") if a.get("diagnostic_source") == "service-user" else None
    if diag in AP_TITLE_DIAGNOSTICS:
        out.append(
            Finding(
                codes.association(outcome).key
                if outcome in codes.ASSOCIATION_OUTCOMES
                else "association:unknown",
                "check",
                "the device rejected the AP title / AE qualifier: some IEDs accept only the calling AP title configured "
                "for their clients, or expect their own AP title from the SCD (OSI-AP-Title, OSI-AE-Qualifier); "
                "the tool sends libiec61850's defaults (calling 1.1.1.999 / 12, called 1.1.1.999.1 / 12)",
                [steps["acse"].detail],
            )
        )
    elif outcome == "acse-rejected-permanent" and diag in (None, 0, 1):
        out.append(
            Finding(
                codes.association("acse-rejected-permanent").key,
                "check",
                "the device rejected the association permanently without a specific reason: check whether it accepts "
                "only listed client IP addresses (use --as / a local address, NBR-2) or requires ACSE authentication "
                "(not supported in v1)",
                [steps["acse"].detail] if "acse" in steps else [],
            )
        )
    return out


def classify_association(
    tcp: ProbeResult,
    cotp: ProbeResult | None = None,
    assoc: AssociationProbe | None = None,
    tls: ProbeResult | None = None,
    *,
    icmp: ProbeResult | None = None,
) -> AssociationFailure:
    """The single most specific outcome, the layer it failed at, and the evidence.

    ``cotp`` may be omitted when ``assoc`` is given (the association probe includes COTP). DIA-6:
    TCP 102 refused / closed / timing out while TCP 3782 is open -> ``tls-suspected``; an ACSE
    rejection or abort with an authentication diagnostic -> ``authentication-suspected``. Both
    add a finding saying that v1 has no security support.
    """
    evidence: list[str] = []
    if icmp is not None:
        evidence.append(f"{icmp.name}: {icmp.detail}")
    evidence.append(f"{tcp.name}: {tcp.detail}")
    if cotp is None and assoc is not None:
        cotp = assoc.cotp
    reachable: bool | None = True if tcp.ok else (True if icmp is not None and icmp.ok else None)
    closed_after_s: float | None = None
    acse_diag: ErrorInfo | None = None
    detail_step: ProbeResult | None = None

    if tcp.ok is not True:
        observed, layer = tcp.outcome, "network"
        detail_step = tcp
        if tcp.ok is None and tcp.outcome not in codes.ASSOCIATION_OUTCOMES:
            observed = "unknown"
    elif assoc is not None:
        observed, layer = assoc.outcome, assoc.failed_layer
        for s in assoc.steps:
            if s.name.startswith("tcp-"):
                continue
            evidence.append(f"{s.name}: {s.detail}")
        detail_step = next((s for s in reversed(assoc.steps) if s.ok is False and s.name != "release"), None)
        acse_diag = assoc.acse_diagnostic if assoc.acse_diagnostic and assoc.acse_diagnostic.code else None
        closed_after_s = assoc.closed_after_s
        if closed_after_s is None and assoc.cotp is not None:
            closed_after_s = assoc.cotp.raw.get("closed_after_s")
    elif cotp is not None:
        evidence.append(f"{cotp.name}: {cotp.detail}")
        if cotp.ok is False:
            observed, layer = cotp.outcome, OUTCOME_LAYERS.get(cotp.outcome) or "transport"
            detail_step = cotp
            closed_after_s = cotp.raw.get("closed_after_s")
        else:
            observed, layer = "unknown", None
            evidence.append("association not probed: only TCP and COTP were tested")
    else:
        observed, layer = "unknown", None
        evidence.append("association not probed: only TCP was tested")
    if observed not in codes.ASSOCIATION_OUTCOMES:
        observed = "unknown"

    outcome = observed
    security: Literal["tls", "authentication"] | None = None
    findings: list[Finding] = []

    # --- DIA-6: TLS ---------------------------------------------------------------------
    tls_open = tls is not None and tls.ok is True
    if tls is not None:
        evidence.append(f"{tls.name}: {tls.detail}")
    tls_on_102 = observed == "cotp-invalid-response" and bool(cotp and cotp.raw.get("looks_like_tls"))
    if (tls_open and observed in TLS_CHECK_OUTCOMES) or tls_on_102:
        outcome, security = "tls-suspected", "tls"
        why = (
            "the MMS port answered the COTP connection request with a TLS record"
            if tls_on_102
            else f"TCP {tcp.raw.get('port', 102)} {codes.ASSOCIATION_OUTCOMES[observed].lower()}, "
            f"but TCP {tls.raw.get('port', 3782) if tls else 3782} (IEC 62351 TLS) is open"
        )
        findings.append(
            Finding(
                codes.tool("security-not-supported").key,
                "likely",
                f"the IED appears to require TLS (IEC 62351): {why}. This version of mms-client has no TLS support, "
                "so it cannot associate with this IED; enable an unsecured MMS port on the IED for testing, or use a "
                "client with TLS",
                [e for e in evidence if e.startswith(("tcp-", "cotp"))],
            )
        )

    # --- DIA-6: ACSE authentication ------------------------------------------------------
    acse = assoc.acse if assoc else None
    if acse is not None and security is None:
        diag = acse.diagnostic if acse.diagnostic_source == "service-user" else None
        auth_text = None
        certainty: Certainty = "likely"
        if (
            acse.kind == "AARE"
            and acse.result in (1, 2)
            and diag in AUTH_DIAGNOSTICS_REQUIRED | AUTH_DIAGNOSTICS_OTHER
        ):
            auth_text = (
                f"the IED rejected the association with ACSE diagnostic {acse.diagnostic_name} ({diag})"
            )
            certainty = "fact" if diag in AUTH_DIAGNOSTICS_REQUIRED else "likely"
        elif acse.kind == "ABRT" and acse.abort_diagnostic in AUTH_ABORT_DIAGNOSTICS:
            auth_text = f"the IED aborted the association with ACSE abort diagnostic {acse.abort_diagnostic_name} ({acse.abort_diagnostic})"
        if auth_text:
            outcome, security = "authentication-suspected", "authentication"
            findings.append(
                Finding(
                    codes.tool("security-not-supported").key,
                    certainty,
                    f"{auth_text}: it expects ACSE authentication (e.g. a password). This version of mms-client does "
                    "not support ACSE authentication (no security in v1), so it cannot associate with this IED as "
                    "configured",
                    [e for e in evidence if e.startswith("acse")],
                )
            )

    findings += _layer_findings(observed, assoc, cotp)
    if outcome in ("accepted", "unknown") and observed == outcome:
        layer = None if outcome == "accepted" else layer
    elif layer is None:
        layer = OUTCOME_LAYERS.get(outcome) or (detail_step.layer if detail_step else None)

    if outcome == "accepted":
        detail = assoc.detail if assoc else "association accepted"
    else:
        head = codes.ASSOCIATION_OUTCOMES[outcome]
        if outcome != observed:
            head += f" (observed: {codes.ASSOCIATION_OUTCOMES[observed].lower()})"
        detail = f"{head}: {detail_step.detail}" if detail_step else head

    return AssociationFailure(
        outcome=outcome,
        layer=layer,
        observed=observed,
        detail=detail,
        evidence=evidence,
        findings=findings,
        acse_diagnostic=acse_diag,
        security=security,
        reachable=reachable,
        congestion=_congestion(assoc, cotp),
        closed_after_s=closed_after_s,
    )


def _slot_pattern(failure: AssociationFailure) -> str | None:
    """Why the refusal fits "all slots in use", or None if it does not (DIA-4 b)."""
    o = failure.observed
    after = f" {failure.closed_after_s:.3f} s after connect" if failure.closed_after_s is not None else ""
    if o == "tcp-closed-immediately":
        return (
            f"TCP was accepted and then closed{after} without any ISO response: this is how libiec61850-based "
            "servers refuse a connection when all association slots are in use (observed in spike RSK-3)"
        )
    if o == "acse-rejected-transient":
        return "the association was rejected as transient (a temporary condition such as no free association)"
    if o == "accepted-then-closed":
        return f"the association was accepted and then closed by the device{after}"
    if o in ("cotp-no-response", "initiate-no-response"):
        return "the device accepted TCP but did not answer the association request in time"
    if o == "tcp-timeout" and failure.reachable:
        return "the device answers ping but not the TCP connection request (a full connection backlog looks like this)"
    if failure.congestion:
        return "the refusal carried an explicit congestion / resource reason code"
    return None


def assess_association_limits(
    failure: AssociationFailure, *, quirks: QuirkInfo | None, tool_holds_association: bool
) -> list[Finding]:
    """DIA-4: (a) known limits from the quirks file, (b) the "slots probably exhausted" hint where
    the refusal pattern fits (``likely``, never fact), (c) the reminder that this tool itself
    occupies one slot while connected."""
    findings: list[Finding] = []
    release_s = quirks.slot_release_after_abrupt_disconnect_s if quirks else None

    # (a) what the quirks file records for this model / firmware
    if quirks is not None and (
        quirks.max_associations is not None or release_s is not None or quirks.refusal_behaviour
    ):
        parts = []
        if quirks.max_associations is not None:
            parts.append(f"at most {quirks.max_associations} simultaneous associations")
        if release_s is not None:
            parts.append(
                f"a slot held by a client that disconnected abruptly is freed after about {release_s:g} s"
            )
        if quirks.refusal_behaviour:
            parts.append(f"when all slots are in use: {quirks.refusal_behaviour}")
        src = quirks.source or "source not recorded"
        if quirks.added:
            src += f", added {quirks.added}"
        findings.append(
            Finding(
                codes.tool("known-association-limit").key,
                "check",
                f"the quirks file records for {quirks.describe_match()}: " + "; ".join(parts) + f" ({src})",
                [f"quirks entry {quirks.origin}"] if quirks.origin else [],
            )
        )

    # (b) the refusal pattern
    why = _slot_pattern(failure)
    if why is not None and not failure.ok:
        evidence = [why]
        if tool_holds_association:
            evidence.append("this tool itself already holds an association with the device (one slot)")
        if quirks is not None and quirks.max_associations is not None:
            evidence.append(f"known limit for this model: {quirks.max_associations} associations")
        text = SLOTS_HINT
        if release_s is not None:
            text += f"; this model frees such slots after about {release_s:g} s, so retrying after that may succeed"
        findings.append(Finding(codes.tool("slots-probably-exhausted").key, "likely", text, evidence))

    # (c) this tool occupies a slot too
    text = "while connected, this tool occupies one of the device's association slots"
    if tool_holds_association:
        text += (
            "; it holds one with this device right now, which is one fewer for the clients under test "
            "(disconnect it before testing whether a neighbour can associate)"
        )
    findings.append(Finding(codes.tool("association-slot-occupied-by-tool").key, "fact", text, []))
    return findings


_IED_CONNECT_NOTES: dict[str, str] = {
    "connection-rejected": (
        "libiec61850 reports every failed association attempt with this code (TCP refused, closed or timing out, "
        "and COTP / session / ACSE / MMS rejections alike), so it does not say which layer failed; the layered "
        "probes in `diagnose` find out"
    ),
    "timeout": "no answer within the timeout: the device, the path to it, or its MMS server did not respond",
    "connection-lost": "the connection dropped during the exchange (the device or the network closed it)",
    "not-connected": "no association is open (connect first)",
    "already-connected": "an association is already open on this connection object",
    "user-provided-invalid-argument": "the stack rejected an argument (host name, port or local address)",
    "outstanding-call-limit-reached": "too many requests outstanding at once for the negotiated limit",
    "service-not-supported": "the device did not offer this service during association (services-supported)",
    "unknown": "libiec61850 gave no specific reason; the layered probes in `diagnose` find out more",
}


def explain_ied_connect_error(
    err: ErrorInfo, *, elapsed_s: float | None = None, timeout_s: float | None = None
) -> str:
    """One-line note for a connect error from libiec61850 when we could not probe deeper.

    With ``elapsed_s`` and ``timeout_s`` the note also says whether the attempt ran into the
    timeout (the only way to tell a timeout from a refusal: libiec61850 reports both as
    ``connection-rejected``, see the module docstring).
    """
    if err.domain == MmsDomain.IED:
        note = _IED_CONNECT_NOTES.get(
            err.name, "libiec61850 error during association; run `diagnose` for the layered probes"
        )
    elif err.domain == codes.Domain.ASSOCIATION:
        note = codes.ASSOCIATION_OUTCOMES.get(err.name, err.name)
    elif err.domain == MmsDomain.MMS and err.name in (
        "connection-rejected",
        "connection-lost",
        "service-timeout",
    ):
        note = {
            "connection-rejected": _IED_CONNECT_NOTES["connection-rejected"],
            "connection-lost": _IED_CONNECT_NOTES["connection-lost"],
            "service-timeout": _IED_CONNECT_NOTES["timeout"],
        }[err.name]
    elif err.domain == codes.Domain.TOOL and err.name == "local-address-missing":
        note = "the local address to bind to is not configured on this host (see the `ip addr add` command given)"
    else:
        note = "error during association; run `diagnose` for the layered probes"
    if elapsed_s is not None and timeout_s:
        if elapsed_s >= 0.9 * timeout_s:
            note += (
                f"; it took {elapsed_s:.1f} s (the full {timeout_s:g} s timeout): nothing answered in time"
            )
        else:
            note += f"; it failed after {elapsed_s:.2f} s, before the {timeout_s:g} s timeout: something refused or closed it"
    return f"{err}: {note}"
