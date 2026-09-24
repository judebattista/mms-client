"""load_scl(): document structure, robustness and error reporting."""

from __future__ import annotations

import pytest

from mms_client.scl import SclDocument, SclError, load_scl

from ._data import BCU_ED2, bcu_text


def test_header_and_version(bcu_doc: SclDocument) -> None:
    assert bcu_doc.scl_version == "2007B"
    assert (bcu_doc.version, bcu_doc.revision, bcu_doc.release) == ("2007", "B", None)
    assert bcu_doc.header.id == "BCU1"
    assert bcu_doc.header.version == "1"
    assert bcu_doc.header.revision == "3"
    assert bcu_doc.header.tool_id == "mms-client test fixture"
    assert bcu_doc.source == str(BCU_ED2)
    assert bcu_doc.warnings == []


def test_versions_of_all_fixtures(rack_doc: SclDocument, ed1_doc: SclDocument) -> None:
    assert rack_doc.scl_version == "2007B4"
    assert rack_doc.release == "4"
    assert ed1_doc.scl_version == "2003"
    assert ed1_doc.version is None


def test_ieds_listing(rack_doc: SclDocument) -> None:
    assert rack_doc.ied_names == ["SM1", "BCU1", "IED3"]
    sm1 = rack_doc.ied("SM1")
    assert sm1 is not None
    assert (sm1.manufacturer, sm1.type, sm1.config_version) == ("Stationware", "SM-500", "5.1")
    assert sm1.original_scl == "2007B4"
    assert rack_doc.ied("IED3").original_scl == "2003"
    assert rack_doc.ied("BCU1").original_scl == "2007B"
    assert rack_doc.ied("nope") is None


def test_ied_structure(bcu_doc: SclDocument) -> None:
    ied = bcu_doc.ied("BCU1")
    assert ied.desc == "Bay 1 control unit"
    ap = ied.access_point()
    assert ap.name == "S1"
    assert ap.server.timeout == 30
    ctrl, prot = ap.server.ldevices
    assert (ctrl.inst, ctrl.ld_name) == ("CTRL", None)
    assert (prot.inst, prot.ld_name, prot.desc) == ("PROT", "BAY1_PROT", "Bay protection")
    assert [ln.name for ln in ctrl.all_lns] == ["LLN0", "LPHD1", "CSWI1", "XCBR1", "GGIO1", "MMXU1"]
    cswi = ctrl.lns[1]
    assert (cswi.prefix, cswi.ln_class, cswi.inst, cswi.ln_type) == ("", "CSWI", "1", "BCU1_CSWI")
    assert ctrl.ln0.logs == ["EventLog"]


def test_services(bcu_doc: SclDocument, ed1_doc: SclDocument) -> None:
    svc = bcu_doc.ied("BCU1").services
    rs = svc.report_settings
    assert rs.owner is True and rs.resv_tms is True
    assert (rs.cb_name, rs.dat_set, rs.intg_pd) == ("Conf", "Dyn", "Dyn")
    assert svc.conf_report_control.max == 12
    assert svc.conf_report_control.buf_mode == "both"
    sg = svc.setting_groups
    assert sg.sg_edit and sg.conf_sg and sg.sg_edit_resv_tms is True
    assert svc.has("GetDirectory") and svc.has("ConfLNs")
    assert svc.raw["ConfLNs"] == {"fixPrefix": "true", "fixLnInst": "true"}
    assert svc.raw["SettingGroups/SGEdit"] == {"resvTms": "true"}
    ed1_rs = ed1_doc.ied("TEMPLATE").services.report_settings
    assert ed1_rs.owner is None and ed1_rs.resv_tms is None


def test_communication(bcu_doc: SclDocument, rack_doc: SclDocument) -> None:
    cap = bcu_doc.connected_ap("BCU1")
    assert cap.ip == "10.0.0.11"
    assert cap.ip_subnet == "255.255.255.0"
    assert cap.ip_gateway == "10.0.0.1"
    assert cap.address["OSI-AP-Title"] == "1,3,9999,33"
    assert cap.address["OSI-AE-Qualifier"] == "33"
    assert cap.address["OSI-PSEL"] == "00000001"
    assert (cap.subnetwork, cap.subnetwork_type) == ("StationBus", "8-MMS")
    (gse,) = cap.gse
    assert (gse.ld_inst, gse.cb_name, gse.min_time, gse.max_time) == ("PROT", "gcbTrip", 4, 1000)
    assert gse.address["APPID"] == "0011"
    assert [c.ied_name for c in rack_doc.connected_aps()] == ["SM1", "BCU1", "IED3", "SM1"]


def test_connected_ap_lookup(rack_doc: SclDocument, ed1_doc: SclDocument) -> None:
    # SM1 has two access points; the default is the one carrying the MMS server.
    assert rack_doc.connected_ap("SM1").ip == "10.0.0.2"
    assert rack_doc.connected_ap("SM1", "S2").ip == "192.168.50.2"
    assert rack_doc.connected_ap("SM1", "S9") is None
    assert rack_doc.connected_ap("IED3").ip == "10.0.0.13"
    assert rack_doc.connected_ap("GW1") is None
    assert ed1_doc.connected_ap("TEMPLATE") is None


def test_datasets_and_control_blocks(bcu_doc: SclDocument) -> None:
    ln0 = bcu_doc.ied("BCU1").access_point().server.ldevices[0].ln0
    ds = ln0.datasets[1]
    assert ds.name == "dsMeas"
    f = ds.fcdas[1]
    assert (f.ld_inst, f.ln_class, f.ln_inst, f.do_name, f.da_name, f.fc) == (
        "CTRL", "MMXU", "1", "PhV.phsA", "cVal.mag.f", "MX")
    brcb, urcb = ln0.report_controls
    assert brcb.buffered and brcb.indexed and brcb.rpt_enabled_max == 3
    assert brcb.trg_ops == {"dchg", "qchg", "gi"}
    assert brcb.opt_fields == {"seqNum", "timeStamp", "dataSet", "reasonCode", "entryID", "configRef", "bufOvfl"}
    assert [c.ied_name for c in brcb.client_lns] == ["SM1", "GW1"]
    assert brcb.client_lns[0].ln_name == "IHMI1"
    assert brcb.client_lns[0].ap_ref == "S1"
    assert brcb.client_lns[0].label == "SM1/S1:LD0/IHMI1"
    assert not urcb.buffered and not urcb.indexed
    assert urcb.intg_pd == 5000 and urcb.buf_time == 0
    # gi defaults to true when not given; bufOvfl defaults to true in 2007 documents
    assert urcb.trg_ops == {"dchg", "period", "gi"}
    assert "bufOvfl" in urcb.opt_fields
    (lcb,) = ln0.log_controls
    assert (lcb.name, lcb.dat_set, lcb.log_name, lcb.log_ena) == ("lcbEvents", "dsStatus", "EventLog", True)
    prot_ln0 = bcu_doc.ied("BCU1").access_point().server.ldevices[1].ln0
    sc = prot_ln0.setting_control
    assert (sc.num_of_sgs, sc.act_sg) == (2, 1)
    (gc,) = prot_ln0.gse_controls
    assert (gc.name, gc.dat_set, gc.app_id, gc.conf_rev) == ("gcbTrip", "dsGoose", "BCU1_PROT_Trip", 1)


def test_ed1_schema_defaults(ed1_doc: SclDocument) -> None:
    ln0 = ed1_doc.ied("TEMPLATE").access_point().server.ldevices[0].ln0
    urcb, brcb = ln0.report_controls
    assert urcb.rpt_id == "TEMPLATE_Events" and urcb.rpt_enabled_max == 2 and urcb.client_lns == []
    assert brcb.rpt_id is None and not brcb.has_rpt_enabled
    # 2003 schema: bufOvfl defaults to false
    assert brcb.opt_fields == {"seqNum", "timeStamp", "entryID"}


def test_instance_values_parsed(bcu_doc: SclDocument) -> None:
    ld = bcu_doc.ied("BCU1").access_point().server.ldevices[1]
    ptoc = ld.lns[1]
    doi = ptoc.dois[0]
    assert doi.name == "StrVal"
    sdi = doi.sdis[0]
    assert sdi.name == "setMag"
    dai = sdi.dais[0]
    assert [(v.text, v.sgroup) for v in dai.vals] == [("1.2", 1), ("1.5", 2)]
    cswi = bcu_doc.ied("BCU1").access_point().server.ldevices[0].lns[1]
    assert cswi.dois[0].dais[0].s_addr == "1001"


def test_templates(bcu_doc: SclDocument) -> None:
    t = bcu_doc.templates
    lt = t.lnode_types["BCU1_PTOC"]
    assert lt.ln_class == "PTOC"
    assert [(d.name, d.transient) for d in lt.dos][2] == ("Op", True)
    wye = t.do_types["BCU1_WYE"]
    assert wye.cdc == "WYE" and [s.name for s in wye.sdos] == ["phsA", "phsB", "phsC"]
    dpc = t.do_types["BCU1_DPC_SBOes"]
    ctl = next(d for d in dpc.das if d.name == "ctlModel")
    assert (ctl.b_type, ctl.type, ctl.fc, ctl.dchg, [v.text for v in ctl.vals]) == (
        "Enum", "CtlModelKind", "CF", True, ["status-only"])
    oper = t.da_types["BCU1_Oper_BOOLEAN"]
    assert [b.name for b in oper.bdas] == ["ctlVal", "origin", "ctlNum", "T", "Test", "Check"]
    assert all(b.is_bda and b.fc is None for b in oper.bdas)
    mult = t.enum_types["MultiplierKind"]
    assert mult.values == {-6: "µ", -3: "m", 0: "", 3: "k", 6: "M"}
    assert mult.ord_of("k") == 3 and mult.ord_of("") == 0 and mult.ord_of("x") is None


def test_private_and_foreign_elements_are_ignored(bcu_doc: SclDocument) -> None:
    # The fixture has a Private block, a Text element and an element in a private namespace.
    ggio = bcu_doc.ied("BCU1").access_point().server.ldevices[0].lns[3]
    assert ggio.name == "GGIO1"
    assert [d.name for d in ggio.dois] == ["SPCSO1", "SPCSO2", "IntIn1"]
    assert bcu_doc.warnings == []


def test_bytes_str_bom_and_comments() -> None:
    data = BCU_ED2.read_bytes()
    doc = load_scl(b"\xef\xbb\xbf" + data)
    assert doc.source == "<bytes>"
    assert doc.ied_names == ["BCU1"]
    doc2 = load_scl(bcu_text())
    assert doc2.source == "<string>" and doc2.ied_names == ["BCU1"]


def test_utf16_file() -> None:
    text = bcu_text().replace('encoding="UTF-8"', 'encoding="UTF-16"')
    doc = load_scl(text.encode("utf-16"))
    assert doc.ied_names == ["BCU1"]
    assert doc.templates.enum_types["MultiplierKind"].values[-6] == "µ"


def test_windows_1252_fallback() -> None:
    text = bcu_text().replace("Bay 1 control unit", "Bay 1 contrôle")
    doc = load_scl(text.encode("cp1252"))
    assert doc.ied("BCU1").desc == "Bay 1 contrôle"
    assert any("Windows-1252" in w for w in doc.warnings)


def test_no_namespace_is_accepted_with_warning() -> None:
    text = bcu_text().replace('xmlns="http://www.iec.ch/61850/2003/SCL" ', "")
    doc = load_scl(text)
    assert doc.ied_names == ["BCU1"]
    assert any("no namespace" in w for w in doc.warnings)


def test_malformed_xml_reports_line(tmp_path) -> None:
    bad = tmp_path / "bad.cid"
    lines = bcu_text().splitlines()
    lines[9] = lines[9] + "<oops"
    bad.write_text("\n".join(lines), encoding="utf-8")
    with pytest.raises(SclError) as exc:
        load_scl(bad)
    assert exc.value.line in (10, 11)
    assert exc.value.source == str(bad)
    assert str(exc.value).startswith(f"{bad}:")


def test_not_scl() -> None:
    with pytest.raises(SclError, match="not SCL"):
        load_scl(b'<?xml version="1.0"?><root xmlns="urn:x"/>')
    with pytest.raises(SclError, match="not <SCL>"):
        load_scl(b"<Foo/>")


def test_missing_file(tmp_path) -> None:
    with pytest.raises(SclError, match="cannot read"):
        load_scl(tmp_path / "missing.scd")


def test_odd_content_becomes_warnings() -> None:
    text = bcu_text()
    text = text.replace('bufTime="100" intgPd="0"', 'bufTime="fast" intgPd="0"')
    text = text.replace('<ReportControl name="brcbProt" rptID="BCU1_Prot" datSet="dsProt" confRev="2"',
                        '<ReportControl name="brcbProt" rptID="BCU1_Prot" datSet="dsProt"')
    text = text.replace("<EnumType id=\"SboClassKind\">",
                        '<EnumType id="HealthKind"><EnumVal ord="9">dup</EnumVal></EnumType>\n'
                        '    <EnumType id="SboClassKind">')
    doc = load_scl(text)
    joined = "\n".join(doc.warnings)
    assert "bufTime='fast' is not an integer" in joined
    assert "'brcbProt' has no confRev" in joined
    assert "duplicate <EnumType> id 'HealthKind'" in joined
    # first definition kept
    assert doc.templates.enum_types["HealthKind"].values == {1: "Ok", 2: "Warning", 3: "Alarm"}
    # every warning carries file:line
    assert all(w.startswith("<string>:") for w in doc.warnings)


def test_dangling_type_reference_is_a_warning_at_load_time() -> None:
    text = bcu_text().replace('<DO name="Hz" type="BCU1_MV"/>', '<DO name="Hz" type="BCU1_MV_missing"/>')
    doc = load_scl(text)
    (w,) = doc.warnings
    assert "unknown DOType 'BCU1_MV_missing'" in w
    line = next(i for i, t in enumerate(text.splitlines(), 1) if "BCU1_MV_missing" in t)
    assert w.startswith(f"<string>:{line}:")
