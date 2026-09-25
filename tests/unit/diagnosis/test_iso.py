"""PDU builders and parsers, checked against a capture of libiec61850 1.6.1 traffic."""

from __future__ import annotations

import pytest

from mms_protocol.diagnosis import ber, iso
from mms_protocol.diagnosis.ber import CONTEXT
from mms_protocol.diagnosis.iso import AssociateParams, IsoError

from . import fakes as f


# --- requests: byte-for-byte what libiec61850's client sends ----------------------------------
def test_requests_match_libiec61850_capture():
    assert iso.tpkt(iso.connection_request()) == f.CAP_CR
    assert iso.tpkt(iso.cotp_dt(iso.association_request())) == f.CAP_CN
    assert iso.tpkt(iso.cotp_dt(iso.conclude_request())) == f.CAP_CONCLUDE_REQ
    assert iso.tpkt(iso.cotp_dt(iso.release_request())) == f.CAP_FINISH
    assert iso.tpkt(iso.cotp_dt(iso.abort_request())) == f.CAP_ABORT


def test_request_parameters_change_the_pdus():
    p = AssociateParams(
        max_pdu_size=1000, calling_ap_title=None, called_ap_title="1.3.9999.13", called_ae_qualifier=None
    )
    cn = iso.parse_spdu(iso.association_request(p))
    assert cn.si == iso.SPDU_CN and cn.version == 2
    cp, _ = ber.decode_one(cn.user_data)
    ((ctx, aarq_bytes),) = iso.parse_user_data(cp.child(CONTEXT, 2).children()[-1])
    assert ctx == 1
    aarq = iso.parse_acse(aarq_bytes)
    assert aarq.kind == "AARQ" and aarq.application_context == "1.0.9506.2.3"
    fields = ber.decode_one(aarq_bytes)[0].children()
    assert [t.number for t in fields] == [1, 2, 7, 30]  # no called AE qualifier, no calling AP title
    assert fields[1].children()[0].as_oid() == "1.3.9999.13"
    init = iso.parse_mms(aarq.user_information)
    assert init.name == "initiate-RequestPDU" and init.initiate.max_pdu_size == 1000
    assert (
        iso.parse_cotp(iso.connection_request(AssociateParams(called_tsel=b"\x00\x02"))).params[0xC2]
        == b"\x00\x02"
    )


# --- COTP ---------------------------------------------------------------------------------------
def test_parse_cc_from_capture():
    cc = iso.parse_cotp(f.CAP_CC[4:])
    assert (cc.kind, cc.dst_ref, cc.src_ref, cc.class_option, cc.tpdu_size) == ("CC", 1, 1, 0, 8192)


def test_parse_dr_and_er():
    dr = iso.parse_cotp(f.cotp_dr(0x81)[4:])
    assert dr.kind == "DR" and dr.reason == 0x81 and dr.reason_name == "remote-transport-entity-congestion"
    er = iso.parse_cotp(f.cotp_er(2)[4:])
    assert er.kind == "ER" and er.reason_name == "invalid-tpdu-type" and er.params[0xC1] == b"\xe0"
    assert iso.parse_cotp(f.cotp_dr(0x42)[4:]).reason_name == "unknown-66"


@pytest.mark.parametrize("bad", ["", "00", "05e00000", "ff00", "08e00000000000c105"])
def test_parse_cotp_rejects_garbage(bad):
    with pytest.raises(IsoError):
        iso.parse_cotp(bytes.fromhex(bad))


def test_tpkt_header_checks():
    assert iso.tpkt_length(f.CAP_CC[:4]) == len(f.CAP_CC)
    with pytest.raises(IsoError):
        iso.tpkt_length(bytes.fromhex("15030300"))
    assert iso.looks_like_tls(bytes.fromhex("150303")) and not iso.looks_like_tls(f.CAP_CC)


# --- session / presentation / ACSE / MMS: the captured accept -----------------------------------
def test_parse_captured_accept_layer_by_layer():
    pdu = iso.parse_cotp(f.CAP_AC[4:])
    assert pdu.kind == "DT" and pdu.eot
    ac = iso.parse_spdu(pdu.user_data)
    assert ac.name == "ACCEPT" and ac.version == 2 and ac.param(iso.PI_CALLED_SSEL) == b"\x00\x01"
    ppdu = iso.parse_presentation(ac.user_data)
    assert ppdu.kind == "CPA" and ppdu.responding_psel == b"\x00\x00\x00\x01"
    assert ppdu.context_results == ((0, None), (0, None))
    aare = iso.parse_acse(ppdu.apdu())
    assert (aare.kind, aare.result, aare.diagnostic_source, aare.diagnostic) == ("AARE", 0, "service-user", 0)
    init = iso.parse_mms(aare.user_information)
    p = init.initiate
    assert init.name == "initiate-ResponsePDU"
    assert (p.max_pdu_size, p.max_serv_outstanding_calling, p.max_serv_outstanding_called) == (65000, 5, 5)
    assert (p.data_structure_nesting_level, p.version) == (10, 1)
    assert p.parameter_cbb_names() == ["str1", "str2", "vnam", "valt", "vlis"]
    assert p.services_supported_bits == 85
    assert p.supports("identify") and p.supports("fileDirectory") and not p.supports("rename")
    assert p.supported_services()[:3] == ["status", "getNameList", "identify"]


def test_parse_release_exchange():
    dt = iso.parse_spdu(iso.parse_cotp(f.CAP_CONCLUDE_RESP[4:]).user_data)
    assert dt.si == iso.SPDU_DT
    (ctx, mms), = iso.parse_presentation_data(dt.user_data)  # fmt: skip
    assert ctx == 3 and iso.parse_mms(mms).name == "conclude-ResponsePDU"
    dn = iso.parse_spdu(iso.parse_cotp(f.CAP_DISCONNECT[4:]).user_data)
    assert dn.name == "DISCONNECT"
    assert iso.parse_acse(iso.parse_presentation_data(dn.user_data)[0][1]).kind == "RLRE"


# --- rejections ------------------------------------------------------------------------------------
def test_refuse_with_reason_and_cpr():
    rf = iso.parse_spdu(iso.parse_cotp(f.refuse(0x02, f.cpr(1))[4:]).user_data)
    assert rf.name == "REFUSE" and rf.refuse_reason == 2 and rf.refuse_reason_name == "rejected-by-user"
    cpr = iso.parse_presentation(rf.user_data)
    assert (
        cpr.kind == "CPR" and cpr.provider_reason == 1 and cpr.provider_reason_name == "temporary-congestion"
    )
    rf2 = iso.parse_spdu(iso.parse_cotp(f.refuse(0x81)[4:]).user_data)
    assert rf2.refuse_reason_name == "session-selector-unknown" and rf2.user_data == b""


def test_rejected_aare_with_authentication_diagnostic():
    a = iso.parse_acse(f.aare(1, 14))
    assert a.result_name == "rejected-permanent" and a.diagnostic_name == "authentication-required"
    p = iso.parse_acse(f.aare(2, 2, provider=True))
    assert p.result_name == "rejected-transient" and p.diagnostic_source == "service-provider"
    assert p.diagnostic_name == "no-common-acse-version"


def test_abort_ppdus():
    ab = iso.parse_spdu(iso.parse_cotp(f.session_abort(iso.presentation_aru(f.abrt(0, 6)))[4:]).user_data)
    assert ab.name == "ABORT" and ab.transport_disconnect == 3
    aru = iso.parse_presentation(ab.user_data, abort=True)
    assert aru.kind == "ARU"
    abrt = iso.parse_acse(aru.apdu())
    assert abrt.kind == "ABRT" and abrt.abort_diagnostic_name == "authentication-required"
    arp = iso.parse_presentation(bytes.fromhex("3003800101"), abort=True)
    assert arp.kind == "ARP" and arp.abort_reason == 1


def test_initiate_error_and_reject():
    e = iso.parse_mms(f.initiate_error(8, 2))
    assert e.name == "initiate-ErrorPDU" and e.error_class_name == "initiate"
    assert e.error_code_name == "max-segment-insufficient"
    r = iso.parse_mms(bytes.fromhex("a406800101850101"))
    assert r.name == "rejectPDU" and r.error_class_name == "pdu-error" and r.error_code == 1


@pytest.mark.parametrize(
    ("parser", "bad"),
    [
        (iso.parse_presentation, "0400"),
        (iso.parse_acse, "3000"),
        (iso.parse_mms, "0201"),
        (iso.parse_acse, "6005a10306"),
    ],
)
def test_parsers_raise_iso_error_on_garbage(parser, bad):
    with pytest.raises(IsoError):
        parser(bytes.fromhex(bad))


def test_session_long_parameters_roundtrip():
    big = bytes(300)
    s = iso.parse_spdu(iso.session_connect(big))
    assert s.user_data == big
    with pytest.raises(IsoError):
        iso.parse_spdu(bytes.fromhex("0d10c1"))
