"""to_libiec61850_config(): text format, and proof that libiec61850 v1.6.1 loads and serves it."""

from __future__ import annotations

from pathlib import Path

import pytest

from mms_client.codes import FCS
from mms_client.scl import ExpectedServer, SclDocument, expand_server, load_scl, to_libiec61850_config

from ._data import FIXTURES, bcu_text

lib = pytest.importorskip("tests.unit.scl.libiec_harness", reason="bundled libiec61850 not loadable")


@pytest.fixture(scope="module")
def bcu_config(bcu: ExpectedServer) -> str:
    warnings: list[str] = []
    text = to_libiec61850_config(bcu, warnings=warnings)
    assert warnings == []
    return text


@pytest.fixture(scope="module")
def bcu_config_file(bcu_config: str, tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("cfg") / "bcu_ed2.cfg"
    path.write_text(bcu_config)
    return path


# ---------------------------------------------------------------------------
# Text format
# ---------------------------------------------------------------------------


def test_layout(bcu_config: str) -> None:
    lines = bcu_config.split("\n")
    assert lines[0] == "MODEL(BCU1){"
    assert bcu_config.endswith("}\n")
    assert "LD(CTRL){" in lines
    assert "LD(PROT BAY1_PROT){" in lines
    assert all(line == line.strip() for line in lines)  # no indentation, no trailing blanks
    assert bcu_config.count("{") == bcu_config.count("}")


def _block(text: str, header: str) -> list[str]:
    lines = text.split("\n")
    i = lines.index(header)
    depth, out = 0, []
    for line in lines[i:]:
        out.append(line)
        depth += line.endswith("{") - (line == "}")
        if depth == 0:
            return out
    raise AssertionError(header)


def test_basic_and_constructed_das(bcu_config: str) -> None:
    mod = _block(bcu_config, "DO(Mod 0){")
    assert mod == ["DO(Mod 0){", "DA(stVal 0 12 0 1 0)=1;", "DA(q 0 23 0 2 0);", "DA(t 0 22 0 0 0);",
                   "DA(ctlModel 0 12 4 1 0)=0;", "}"]
    lln0 = _block(bcu_config, "LN(LLN0){")
    assert 'DA(ldNs 0 20 11 0 0)="IEC 61850-7-4:2007B";' in lln0
    cswi = _block(bcu_config, "LN(CSWI1){")
    oper = cswi.index(f"DA(Oper 0 27 {FCS['CO']} 0 0){{")
    assert cswi[oper + 1:oper + 9] == [
        "DA(ctlVal 0 0 12 0 0);",
        "DA(origin 0 27 12 0 0){",
        "DA(orCat 0 12 12 0 0);",
        "DA(orIdent 0 13 12 0 0);",
        "}",
        "DA(ctlNum 0 6 12 0 0);",
        "DA(T 0 22 12 0 0);",
        "DA(Test 0 0 12 0 0);",
    ]
    assert "DA(stVal 0 25 0 1 1001);" in cswi  # Dbpos -> CODEDENUM, numeric sAddr kept
    assert "DA(ctlModel 0 12 4 1 0)=4;" in cswi
    assert "DA(sboTimeout 0 9 4 1 0)=30000;" in cswi
    xcbr = _block(bcu_config, "LN(XCBR1){")
    assert "DA(stVal 0 25 0 1 0);" in xcbr  # non-numeric sAddr -> 0
    ggio = _block(bcu_config, "LN(GGIO1){")
    assert "DA(SBO 0 19 12 0 0);" in ggio


def test_sdo_nesting_and_values(bcu_config: str) -> None:
    phv = _block(bcu_config, "DO(PhV 0){")
    assert phv[1] == "DO(phsA 0){"
    assert "DA(SIUnit 0 12 4 1 0)=29;" in phv
    assert "DA(multiplier 0 12 4 1 0)=3;" in phv
    mmxu = _block(bcu_config, "LN(MMXU1){")
    assert "DA(db 0 9 4 1 0)=1000;" in mmxu
    assert "DA(f 0 10 1 5 0);" in mmxu  # BDA inherits dchg|dupd from mag


def test_setting_groups_and_transient(bcu_config: str) -> None:
    prot_lln0 = _block(bcu_config, "LD(PROT BAY1_PROT){")
    assert prot_lln0[1:3] == ["LN(LLN0){", "SG(1 2)"]
    str_val = _block(bcu_config, "DO(StrVal 0){")
    assert str_val[1:7] == ["DA(setMag 0 27 6 1 0){", "DA(f 0 10 6 1 0)=1.2;", "}",
                            "DA(setMag 0 27 7 1 0){", "DA(f 0 10 7 1 0)=1.2;", "}"]
    assert "DA(minVal 0 27 4 1 0){" in str_val
    assert "DA(f 0 10 4 1 0)=0.1;" in str_val
    op = _block(bcu_config, "DO(Op 0){")
    assert op[1:4] == ["DA(general 0 0 0 129 0);", "DA(q 0 23 0 130 0);", "DA(t 0 22 0 128 0);"]
    assert "DA(setVal 0 3 6 1 0)=100;" in bcu_config


def test_datasets_rcbs_and_other_blocks(bcu_config: str) -> None:
    assert _block(bcu_config, "DS(dsMeas){") == [
        "DS(dsMeas){",
        "DE(MMXU1$MX$TotW$mag$f);",
        "DE(MMXU1$MX$PhV$phsA$cVal$mag$f);",
        "DE(MMXU1$MX$PhV$phsB);",
        "DE(MMXU1$MX$Hz);",
        "}",
    ]
    lines = bcu_config.split("\n")
    # trgOps dchg|qchg|gi = 19, +64 for Services/ReportSettings@owner; optFlds 239
    assert [ln for ln in lines if ln.startswith("RC(")] == [
        "RC(brcbStatus01 BCU1_Status 1 dsStatus 1 83 239 100 0);",
        "RC(brcbStatus02 BCU1_Status 1 dsStatus 1 83 239 100 0);",
        "RC(brcbStatus03 BCU1_Status 1 dsStatus 1 83 239 100 0);",
        "RC(urcbMeas BCU1_Meas 0 dsMeas 1 89 15 0 5000);",
        "RC(brcbProt01 BCU1_Prot 1 dsProt 2 83 255 50 0);",
        "RC(brcbProt02 BCU1_Prot 1 dsProt 2 83 255 50 0);",
    ]
    assert "LC(lcbEvents dsStatus BCU1CTRL/LLN0$EventLog 19 0 1 1);" in lines
    assert "LOG(EventLog);" in lines
    assert "GC(gcbTrip BCU1_PROT_Trip dsGoose 1 0 4 1000);" in lines


def test_ed1_without_owner_and_rpt_id(ed1: ExpectedServer) -> None:
    text = to_libiec61850_config(ed1)
    assert "LD(LD0){" in text
    assert "RC(rcbEvents01 TEMPLATE_Events 0 Events 1 19 15 0 0);" in text
    assert "RC(brcbAnalogs - 1 Analogs 1 25 67 200 10000);" in text


def test_cross_ld_members(rack_doc: SclDocument) -> None:
    text = to_libiec61850_config(expand_server(rack_doc, "SM1"))
    assert _block(text, "DS(dsSys){") == [
        "DS(dsSys){", "DE(LPHD1$ST$PhyHealth);", "DE(SYS/GGIO1$ST$Ind1$stVal);", "DE(SYS/GGIO1$ST$Ind1$q);", "}"]


def test_cross_ld_member_with_ld_name_is_left_out() -> None:
    text = bcu_text().replace(
        '<FCDA ldInst="CTRL" lnClass="LLN0" doName="LocKey" daName="stVal" fc="ST"/>',
        '<FCDA ldInst="CTRL" lnClass="LLN0" doName="LocKey" daName="stVal" fc="ST"/>\n'
        '              <FCDA ldInst="PROT" lnClass="PTOC" lnInst="1" doName="Op" daName="general" fc="ST"/>')
    srv = expand_server(load_scl(text), "BCU1")
    assert srv.dataset("dsStatus").members[-1].resolved  # the model itself is fine
    warnings: list[str] = []
    cfg = to_libiec61850_config(srv, warnings=warnings)
    assert "PTOC1$ST$Op$general" not in _block(cfg, "DS(dsStatus){")[-2]
    assert any("cross-LD member BAY1_PROT/PTOC1.Op.general" in w for w in warnings)


def test_broken_refs_export(broken_doc: SclDocument) -> None:
    warnings: list[str] = []
    text = to_libiec61850_config(expand_server(broken_doc, "BCU1"), warnings=warnings)
    assert "RC(urcbMeas BCU1_Meas 0 dsMeasOld 1 89 15 0 5000);" in text  # defect reproduced
    assert "XCBR2" not in text
    assert len(warnings) == 2 and all("does not resolve; left out" in w for w in warnings)


def test_string_values_are_sanitised() -> None:
    text = bcu_text().replace("<Val>Bay 1 control</Val>", '<Val>Bay "1"\n control</Val>')
    cfg = to_libiec61850_config(expand_server(load_scl(text), "BCU1"))
    assert "DA(d 0 20 5 0 0)=\"Bay '1'  control\";" in cfg.split("\n")


def test_rpt_id_with_spaces_becomes_a_token() -> None:
    text = bcu_text().replace('rptID="BCU1_Meas"', 'rptID="BCU1 Meas"')
    cfg = to_libiec61850_config(expand_server(load_scl(text), "BCU1"))
    assert "RC(urcbMeas BCU1_Meas 0 dsMeas" in cfg


# ---------------------------------------------------------------------------
# libiec61850 accepts it
# ---------------------------------------------------------------------------


def test_bcu_config_loads_in_libiec61850(bcu_config_file: Path) -> None:
    model = lib.load_model(bcu_config_file)
    assert model, "libiec61850 rejected the generated config"
    try:
        for ref in [
            "BCU1CTRL/CSWI1.Pos.stVal",
            "BCU1CTRL/MMXU1.PhV.phsA.cVal.mag.f",
            "BCU1CTRL/CSWI1.Pos.Oper.origin.orCat",
            "BCU1CTRL/CSWI1.Pos.SBOw.Check",
            "BCU1CTRL/GGIO1.SPCSO2.SBO",
            "BCU1CTRL/GGIO1.IntIn1.Oper.ctlVal",
            "BCU1CTRL/LPHD1.PhyNam.serNum",
            "BAY1_PROT/PTOC1.StrVal.setMag.f",
            "BAY1_PROT/PTOC1.OpDlTmms.setVal",
            "BAY1_PROT/LLN0.NamPlt.ldNs",
        ]:
            assert lib.has_node(model, ref), ref
        assert not lib.has_node(model, "BCU1CTRL/CSWI1.Pos.nope")
        assert not lib.has_node(model, "BCU1PROT/PTOC1.Str.general")  # domain is the ldName
    finally:
        lib.lib.IedModel_destroy(model)


@pytest.mark.parametrize(
    ("fixture", "ied"),
    [("rack_mixed.scd", "SM1"), ("rack_mixed.scd", "BCU1"), ("rack_mixed.scd", "IED3"),
     ("ied_ed1.icd", "TEMPLATE"), ("broken_refs.cid", "BCU1")],
)
def test_every_fixture_loads(fixture: str, ied: str, tmp_path: Path) -> None:
    path = tmp_path / "model.cfg"
    path.write_text(to_libiec61850_config(expand_server(load_scl(FIXTURES / fixture), ied)))
    model = lib.load_model(path)
    assert model
    lib.lib.IedModel_destroy(model)


def test_array_attributes_load(tmp_path: Path) -> None:
    text = bcu_text().replace(
        '<DA name="db" bType="INT32U" fc="CF" dchg="true"><Val>500</Val></DA>',
        '<DA name="db" bType="INT32U" fc="CF" dchg="true"><Val>500</Val></DA>\n'
        '      <DA name="hist" bType="FLOAT32" fc="MX" count="3"/>\n'
        '      <DA name="pts" bType="Struct" type="BCU1_Vector" fc="MX" count="2"/>')
    text = text.replace('<DAI name="db"><Val>1000</Val></DAI>',
                        '<DAI name="db"><Val>1000</Val></DAI>\n              <DAI name="hist" ix="1"><Val>7.5</Val></DAI>')
    cfg = to_libiec61850_config(expand_server(load_scl(text), "BCU1"))
    assert "DA(hist 3 10 1 0 0){\n[0];\n[1]=7.5;\n[2];\n}\n" in cfg
    assert "DA(pts 2 27 1 0 0){\n[0]{\nDA(mag 0 27 1 0 0){" in cfg
    path = tmp_path / "arrays.cfg"
    path.write_text(cfg)
    model = lib.load_model(path)
    assert model
    try:
        assert lib.has_node(model, "BCU1CTRL/MMXU1.TotW.hist")
        assert lib.has_node(model, "BCU1CTRL/MMXU1.TotW.pts")
        assert lib.has_node(model, "BCU1CTRL/MMXU1.TotW.units.SIUnit")  # parsing continued after arrays
    finally:
        lib.lib.IedModel_destroy(model)


# ---------------------------------------------------------------------------
# A live server built from the export behaves like the SCL says
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def live_client(bcu_config_file: Path):
    with lib.running_server(bcu_config_file) as port:
        client = lib.Client(port)
        try:
            yield client
        finally:
            client.close()


def test_live_domains(live_client, bcu: ExpectedServer) -> None:
    assert sorted(live_client.logical_devices()) == sorted(bcu.domains)


@pytest.mark.parametrize(
    ("ref", "fc"),
    [
        ("BCU1CTRL/CSWI1.Pos.ctlModel", "CF"),
        ("BCU1CTRL/CSWI1.Pos.sboTimeout", "CF"),
        ("BCU1CTRL/LLN0.NamPlt.ldNs", "EX"),
        ("BCU1CTRL/LPHD1.PhyNam.serNum", "DC"),
        ("BCU1CTRL/MMXU1.TotW.units.multiplier", "CF"),
        ("BCU1CTRL/GGIO1.IntIn1.maxVal", "CF"),
        ("BAY1_PROT/PTOC1.OpDlTmms.setVal", "SG"),
        ("BAY1_PROT/PTOC1.StrVal.setMag.f", "SG"),
    ],
)
def test_live_values_match_scl(live_client, bcu: ExpectedServer, ref: str, fc: str) -> None:
    expected = bcu.attribute(ref, fc).parsed_value
    got = live_client.read(ref, FCS[fc])
    if isinstance(expected, float):
        assert got == pytest.approx(expected)
    else:
        assert got == expected


def test_live_datasets_match_scl(live_client, bcu: ExpectedServer) -> None:
    for ds in bcu.datasets:
        got = live_client.dataset_directory(ds.ref)
        assert got == [f"{m.ref}[{m.fc}]" for m in ds.members]


def test_live_rcbs_match_scl(live_client, bcu: ExpectedServer) -> None:
    for rcb in bcu.rcbs:
        got = live_client.rcb(rcb.ref)
        assert got["rptID"] == rcb.effective_rpt_id
        assert got["datSet"] == rcb.dat_set
        assert got["confRev"] == rcb.conf_rev
        assert got["trgOps"] == rcb.trg_ops
        assert got["optFlds"] == rcb.opt_fields_int
        assert (got["bufTm"], got["intgPd"], got["buffered"]) == (rcb.buf_time, rcb.intg_pd, rcb.buffered)


def test_live_sgcb(live_client, bcu: ExpectedServer) -> None:
    (sgcb,) = bcu.sgcbs
    assert live_client.read(f"{sgcb.domain}/LLN0.SGCB.NumOfSG", FCS["SP"]) == sgcb.num_of_sgs
    assert live_client.read(f"{sgcb.domain}/LLN0.SGCB.ActSG", FCS["SP"]) == sgcb.act_sg
