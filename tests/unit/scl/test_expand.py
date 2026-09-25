"""expand_server(): the server model as a client sees it."""

from __future__ import annotations

import pytest

from ied_client.core.refs import ObjectRef
from ied_client.protocol.types import DatasetRef
from ied_client.scl import (
    ExpectedAttribute,
    ExpectedDO,
    ExpectedServer,
    SclDocument,
    SclError,
    ValueType,
    expand_server,
    load_scl,
)

from ._data import bcu_text

# ---------------------------------------------------------------------------
# Domains, LNs, tree
# ---------------------------------------------------------------------------


def test_domain_names_with_and_without_ld_name(bcu: ExpectedServer, ed1: ExpectedServer) -> None:
    assert bcu.domains == ["BCU1CTRL", "BAY1_PROT"]  # CTRL: iedName+ldInst; PROT: ldName
    assert bcu.ld("CTRL") is bcu.ld("BCU1CTRL")
    assert bcu.ld("PROT").ld_name == "BAY1_PROT"
    assert ed1.domains == ["TEMPLATELD0"]


def test_ln_names(bcu: ExpectedServer) -> None:
    assert [ln.name for ln in bcu.ld("CTRL").lns] == ["LLN0", "LPHD1", "CSWI1", "XCBR1", "GGIO1", "MMXU1"]
    lln0 = bcu.ln("BCU1CTRL/LLN0")
    assert lln0.is_ln0 and lln0.ln_class == "LLN0"
    assert bcu.ln("PROT/PTOC1").ref == "BAY1_PROT/PTOC1"


def test_server_identity(bcu: ExpectedServer) -> None:
    assert (bcu.ied_name, bcu.ap_name, bcu.manufacturer, bcu.ied_type) == ("BCU1", "S1", "Rackworks", "BCU-100")
    assert bcu.ip == "10.0.0.11"
    assert bcu.edition.edition == "Ed2"
    assert bcu.warnings == []


def test_tree_keeps_cdc_and_do_da_distinction(bcu: ExpectedServer) -> None:
    phv = bcu.do("BCU1CTRL/MMXU1.PhV")
    assert isinstance(phv, ExpectedDO)
    assert phv.cdc == "WYE" and not phv.is_sdo
    assert [s.name for s in phv.sdos] == ["phsA", "phsB", "phsC"]
    phs_a = phv.child("phsA")
    assert isinstance(phs_a, ExpectedDO) and phs_a.is_sdo and phs_a.cdc == "CMV"
    assert phs_a.ref == "BCU1CTRL/MMXU1.PhV.phsA"
    cval = phs_a.child("cVal")
    assert isinstance(cval, ExpectedAttribute) and cval.is_constructed
    mag = cval.child("mag")
    assert [c.name for c in mag.children] == ["f"]
    assert bcu.do("BCU1CTRL/CSWI1.Pos").cdc == "DPC"
    assert bcu.do("BAY1_PROT/PTOC1.StrVal").cdc == "ASG"
    assert phv.fcs == {"MX", "CF"}


def test_sdo_leaf_reference_and_object_ref(bcu: ExpectedServer) -> None:
    a = bcu.attribute("BCU1CTRL/MMXU1.PhV.phsA.cVal.mag.f")
    assert a is not None
    assert a.fc == "MX"
    assert a.b_type == "FLOAT32"
    assert a.value_type == ValueType("float", 32)
    assert a.do_path == ("PhV", "phsA")
    assert a.da_path == ("cVal", "mag", "f")
    assert a.object_ref == ObjectRef("BCU1CTRL", "MMXU1", ("PhV", "phsA", "cVal", "mag", "f"), "MX")
    assert a.do_ref == "BCU1CTRL/MMXU1.PhV.phsA"
    # BDAs inherit the DA's trigger options
    assert a.trigger_options == {"dchg", "dupd"}
    assert a.trigger_options_int == 5
    assert a.is_leaf


def test_flat_list_contains_constructed_and_leaves_in_order(bcu: ExpectedServer) -> None:
    refs = [a.ref for a in bcu.attributes if a.do_ref == "BCU1CTRL/CSWI1.Pos"]
    assert refs[:4] == ["BCU1CTRL/CSWI1.Pos.SBOw", "BCU1CTRL/CSWI1.Pos.SBOw.ctlVal",
                        "BCU1CTRL/CSWI1.Pos.SBOw.origin", "BCU1CTRL/CSWI1.Pos.SBOw.origin.orCat"]
    oper = bcu.attribute("BCU1CTRL/CSWI1.Pos.Oper", "CO")
    assert oper.value_type == ValueType("structure")
    assert [c.name for c in oper.children] == ["ctlVal", "origin", "ctlNum", "T", "Test", "Check"]
    or_cat = bcu.attribute("BCU1CTRL/CSWI1.Pos.Oper.origin.orCat")
    assert (or_cat.fc, or_cat.b_type, or_cat.enum_type) == ("CO", "Enum", "OrCatKind")
    assert or_cat.enum_values[3] == "remote-control"
    assert or_cat.object_ref == ObjectRef("BCU1CTRL", "CSWI1", ("Pos", "Oper", "origin", "orCat"), "CO")
    check = bcu.attribute("BCU1CTRL/CSWI1.Pos.Oper.Check")
    assert check.value_type == ValueType("bit-string", 2)
    assert bcu.attribute("BCU1CTRL/CSWI1.Pos.stVal").value_type == ValueType("bit-string", 2)
    assert bcu.attribute("BCU1CTRL/GGIO1.SPCSO2.SBO").value_type == ValueType("visible-string", 129)


def test_find(bcu: ExpectedServer) -> None:
    assert bcu.find("BCU1CTRL").inst == "CTRL"
    assert bcu.find("BCU1CTRL/GGIO1").name == "GGIO1"
    assert isinstance(bcu.find("BCU1CTRL/GGIO1.IntIn1"), ExpectedDO)
    assert isinstance(bcu.find("BCU1CTRL/GGIO1.IntIn1.Oper.ctlVal"), ExpectedAttribute)
    assert bcu.find("BCU1CTRL/GGIO1.Nope") is None


def test_transient_do(bcu: ExpectedServer) -> None:
    op = bcu.do("BAY1_PROT/PTOC1.Op")
    assert op.transient
    assert all(a.transient for a in op.walk_attributes())
    assert not bcu.do("BAY1_PROT/PTOC1.Str").transient


def test_sg_attributes_get_se_copies(bcu: ExpectedServer) -> None:
    sg = bcu.attribute("BAY1_PROT/PTOC1.StrVal.setMag.f", "SG")
    se = bcu.attribute("BAY1_PROT/PTOC1.StrVal.setMag.f", "SE")
    assert sg is not None and se is not None and sg is not se
    assert se.object_ref == ObjectRef("BAY1_PROT", "PTOC1", ("StrVal", "setMag", "f"), "SE")
    assert [a.fc for a in bcu.attributes_for("BAY1_PROT/PTOC1.OpDlTmms.setVal")] == ["SG", "SE"]
    # the SE copy directly follows its SG attribute in the DO
    names = [(c.name, c.fc) for c in bcu.do("BAY1_PROT/PTOC1.OpDlTmms").attributes]
    assert names[:2] == [("setVal", "SG"), ("setVal", "SE")]


def test_explicit_se_is_not_duplicated() -> None:
    text = bcu_text().replace(
        '<DA name="setVal" bType="INT32" fc="SG" dchg="true"/>',
        '<DA name="setVal" bType="INT32" fc="SG" dchg="true"/>\n      <DA name="setVal" bType="INT32" fc="SE"/>')
    srv = expand_server(load_scl(text), "BCU1")
    assert [a.fc for a in srv.attributes_for("BAY1_PROT/PTOC1.OpDlTmms.setVal")] == ["SG", "SE"]


# ---------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------


def test_enum_value_resolution(bcu: ExpectedServer) -> None:
    beh = bcu.attribute("BCU1CTRL/LLN0.Beh.stVal")
    assert (beh.value, beh.parsed_value, beh.value_source) == ("on", 1, "instance")
    health = bcu.attribute("BCU1CTRL/LPHD1.PhyHealth.stVal")
    assert health.parsed_value == 1
    mult = bcu.attribute("BCU1CTRL/MMXU1.TotW.units.multiplier")
    assert (mult.value, mult.parsed_value) == ("k", 3)
    si = bcu.attribute("BCU1CTRL/MMXU1.TotW.units.SIUnit")
    assert (si.value, si.parsed_value) == ("W", 38)
    # template default "" resolves to the ord whose literal is empty
    hz_mult = bcu.attribute("BCU1CTRL/MMXU1.Hz.units.multiplier")
    assert (hz_mult.value, hz_mult.parsed_value, hz_mult.value_source) == ("", 0, "template")


def test_doi_overrides_template_value(bcu: ExpectedServer) -> None:
    ctl = bcu.attribute("BCU1CTRL/CSWI1.Pos.ctlModel")
    assert (ctl.value, ctl.parsed_value, ctl.value_source) == ("sbo-with-enhanced-security", 4, "instance")
    assert bcu.attribute("BCU1CTRL/GGIO1.SPCSO1.ctlModel").parsed_value == 1
    assert bcu.attribute("BCU1CTRL/GGIO1.SPCSO2.ctlModel").parsed_value == 2
    assert bcu.attribute("BCU1CTRL/GGIO1.IntIn1.ctlModel").parsed_value == 3
    # template value kept where there is no DOI
    lsta = bcu.attribute("BCU1CTRL/LLN0.LocSta.ctlModel")
    assert (lsta.parsed_value, lsta.value_source) == (0, "template")
    sbo_to = bcu.attribute("BCU1CTRL/CSWI1.Pos.sboTimeout")
    assert (sbo_to.parsed_value, sbo_to.value_source) == (30000, "instance")
    assert bcu.attribute("BCU1CTRL/GGIO1.SPCSO2.sboTimeout").parsed_value == 20000
    db = bcu.attribute("BCU1CTRL/MMXU1.TotW.db")
    assert (db.parsed_value, db.value_source) == (1000, "instance")
    assert bcu.attribute("BCU1CTRL/MMXU1.Hz.db").parsed_value == 500
    assert bcu.attribute("BCU1CTRL/GGIO1.IntIn1.maxVal").parsed_value == 100


def test_sdi_values_in_sdo_and_struct(bcu: ExpectedServer) -> None:
    unit = bcu.attribute("BCU1CTRL/MMXU1.PhV.phsA.units.SIUnit")
    assert unit.parsed_value == 29
    assert bcu.attribute("BCU1CTRL/MMXU1.PhV.phsB.units.SIUnit").value is None


def test_string_and_float_values(bcu: ExpectedServer) -> None:
    ld_ns = bcu.attribute("BCU1CTRL/LLN0.NamPlt.ldNs")
    assert (ld_ns.fc, ld_ns.parsed_value) == ("EX", "IEC 61850-7-4:2007B")
    assert bcu.attribute("BCU1CTRL/LPHD1.PhyNam.serNum").parsed_value == "RW-000123"
    assert bcu.attribute("BAY1_PROT/PTOC1.StrVal.minVal.f").parsed_value == pytest.approx(0.1)


def test_setting_group_values(bcu: ExpectedServer) -> None:
    f = bcu.attribute("BAY1_PROT/PTOC1.StrVal.setMag.f", "SG")
    assert f.values_by_sg == {1: "1.2", 2: "1.5"}
    assert f.value == "1.2"  # actSG = 1
    assert bcu.attribute("BAY1_PROT/PTOC1.StrVal.setMag.f", "SE").value == "1.2"
    op_dl = bcu.attribute("BAY1_PROT/PTOC1.OpDlTmms.setVal", "SG")
    assert (op_dl.values_by_sg, op_dl.parsed_value) == ({1: "100", 2: "250"}, 100)


def test_setting_group_value_follows_act_sg() -> None:
    text = bcu_text().replace('<SettingControl numOfSGs="2" actSG="1"/>', '<SettingControl numOfSGs="2" actSG="2"/>')
    srv = expand_server(load_scl(text), "BCU1")
    assert srv.attribute("BAY1_PROT/PTOC1.OpDlTmms.setVal", "SG").parsed_value == 250


def test_s_addr_from_dai(bcu: ExpectedServer) -> None:
    assert bcu.attribute("BCU1CTRL/CSWI1.Pos.stVal").s_addr == "1001"
    assert bcu.attribute("BCU1CTRL/XCBR1.Pos.stVal").s_addr == "io:x1.pos"
    assert bcu.attribute("BCU1CTRL/XCBR1.Pos.q").s_addr is None


def test_bad_values_are_warnings() -> None:
    text = bcu_text().replace('<DAI name="stVal"><Val>Ok</Val></DAI>\n            </DOI>\n            <DOI name="NamPlt">\n'
                              '              <DAI name="vendor"><Val>Rackworks</Val></DAI>\n'
                              '              <DAI name="swRev"><Val>2.4.1</Val></DAI>\n'
                              '              <DAI name="d"><Val>Bay 1 control</Val></DAI>',
                              '<DAI name="stVal"><Val>Fine</Val></DAI>\n            </DOI>\n            <DOI name="NamPlt">\n'
                              '              <DAI name="vendor"><Val>Rackworks</Val></DAI>\n'
                              '              <DAI name="swRev"><Val>2.4.1</Val></DAI>\n'
                              '              <DAI name="nope"><Val>x</Val></DAI>', 1)
    text = text.replace('<DAI name="sboTimeout"><Val>20000</Val></DAI>', '<DAI name="sboTimeout"><Val>soon</Val></DAI>')
    srv = expand_server(load_scl(text), "BCU1")
    joined = "\n".join(srv.warnings)
    assert "BCU1CTRL/LLN0.Health.stVal: 'Fine' is not a literal of EnumType 'HealthKind'" in joined
    assert "DAI 'nope': BCU1CTRL/LLN0.NamPlt has no such attribute" in joined
    assert "BCU1CTRL/GGIO1.SPCSO2.sboTimeout: value 'soon' is not a valid INT32U" in joined
    assert srv.attribute("BCU1CTRL/LLN0.Health.stVal").parsed_value is None


def test_unknown_doi_is_a_warning() -> None:
    text = bcu_text().replace('<DOI name="Hz">', '<DOI name="Freq">')
    srv = expand_server(load_scl(text), "BCU1")
    assert any("DOI 'Freq' in BCU1CTRL/MMXU1" in w for w in srv.warnings)


# ---------------------------------------------------------------------------
# Datasets
# ---------------------------------------------------------------------------


def test_datasets(bcu: ExpectedServer) -> None:
    assert [d.dataset_ref for d in bcu.datasets] == [
        DatasetRef("BCU1CTRL", "LLN0", "dsStatus"), DatasetRef("BCU1CTRL", "LLN0", "dsMeas"),
        DatasetRef("BAY1_PROT", "LLN0", "dsProt"), DatasetRef("BAY1_PROT", "LLN0", "dsGoose")]
    ds = bcu.dataset("BCU1CTRL/LLN0.dsMeas")
    assert ds is bcu.dataset("dsMeas")
    assert (ds.ln_name, ds.name, ds.ref, ds.domain) == ("LLN0", "dsMeas", "BCU1CTRL/LLN0.dsMeas", "BCU1CTRL")
    assert ds.resolved
    m = ds.members[1]
    assert (m.ref, m.fc, m.object_ref) == ("BCU1CTRL/MMXU1.PhV.phsA.cVal.mag.f", "MX",
                                           ObjectRef("BCU1CTRL", "MMXU1", ("PhV", "phsA", "cVal", "mag", "f"), "MX"))
    assert m.resolved and m.target is bcu.attribute("BCU1CTRL/MMXU1.PhV.phsA.cVal.mag.f")
    whole_sdo = ds.members[2]
    assert whole_sdo.ref == "BCU1CTRL/MMXU1.PhV.phsB" and isinstance(whole_sdo.target, ExpectedDO)
    prot = bcu.dataset("BAY1_PROT/LLN0.dsProt")
    assert [m.ref for m in prot.members] == ["BAY1_PROT/PTOC1.Str", "BAY1_PROT/PTOC1.Op"]


def test_unresolved_dataset_members(broken_doc: SclDocument) -> None:
    srv = expand_server(broken_doc, "BCU1")
    ds = srv.dataset("BCU1CTRL/LLN0.dsStatus")
    assert not ds.resolved
    bad = [m for m in ds.members if not m.resolved]
    assert [(m.ref, m.fc) for m in bad] == [("BCU1CTRL/XCBR2.Pos.stVal", "ST"), ("BCU1CTRL/GGIO1.SPCSO1.stVal", "MX")]
    assert bad[0].problem == "no logical node 'XCBR2' in BCU1CTRL"
    assert bad[1].problem == "BCU1CTRL/GGIO1.SPCSO1.stVal exists with FC ST, not MX"
    assert bad[0].target is None
    assert sum(m.resolved for m in ds.members) == 6


@pytest.mark.parametrize(
    ("fcda", "problem"),
    [
        ('ldInst="NOPE" lnClass="GGIO" lnInst="1" doName="Ind1" fc="ST"', "no logical device with inst 'NOPE'"),
        ('ldInst="CTRL" lnClass="GGIO" lnInst="1" doName="Ind9" fc="ST"', "BCU1CTRL/GGIO1 has no data object 'Ind9'"),
        ('ldInst="CTRL" lnClass="MMXU" lnInst="1" doName="PhV.phsN" fc="MX"', "has no sub data object 'phsN'"),
        ('ldInst="CTRL" lnClass="MMXU" lnInst="1" doName="TotW" daName="mag.i" fc="MX"', "has no sub-attribute 'i'"),
        ('ldInst="CTRL" lnClass="GGIO" lnInst="1" doName="Ind1" fc="SP"', "has no data with FC SP"),
        ('ldInst="CTRL" lnClass="GGIO" lnInst="1" doName="Ind1" fc="XX"', "unknown functional constraint 'XX'"),
        ('ldInst="CTRL" lnClass="GGIO" lnInst="1" doName="Ind1" fc="ST" ix="0"', "is not an array"),
    ],
)
def test_fcda_resolution_problems(fcda: str, problem: str) -> None:
    text = bcu_text().replace(
        '<FCDA ldInst="CTRL" lnClass="MMXU" lnInst="1" doName="Hz" fc="MX"/>',
        f'<FCDA ldInst="CTRL" lnClass="MMXU" lnInst="1" doName="Hz" fc="MX"/>\n              <FCDA {fcda}/>')
    srv = expand_server(load_scl(text), "BCU1")
    m = srv.dataset("BCU1CTRL/LLN0.dsMeas").members[-1]
    assert not m.resolved
    assert problem in m.problem


# ---------------------------------------------------------------------------
# RCBs and other control blocks
# ---------------------------------------------------------------------------


def test_indexed_rcb_instances_and_client_mapping(bcu: ExpectedServer) -> None:
    inst = bcu.rcbs_of("brcbStatus")
    assert [r.name for r in inst] == ["brcbStatus01", "brcbStatus02", "brcbStatus03"]
    assert [r.index for r in inst] == [1, 2, 3]
    r1, r2, r3 = inst
    assert r1.ref == "BCU1CTRL/LLN0.BR.brcbStatus01"
    assert r1.fc == "BR" and r1.buffered
    assert [c.ied_name for c in r1.clients] == ["SM1"]
    assert [c.ied_name for c in r2.clients] == ["GW1"]
    assert r3.clients == []
    assert bcu.rcb("brcbStatus02") is r2
    assert bcu.rcb("BCU1CTRL/LLN0.BR.brcbStatus02") is r2


def test_rcb_attributes(bcu: ExpectedServer) -> None:
    r = bcu.rcb("brcbStatus01")
    assert r.rpt_id == "BCU1_Status"
    assert r.dat_set_dataset == DatasetRef("BCU1CTRL", "LLN0", "dsStatus")
    assert r.dat_set_ref == "BCU1CTRL/LLN0.dsStatus"
    assert r.dataset is bcu.dataset("BCU1CTRL/LLN0.dsStatus")
    assert (r.conf_rev, r.buf_time, r.intg_pd) == (1, 100, 0)
    assert r.trg_ops == 1 + 2 + 16
    assert r.trg_ops_names == {"dchg", "qchg", "gi"}
    assert r.opt_fields == {"seqNum", "timeStamp", "dataSet", "reasonCode", "entryID", "confRev", "bufOvfl"}
    assert r.opt_fields_int == 1 + 2 + 8 + 4 + 64 + 128 + 32
    assert r.has_owner and r.resv_tms is True


def test_unindexed_and_unbuffered_rcb(bcu: ExpectedServer) -> None:
    (u,) = bcu.rcbs_of("urcbMeas")
    assert (u.name, u.index, u.fc, u.indexed) == ("urcbMeas", None, "RP", False)
    assert u.ref == "BCU1CTRL/LLN0.RP.urcbMeas"
    assert u.trg_ops == 1 + 8 + 16
    # bufOvfl/entryID only apply to BRCBs
    assert u.opt_fields == {"seqNum", "timeStamp", "dataSet", "reasonCode"}
    assert u.opt_fields_int == 15
    assert [c.ln_name for c in u.clients] == ["IHMI1"]


def test_rcb_in_ld_with_ld_name(bcu: ExpectedServer) -> None:
    p1, p2 = bcu.rcbs_of("brcbProt")
    assert p1.ref == "BAY1_PROT/LLN0.BR.brcbProt01"
    assert p1.dat_set_dataset == DatasetRef("BAY1_PROT", "LLN0", "dsProt")
    assert p1.conf_rev == 2
    assert p1.opt_fields_int == 255
    assert [c.ied_name for c in p1.clients] == ["SM1"] and p2.clients == []


def test_ed1_rcbs(ed1: ExpectedServer) -> None:
    assert [r.name for r in ed1.rcbs] == ["rcbEvents01", "rcbEvents02", "brcbAnalogs"]
    b = ed1.rcb("brcbAnalogs")
    assert b.rpt_id is None and b.ref == "TEMPLATELD0/LLN0.BR.brcbAnalogs"
    assert not b.has_owner
    assert b.opt_fields == {"seqNum", "timeStamp", "entryID"}


def test_rcb_with_missing_dataset(broken_doc: SclDocument) -> None:
    srv = expand_server(broken_doc, "BCU1")
    u = srv.rcb("urcbMeas")
    assert u.dat_set_name == "dsMeasOld" and u.dataset is None and not u.dataset_exists
    assert any("references missing dataset 'dsMeasOld'" in w for w in srv.warnings)


def test_more_client_lns_than_instances_is_a_warning() -> None:
    text = bcu_text().replace('<RptEnabled max="3">', '<RptEnabled max="1">')
    srv = expand_server(load_scl(text), "BCU1")
    (r,) = srv.rcbs_of("brcbStatus")
    assert r.name == "brcbStatus"
    assert [c.ied_name for c in r.clients] == ["SM1", "GW1"]
    assert any("share one non-indexed instance" in w for w in srv.warnings)
    text = bcu_text().replace('<RptEnabled max="2">\n                <ClientLN iedName="SM1"',
                              '<RptEnabled max="2">\n                <ClientLN iedName="A1" lnClass="IHMI" lnInst="1"/>\n'
                              '                <ClientLN iedName="A2" lnClass="IHMI" lnInst="1"/>\n'
                              '                <ClientLN iedName="SM1"')
    srv = expand_server(load_scl(text), "BCU1")
    assert [[c.ied_name for c in r.clients] for r in srv.rcbs_of("brcbProt")] == [["A1"], ["A2"]]
    assert any("3 ClientLNs but only 2 instances" in w for w in srv.warnings)


def test_sgcb_and_other_control_blocks(bcu: ExpectedServer) -> None:
    (sgcb,) = bcu.sgcbs
    assert (sgcb.num_of_sgs, sgcb.act_sg, sgcb.domain) == (2, 1, "BAY1_PROT")
    assert sgcb.ref == "BAY1_PROT/LLN0.SGCB"
    assert bcu.ln("BAY1_PROT/LLN0").sgcb is sgcb
    (lcb,) = bcu.lcbs
    assert (lcb.kind, lcb.fc, lcb.ref) == ("LCB", "LG", "BCU1CTRL/LLN0.LG.lcbEvents")
    assert lcb.details["logRef"] == "BCU1CTRL/LLN0$EventLog"
    (gocb,) = bcu.gocbs
    assert (gocb.kind, gocb.ref, gocb.dat_set_name) == ("GoCB", "BAY1_PROT/LLN0.GO.gcbTrip", "dsGoose")
    assert gocb.details["appID"] == "BCU1_PROT_Trip"
    assert gocb.details["address"]["MAC-Address"] == "01-0C-CD-01-00-11"
    assert (gocb.details["minTime"], gocb.details["maxTime"]) == (4, 1000)
    assert bcu.svcbs == []


# ---------------------------------------------------------------------------
# Selection and errors
# ---------------------------------------------------------------------------


def test_mixed_scd_expansion(rack_doc: SclDocument) -> None:
    ied3 = expand_server(rack_doc, "IED3")
    assert ied3.domains == ["IED3LD0"] and ied3.ip == "10.0.0.13"
    assert [r.name for r in ied3.rcbs] == ["urcbA01", "urcbA02"]
    assert ied3.rcb("urcbA01").clients[0].ied_name == "SM1"
    sm1 = expand_server(rack_doc, "SM1")
    assert sm1.domains == ["SM1LD0", "SM1SYS"]
    ds = sm1.dataset("dsSys")
    assert ds.resolved and ds.members[1].ref == "SM1SYS/GGIO1.Ind1.stVal"
    bcu = expand_server(rack_doc, "BCU1")
    assert [c.ln_name for c in bcu.rcb("brcbStatus02").clients] == ["ITCI1"]
    assert all(not s.warnings for s in (ied3, sm1, bcu))


def test_access_point_selection(rack_doc: SclDocument) -> None:
    assert expand_server(rack_doc, "SM1", "S1").ap_name == "S1"
    with pytest.raises(SclError, match="access point 'S2' has no Server"):
        expand_server(rack_doc, "SM1", "S2")
    with pytest.raises(SclError, match="has no access point 'S7'"):
        expand_server(rack_doc, "SM1", "S7")


def test_server_at() -> None:
    text = bcu_text().replace('    </AccessPoint>\n  </IED>',
                              '    </AccessPoint>\n    <AccessPoint name="S2"><ServerAt apName="S1"/></AccessPoint>\n  </IED>')
    doc = load_scl(text)
    srv = expand_server(doc, "BCU1", "S2")
    assert srv.ap_name == "S2" and srv.domains == ["BCU1CTRL", "BAY1_PROT"]


def test_ied_object_is_accepted(bcu_doc: SclDocument) -> None:
    assert expand_server(bcu_doc, bcu_doc.ieds[0]).ied_name == "BCU1"


def test_unknown_ied(bcu_doc: SclDocument) -> None:
    with pytest.raises(SclError, match=r"IED 'BCU9' not found \(IEDs: BCU1\)"):
        expand_server(bcu_doc, "BCU9")


def test_broken_type_reference_raises_with_line() -> None:
    text = bcu_text().replace('<DO name="Hz" type="BCU1_MV"/>', '<DO name="Hz" type="BCU1_MV_missing"/>')
    doc = load_scl(text)
    line = next(i for i, t in enumerate(text.splitlines(), 1) if "BCU1_MV_missing" in t)
    with pytest.raises(SclError) as exc:
        expand_server(doc, "BCU1")
    assert exc.value.line == line
    assert "BCU1CTRL/MMXU1.Hz: unknown DOType 'BCU1_MV_missing'" in str(exc.value)
    assert str(exc.value).startswith(f"<string>:{line}:")
    lenient = expand_server(doc, "BCU1", strict=False)
    assert lenient.do("BCU1CTRL/MMXU1.Hz") is None
    assert lenient.do("BCU1CTRL/MMXU1.TotW") is not None
    assert any("unknown DOType" in w for w in lenient.warnings)
    # the dataset member that pointed at it no longer resolves
    assert not lenient.dataset("dsMeas").resolved


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ('lnType="BCU1_GGIO"', 'lnType="BCU1_GGIO_X"', "unknown LNodeType 'BCU1_GGIO_X'"),
        ('type="BCU1_Originator" fc="ST"/>', 'type="BCU1_Orig_X" fc="ST"/>', "unknown DAType 'BCU1_Orig_X'"),
        ('type="HealthKind" fc="ST"', 'type="HealthKind_X" fc="ST"', "unknown EnumType 'HealthKind_X'"),
    ],
)
def test_other_broken_references(old: str, new: str, message: str) -> None:
    doc = load_scl(bcu_text().replace(old, new, 1))
    with pytest.raises(SclError, match=message):
        expand_server(doc, "BCU1")
    assert expand_server(doc, "BCU1", strict=False).warnings


def test_array_attributes() -> None:
    text = bcu_text().replace(
        '<DA name="db" bType="INT32U" fc="CF" dchg="true"><Val>500</Val></DA>',
        '<DA name="db" bType="INT32U" fc="CF" dchg="true"><Val>500</Val></DA>\n'
        '      <DA name="hist" bType="FLOAT32" fc="MX" count="4"/>')
    text = text.replace('<DAI name="db"><Val>1000</Val></DAI>',
                        '<DAI name="db"><Val>1000</Val></DAI>\n              <DAI name="hist" ix="2"><Val>7.5</Val></DAI>')
    srv = expand_server(load_scl(text), "BCU1")
    a = srv.attribute("BCU1CTRL/MMXU1.TotW.hist")
    assert a.count == 4 and a.value_type == ValueType("array", 4, ValueType("float", 32))
    assert a.element_values == {2: "7.5"}
