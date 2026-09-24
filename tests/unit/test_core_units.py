"""Unit tests for core pieces that need no network."""

from __future__ import annotations

import json

import pytest
import yaml

from mms_client import codes
from mms_client.adapter import BitString, MmsKind, UtcTime, VarSpec, value_from_json, value_to_json
from mms_client.adapter.codec import decode, encode, parse_text
from mms_client.adapter.errors import EncodeError
from mms_client.core.identity import Edition, edition_from_ldns
from mms_client.core.model import DeviceModel, LogicalDeviceInfo, build_ln
from mms_client.core.refs import ObjectRef, RefError, parse_mms, parse_ref, parse_shell_path
from mms_client.core.results import Category, CheckReport, CheckResult, Status, envelope
from mms_client.core.safety import (
    ConfirmationDeclined,
    NonInteractive,
    Policy,
    PolicyError,
    SafetyProfile,
    Scripted,
)
from mms_client.core.session import owner_ip, resolve_target
from mms_client.core.sessionlog import SessionLog, read_log, restorable_writes
from mms_client.inventory import InventoryError, parse_inventory
from mms_client.verify.diff import IgnoreRules, diff_snapshots
from mms_client.verify.snapshot import Snapshot


# ------------------------------------------------------------------------- refs
@pytest.mark.parametrize(
    "text,cwd,expected",
    [
        ("CTRL/CSWI1.Pos.stVal", (), ("CTRL", "CSWI1", ("Pos", "stVal"), None)),
        ("CTRL/CSWI1$ST$Pos$stVal", (), ("CTRL", "CSWI1", ("Pos", "stVal"), "ST")),
        ("Pos.stVal [ST]", ("CTRL", "CSWI1"), ("CTRL", "CSWI1", ("Pos", "stVal"), "ST")),
        ("../XCBR1.Pos", ("CTRL", "CSWI1"), ("CTRL", "XCBR1", ("Pos",), None)),
        ("/CTRL/CSWI1/Pos/stVal", ("X",), ("CTRL", "CSWI1", ("Pos", "stVal"), None)),
        ("CTRL", (), ("CTRL", None, (), None)),
    ],
)
def test_parse_ref(text, cwd, expected):
    r = parse_ref(text, cwd)
    assert (r.ld, r.ln, r.path, r.fc) == expected


def test_parse_ref_known_lds_and_errors():
    assert parse_ref("PROT/PTOC1", ("CTRL", "CSWI1"), known_lds={"CTRL", "PROT"}).ld == "PROT"
    assert parse_ref("Pos/stVal", ("CTRL", "CSWI1"), known_lds={"CTRL", "PROT"}).path == ("Pos", "stVal")
    with pytest.raises(RefError):
        parse_ref("A/B.C [XX]")
    with pytest.raises(RefError):
        parse_ref("A/B$ST$C", fc="MX")
    with pytest.raises(RefError):
        parse_ref("/")
    assert parse_shell_path("..", ("A", "B")) == ("A",)
    assert parse_mms("LD/LLN0$BR$brcb01") == ObjectRef("LD", "LLN0", ("brcb01",), "BR")
    assert ObjectRef("LD", "LN", ("DO", "DA"), "ST").mms_item() == "LN$ST$DO$DA"


# ------------------------------------------------------------------------- codec (needs the native lib, no network)
def roundtrip(spec: VarSpec, value):
    from mms_client.adapter._native import lib

    p = encode(spec, value)
    try:
        return decode(p, spec)
    finally:
        lib().MmsValue_delete(p)


def test_codec_roundtrips():
    s = VarSpec(MmsKind.STRUCTURE, children=(VarSpec(MmsKind.BOOLEAN, "b"), VarSpec(MmsKind.INTEGER, "i", 32),
                                             VarSpec(MmsKind.FLOAT, "f", 32), VarSpec(MmsKind.VISIBLE_STRING, "s", 64),
                                             VarSpec(MmsKind.BIT_STRING, "q", 13), VarSpec(MmsKind.OCTET_STRING, "o", 8),
                                             VarSpec(MmsKind.UNSIGNED, "u", 32), VarSpec(MmsKind.UTC_TIME, "t")))
    v = {"b": True, "i": -5, "f": 0.1, "s": "abc", "q": BitString.from_text("0100000000000"), "o": b"\x01\x02",
         "u": 7, "t": UtcTime(1_700_000_000_123, 0, 0x0A)}
    assert roundtrip(s, v) == v
    assert roundtrip(VarSpec(MmsKind.ARRAY, size=3, element=VarSpec(MmsKind.INTEGER, size=8)), [1, 2, 3]) == [1, 2, 3]


def test_codec_rejects_wrong_types():
    with pytest.raises(EncodeError):
        encode(VarSpec(MmsKind.BOOLEAN), 1)
    with pytest.raises(EncodeError):
        encode(VarSpec(MmsKind.INTEGER, size=8), 200)
    with pytest.raises(EncodeError):
        encode(VarSpec(MmsKind.VISIBLE_STRING, size=3), "toolong")
    with pytest.raises(EncodeError):
        encode(VarSpec(MmsKind.BIT_STRING, size=2), BitString.from_text("101"))
    with pytest.raises(EncodeError):
        parse_text(VarSpec(MmsKind.UNSIGNED, size=8), "-1")
    assert parse_text(VarSpec(MmsKind.OCTET_STRING, size=-8), "01:02") == b"\x01\x02"


def test_value_json_roundtrip():
    v = {"a": BitString.from_text("01"), "b": UtcTime(5, 1, 2), "c": b"\xff", "d": [1.5, "x", None]}
    assert value_from_json(json.loads(json.dumps(value_to_json(v)))) == v


def test_bitstring_int_views():
    b = BitString.from_text("10")
    assert b.as_int_msb0() == 2 and b.as_int_lsb0() == 1  # Dbpos "on" / Quality bit 0


# ------------------------------------------------------------------------- model
def _model() -> DeviceModel:
    ln_spec = VarSpec(
        MmsKind.STRUCTURE,
        "CSWI1",
        children=(
            VarSpec(MmsKind.STRUCTURE, "ST", children=(VarSpec(MmsKind.STRUCTURE, "Pos", children=(VarSpec(MmsKind.BIT_STRING, "stVal", 2),)),)),
            VarSpec(MmsKind.STRUCTURE, "CF", children=(VarSpec(MmsKind.STRUCTURE, "Pos", children=(VarSpec(MmsKind.INTEGER, "ctlModel", 8),)),)),
            VarSpec(MmsKind.STRUCTURE, "BR", children=(VarSpec(MmsKind.STRUCTURE, "brcb01", children=(VarSpec(MmsKind.VISIBLE_STRING, "RptID", 65),)),)),
        ),
    )
    m = DeviceModel()
    m.lds["CTRL"] = LogicalDeviceInfo("CTRL", {"CSWI1": build_ln("CTRL", "CSWI1", ln_spec)})
    return m


def test_model_merge_and_resolve():
    m = _model()
    pos = m.resolve(ObjectRef("CTRL", "CSWI1", ("Pos",))).node
    assert set(pos.fcs) == {"ST", "CF"} and set(pos.children) == {"stVal", "ctlModel"}
    ref, spec = m.resolve_fc(ObjectRef("CTRL", "CSWI1", ("Pos", "ctlModel")))
    assert ref.fc == "CF" and spec.size == 8
    assert [cb.reference for cb in m.rcbs()] == ["CTRL/CSWI1.BR.brcb01"]
    assert m.children(("CTRL", "CSWI1")) == [("Pos", "do")]
    again = DeviceModel.from_json(json.loads(json.dumps(m.to_json())))
    assert again.resolve_fc(ObjectRef("CTRL", "CSWI1", ("Pos", "stVal")))[1].type_name() == "bit-string(2)"


# ------------------------------------------------------------------------- identity
def test_ldns_mapping():
    assert edition_from_ldns("IEC 61850-7-4:2003") is Edition.ED1
    assert edition_from_ldns("IEC 61850-7-4:2007") is Edition.ED2
    assert edition_from_ldns("IEC 61850-7-4:2007A") is Edition.ED2
    assert edition_from_ldns("IEC 61850-7-4:2007B") is Edition.ED21
    assert edition_from_ldns("vendor-specific") is None


# ------------------------------------------------------------------------- safety
def test_policy_rules():
    p = Policy()
    with pytest.raises(PolicyError):
        p.require_expert("x")
    with pytest.raises(ConfirmationDeclined):
        p.confirm_write(NonInteractive(), "w")
    Policy(yes=True).confirm_write(NonInteractive(), "w")  # --yes confirms writes (SAF-3)
    with pytest.raises(ConfirmationDeclined):
        Policy(yes=True).confirm_dangerous(NonInteractive(), "c", "CSWI1.Pos")  # never controls
    ui = Scripted(["CSWI1.Pos"])
    Policy(safety=SafetyProfile.STRICT).confirm_dangerous(ui, "c", "CSWI1.Pos")
    ui = Scripted([True])
    Policy(safety=SafetyProfile.LAB).confirm_dangerous(ui, "c", "CSWI1.Pos")


# ------------------------------------------------------------------------- inventory
INV = """
schema_version: 1
experiment: e1
safety: lab
devices:
  - {name: relay-F12, ip: 10.0.0.12, role: bcu, reference: {cid: f12.cid}, identity: {edition: Ed2}}
  - {name: sm, ip: 10.0.0.2, role: station-manager}
clients:
  - {name: sm-f12, client: sm, server: relay-F12, source_ip: 10.0.0.5, rcbs: [CTRL/LLN0.BR.brcbA01]}
"""


def test_inventory_parse_and_lookup(tmp_path):
    p = tmp_path / "exp.yaml"
    p.write_text(INV)
    inv = parse_inventory(yaml.safe_load(INV), p)
    assert inv.safety == "lab" and inv.device("10.0.0.12").name == "relay-F12"
    assert inv.devices[0].reference.path == tmp_path / "f12.cid"
    assert inv.name_for_ip("10.0.0.5") == "sm"
    t = resolve_target("relay-F12", inv, as_client="sm")
    assert (t.host, t.local_ip, t.as_client.name) == ("10.0.0.12", "10.0.0.5", "sm-f12")
    assert resolve_target("10.9.9.9:1102", inv).port == 1102
    again = parse_inventory(yaml.safe_load(inv.dump()), p)
    assert again.devices[0].reference.path == inv.devices[0].reference.path


@pytest.mark.parametrize(
    "bad",
    [
        "devices: [{name: a}]",
        "safety: loose",
        "devices: [{name: a, ip: nope}]",
        "devices: [{name: a, ip: 1.2.3.4}]\nclients: [{client: x, server: a, orCat: 2}]",
    ],
)
def test_inventory_rejects(bad):
    with pytest.raises(InventoryError):
        parse_inventory(yaml.safe_load(bad))


def test_owner_ip():
    assert owner_ip(bytes([0] * 12 + [10, 0, 0, 5])) == "10.0.0.5"
    assert owner_ip(bytes(4)) is None and owner_ip(None) is None


# ------------------------------------------------------------------------- log / restore planning
def test_restorable_writes_excludes_controls(tmp_path):
    log = SessionLog.create("dev", tmp_path)
    log.start()
    log.write("write", ref="A/B.C.d", fc="SP", before=1, after=2, ok=True)
    log.write("control", action="operate", outcome={})
    log.write("write", ref="A/B.C.d", fc="SP", before=2, after=3, ok=True)
    log.write("write", ref="A/B.C.e", fc="SP", before=5, after=5, ok=False)
    log.write("write", ref="A/B.C.f", fc="SP", before=0, after=0, ok=True, write_kind="write-test")
    log.close()
    ws = list(restorable_writes(read_log(log.path)))
    assert [(w.ref, w.before) for w in ws] == [("A/B.C.d", 2), ("A/B.C.d", 1)]


# ------------------------------------------------------------------------- results
def test_categories_never_merge():
    rep = CheckReport("d", None)
    rep.add(CheckResult("a", "A", Status.PASS, Category.CONFIGURATION))
    rep.add(CheckResult("b", "B", Status.FAIL, Category.COMMUNICATION))
    s = rep.summary()
    assert s == {"Configuration": "pass", "Communication": "fail", "device_passes": "no"}
    env = envelope("check", rep, device="d")
    assert env["schema_version"] and env["result"]["summary"]["Communication"] == "fail"


def test_diff_ignore_rules(tmp_path):
    a = Snapshot(configuration={"X/L.D.a[CF]": 1, "X/L.OpCnt.stVal[ST]": 3})
    b = Snapshot(configuration={"X/L.D.a[CF]": 2, "X/L.OpCnt.stVal[ST]": 4})
    f = tmp_path / "ignore.yaml"
    f.write_text("ignore:\n  - 'configuration:*OpCnt*'\n")
    d = diff_snapshots(a, b, ignore=IgnoreRules.load(f))
    assert [e.key for e in d.entries] == ["X/L.D.a[CF]"] and d.ignored == 1 and d.status is Status.WARN


def test_codes_parse():
    assert codes.parse_key("add-cause:10").name == "blocked-by-interlocking"
    assert codes.parse_key("data-access:object-access-denied").code == 3
