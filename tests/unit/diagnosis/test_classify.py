"""Classification (DIA-3/4/6) of probe results, including the association-limit reasoning."""

from __future__ import annotations

import pytest

from ied_client import codes
from ied_client.quirks import QuirkInfo
from mms_protocol import codes as mms_codes
from mms_protocol.diagnosis import iso, probes
from mms_protocol.diagnosis.classify import (
    SLOTS_HINT,
    assess_association_limits,
    classify_association,
    explain_ied_connect_error,
)
from mms_protocol.diagnosis.probes import ProbeResult, iso_associate

from . import fakes as f
from .fakes import FakeServer

T = 0.5


def pr(name: str, ok, outcome: str, detail: str = "", **raw) -> ProbeResult:
    err = codes.association(outcome) if ok is False and outcome in codes.ASSOCIATION_OUTCOMES else None
    return ProbeResult("network", name, ok, outcome, 0.01, detail or outcome, err, raw)


TCP_OK = pr("tcp-102", True, "accepted", port=102)
TCP_REFUSED = pr("tcp-102", False, "tcp-refused", "TCP 10.0.0.5:102: connection refused", port=102)
TLS_OPEN = pr("tcp-3782", True, "tls", "TCP 3782 is open and speaks TLS (TLSv1.2)", port=3782)
TLS_CLOSED = pr("tcp-3782", False, "tcp-refused", port=3782)


def keys(findings):
    return [x.key for x in findings]


def probe_fake(handler, **kw):
    kw.setdefault("linger_s", 0.2)
    with FakeServer(handler) as srv:
        a = iso_associate("127.0.0.1", srv.port, T, **kw)
        tcp = a.tcp
    return tcp, a


# --- DIA-6 --------------------------------------------------------------------------------------
def test_tls_suspected_when_102_refused_and_3782_open():
    c = classify_association(TCP_REFUSED, None, None, TLS_OPEN)
    assert (c.outcome, c.observed, c.layer, c.security) == ("tls-suspected", "tcp-refused", "network", "tls")
    assert c.error == codes.association("tls-suspected") and not c.ok
    (sec,) = [x for x in c.findings if x.key == "tool:security-not-supported"]
    assert sec.certainty == "likely" and "no TLS support" in sec.text and "3782" in sec.text
    assert any("tcp-3782" in e for e in c.evidence)


def test_no_tls_suspicion_when_3782_closed_or_not_checked():
    assert classify_association(TCP_REFUSED, None, None, TLS_CLOSED).outcome == "tcp-refused"
    c = classify_association(TCP_REFUSED)
    assert (c.outcome, c.layer, c.security) == ("tcp-refused", "network", None)


def test_tls_suspected_when_closed_immediately_but_slot_hint_kept():
    tcp, a = probe_fake(lambda c: c.close())
    c = classify_association(tcp, None, a, TLS_OPEN)
    assert (c.outcome, c.observed, c.layer) == ("tls-suspected", "tcp-closed-immediately", "transport")
    limits = assess_association_limits(c, quirks=None, tool_holds_association=False)
    assert "tool:slots-probably-exhausted" in keys(limits)  # both explanations stay on the table


def test_tls_record_on_mms_port():
    def handler(c):
        c.recv_tpkt()
        c.send(bytes.fromhex("15030300020228"))

    tcp, a = probe_fake(handler)
    c = classify_association(tcp, None, a, None)
    assert c.outcome == "tls-suspected" and c.observed == "cotp-invalid-response"
    assert "TLS record" in c.findings[0].text


@pytest.mark.parametrize(("diag", "certainty"), [(14, "fact"), (12, "fact"), (13, "likely"), (11, "likely")])
def test_authentication_suspected_from_aare(diag, certainty):
    tcp, a = probe_fake(f.cotp_then(f.accept(f.cpa(f.aare(1, diag))), linger=0.5))
    c = classify_association(tcp, None, a, None)
    assert (c.outcome, c.observed, c.layer, c.security) == (
        "authentication-suspected", "acse-rejected-permanent", "mms", "authentication",
    )  # fmt: skip
    assert c.acse_diagnostic == codes.info(mms_codes.MmsDomain.ACSE_DIAG, diag)
    (sec,) = [x for x in c.findings if x.key == "tool:security-not-supported"]
    assert sec.certainty == certainty
    assert "does not support ACSE authentication" in sec.text and f"({diag})" in sec.text


def test_authentication_suspected_from_abort():
    tcp, a = probe_fake(f.cotp_then(f.session_abort(iso.presentation_aru(f.abrt(0, 6)))))
    c = classify_association(tcp, None, a, None)
    assert c.outcome == "authentication-suspected" and c.observed == "acse-aborted"


def test_permanent_rejection_without_reason_suggests_checks():
    tcp, a = probe_fake(f.cotp_then(f.accept(f.cpa(f.aare(1, 1))), linger=0.5))
    c = classify_association(tcp, None, a, None)
    assert c.outcome == "acse-rejected-permanent" and c.security is None
    (chk,) = c.findings
    assert chk.key == "association:acse-rejected-permanent" and chk.certainty == "check"
    assert "authentication" in chk.text and "IP" in chk.text
    limits = assess_association_limits(c, quirks=None, tool_holds_association=False)
    assert "tool:slots-probably-exhausted" not in keys(limits)


def test_ap_title_and_selector_checks():
    tcp, a = probe_fake(f.cotp_then(f.accept(f.cpa(f.aare(1, 3))), linger=0.5))
    c = classify_association(tcp, None, a, None)
    assert any("AP title" in x.text for x in c.findings)
    tcp, a = probe_fake(f.cotp_then(f.refuse(0x81)))
    c = classify_association(tcp, None, a, None)
    assert c.outcome == "session-refused" and "SSEL" in c.findings[0].text


# --- plain outcomes and evidence ------------------------------------------------------------------
def test_accepted():
    tcp, a = probe_fake(f.full_server)
    c = classify_association(tcp, None, a, None)
    assert c.ok and c.outcome == "accepted" and c.layer is None and c.findings == []
    assert c.detail.startswith("association accepted: max PDU 65000")
    assert [e.split(":")[0] for e in c.evidence][:3] == [tcp.name, "cotp", "iso-session"]
    assert c.to_json()["error"] == {"domain": "association", "code": None, "name": "accepted"}


def test_cotp_only_and_tcp_only():
    cotp = pr("cotp", True, "accepted", "COTP CC")
    c = classify_association(TCP_OK, cotp)
    assert c.outcome == "unknown" and any("association not probed" in e for e in c.evidence)
    c = classify_association(TCP_OK)
    assert c.outcome == "unknown"
    rej = pr("cotp", False, "cotp-rejected", "COTP DR: reason 0x81")
    rej.layer = "transport"
    c = classify_association(TCP_OK, rej)
    assert (c.outcome, c.layer) == ("cotp-rejected", "transport") and "COTP DR" in c.detail


def test_bind_failure_is_unknown_network():
    tcp = probes.tcp_connect("127.0.0.1", 1, T, local_ip="203.0.113.77")
    c = classify_association(tcp)
    assert c.outcome == "unknown" and c.layer == "network" and "not configured" in c.detail


# --- DIA-4 ------------------------------------------------------------------------------------------
QUIRK = QuirkInfo(
    vendor="Acme", model="BCU-5*", max_associations=4, slot_release_after_abrupt_disconnect_s=60,
    refusal_behaviour="TCP accepted then closed", source="spikes/rsk6.md", added="2026-09-20", origin="q.yaml#0",
)  # fmt: skip


def test_slot_hint_for_closed_immediately_with_quirks():
    tcp, a = probe_fake(lambda c: c.close())
    c = classify_association(tcp, None, a, None)
    assert c.outcome == "tcp-closed-immediately" and c.closed_after_s is not None
    fs = assess_association_limits(c, quirks=QUIRK, tool_holds_association=True)
    assert keys(fs) == [
        "tool:known-association-limit", "tool:slots-probably-exhausted", "tool:association-slot-occupied-by-tool",
    ]  # fmt: skip
    known, slots, own = fs
    assert known.certainty == "check" and "at most 4 simultaneous associations" in known.text
    assert "about 60 s" in known.text and "spikes/rsk6.md" in known.text
    assert slots.certainty == "likely" and slots.text.startswith(SLOTS_HINT) and "60 s" in slots.text
    assert any("libiec61850" in e for e in slots.evidence)
    assert any("this tool itself already holds" in e for e in slots.evidence)
    assert own.certainty == "fact" and "holds one with this device right now" in own.text


@pytest.mark.parametrize(
    "handler",
    [
        f.cotp_then(f.accept(f.cpa(f.aare(2, 1)))),  # ACSE rejected-transient
        f.cotp_then(f.CAP_AC),  # accepted then closed
        f.cotp_then(f.refuse(0x02, f.cpr(1))),  # presentation: temporary congestion
        f.cotp_then(f.cotp_dr(0x81)),  # COTP DR: congestion
    ],
)
def test_slot_hint_patterns(handler):
    tcp, a = probe_fake(handler, linger_s=1.0)
    c = classify_association(tcp, None, a, None)
    fs = assess_association_limits(c, quirks=None, tool_holds_association=False)
    assert keys(fs) == ["tool:slots-probably-exhausted", "tool:association-slot-occupied-by-tool"], c.outcome
    assert fs[0].certainty == "likely"


def test_slot_hint_for_silence_and_for_tcp_timeout_on_pingable_device():
    def silent(c):
        c.recv_tpkt()
        c.wait_closed(2)

    tcp, a = probe_fake(silent)
    c = classify_association(tcp, None, a, None)
    assert c.outcome == "cotp-no-response"
    assert "tool:slots-probably-exhausted" in keys(
        assess_association_limits(c, quirks=None, tool_holds_association=False)
    )
    timeout = pr("tcp-102", False, "tcp-timeout")
    icmp = pr("icmp", True, "reply")
    c = classify_association(timeout, icmp=icmp)
    assert c.reachable and "tool:slots-probably-exhausted" in keys(
        assess_association_limits(c, quirks=None, tool_holds_association=False)
    )
    c = classify_association(timeout, icmp=pr("icmp", False, "no-reply"))
    assert "tool:slots-probably-exhausted" not in keys(
        assess_association_limits(c, quirks=None, tool_holds_association=False)
    )


def test_no_slot_hint_when_accepted_but_reminder_always():
    tcp, a = probe_fake(f.full_server)
    c = classify_association(tcp, None, a, None)
    fs = assess_association_limits(c, quirks=None, tool_holds_association=False)
    assert keys(fs) == ["tool:association-slot-occupied-by-tool"]
    assert "holds one" not in fs[0].text
    fs = assess_association_limits(c, quirks=QUIRK, tool_holds_association=False)
    assert keys(fs) == ["tool:known-association-limit", "tool:association-slot-occupied-by-tool"]
    empty = QuirkInfo(vendor="Acme", model="X", notes="nothing about slots")
    assert keys(assess_association_limits(c, quirks=empty, tool_holds_association=False)) == [
        "tool:association-slot-occupied-by-tool"
    ]


# --- libiec61850's generic connect errors -------------------------------------------------------------
def test_explain_ied_connect_error():
    s = explain_ied_connect_error(mms_codes.ied_error(5))
    assert s.startswith("ied:connection-rejected (5): ") and "does not say which layer failed" in s
    s = explain_ied_connect_error(mms_codes.ied_error(5), elapsed_s=2.0, timeout_s=2.0)
    assert "full 2 s timeout" in s
    s = explain_ied_connect_error(mms_codes.ied_error(5), elapsed_s=0.01, timeout_s=2.0)
    assert "before the 2 s timeout" in s
    assert "timeout" in explain_ied_connect_error(mms_codes.ied_error(20))
    assert explain_ied_connect_error(codes.association("tcp-refused")).endswith(
        "TCP connection refused (RST) on the MMS port"
    )
    assert "not configured" in explain_ied_connect_error(codes.tool("local-address-missing"))
    assert explain_ied_connect_error(mms_codes.mms_error(3)).startswith("mms:service-timeout (3): ")
    assert "diagnose" in explain_ied_connect_error(mms_codes.ied_error(34))
