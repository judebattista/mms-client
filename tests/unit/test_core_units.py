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


# ------------------------------------------------------------------------- RPT-7: BRCB reservation (Version-1.01)
class _FakeRcbClient:
    """Just enough of IedClient for the BRCB reservation cleanup."""

    is_connected = True
    local_ip = "10.0.0.9"

    def __init__(self, *, refuse_release: bool = False, keeps_reservation: bool = False) -> None:
        self.refuse_release = refuse_release
        self.keeps_reservation = keeps_reservation
        self.resv_tms = 30
        self.writes: list[dict] = []

    def get_rcb(self, ref):
        from mms_client.adapter import RcbValues

        owner = bytes([10, 0, 0, 9]) if self.resv_tms else b""
        return RcbValues(ref, True, rpt_ena=False, resv_tms=self.resv_tms, owner=owner)

    def set_rcb(self, ref, changes, single_request=True):
        from mms_client.adapter import ServiceError

        self.writes.append(dict(changes))
        if self.refuse_release:
            raise ServiceError("set-rcb", ref, codes.ied_error(21))
        if not self.keeps_reservation:
            self.resv_tms = changes.get("resv_tms", self.resv_tms)


def _brcb_session(client, *, before_resv_tms=0):
    from mms_client.adapter import RcbValues
    from mms_client.core.model import ControlBlockInfo
    from mms_client.core.reports import _brcb_reservation_cleanup
    from mms_client.core.session import Session, Target

    s = Session(Target("10.0.0.12"))
    s.client = client  # type: ignore[assignment]
    cb = ControlBlockInfo("CTRL", "LLN0", "brcbA01", "BR", VarSpec(MmsKind.STRUCTURE))
    before = RcbValues(cb.reference, True, rpt_ena=False, resv_tms=before_resv_tms, owner=b"")
    s.register_cleanup(_brcb_reservation_cleanup(s, cb, before, ["station-manager-1"]))
    return s


def test_brcb_reservation_is_released_and_nothing_lingers():
    client = _FakeRcbClient()
    notes = _brcb_session(client).run_cleanups()
    assert client.writes == [{"resv_tms": 0}]
    assert notes == ["undone: reservation of CTRL/LLN0.BR.brcbA01 (ResvTms back to 0)"]


def test_brcb_reservation_that_lingers_is_reported_with_effect_and_duration():
    notes = _brcb_session(_FakeRcbClient(keeps_reservation=True)).run_cleanups()
    lingering = [n for n in notes if n.startswith("still in effect:")]
    assert len(lingering) == 1
    assert "up to 30 s" in lingering[0] and "station-manager-1 cannot enable it" in lingering[0]


def test_brcb_reservation_refused_release_is_reported():
    notes = _brcb_session(_FakeRcbClient(refuse_release=True)).run_cleanups()
    assert len(notes) == 1 and notes[0].startswith("could not undo: reservation of CTRL/LLN0.BR.brcbA01")
    assert "station-manager-1 cannot enable it" in notes[0]


def test_brcb_reserved_by_configuration_is_not_released():
    client = _FakeRcbClient()
    notes = _brcb_session(client, before_resv_tms=-1).run_cleanups()
    assert client.writes == []  # a reservation made by configuration is not the tool's to release
    assert notes[0].startswith("left the reservation of CTRL/LLN0.BR.brcbA01 as the tool found it")


def test_brcb_reservation_held_by_another_client_is_left_alone():
    client = _FakeRcbClient()
    client.get_rcb = lambda ref: __import__("mms_client.adapter", fromlist=["RcbValues"]).RcbValues(  # type: ignore[method-assign]
        ref, True, rpt_ena=False, resv_tms=30, owner=bytes([10, 0, 0, 5]))
    notes = _brcb_session(client).run_cleanups()
    assert client.writes == []
    assert notes == ["left the reservation of CTRL/LLN0.BR.brcbA01 alone: it is held by 10.0.0.5, not by this tool"]


# ------------------------------------------------------------------------- LOG-2: restore markers (Version-1.01)
def test_restored_seqs_counts_only_successful_restores_of_the_source_session():
    from mms_client.core.sessionlog import restored_seqs

    entries = [
        {"seq": 1, "kind": "write", "ok": True, "ref": "LD/LN.A.b", "fc": "SP", "before": 1},
        # a restore's own write: accepted, but only the restore entry says whether the value was put back
        {"seq": 2, "kind": "write", "ok": True, "restored_from": 1, "restored_from_session": "S1"},
        {"seq": 3, "kind": "restore", "ok": False, "restored_from": 1, "restored_from_session": "S1"},
    ]
    assert restored_seqs(entries, "S1", same_log=True) == set()
    entries.append({"seq": 4, "kind": "restore", "ok": True, "restored_from": 1, "restored_from_session": "S1"})
    assert restored_seqs(entries, "S1", same_log=True) == {1}
    assert restored_seqs(entries, "OTHER", same_log=True) == set()


def test_restored_seqs_reads_pre_1_01_markers_only_in_their_own_log():
    from mms_client.core.sessionlog import restored_seqs

    legacy = [{"seq": 9, "kind": "write", "ok": True, "restored_from": 4, "before": 2, "after": 1}]
    assert restored_seqs(legacy, "S1", same_log=True) == {4}
    assert restored_seqs(legacy, "S1", same_log=False) == set()


def test_restorable_writes_skips_restore_writes_and_already_restored():
    entries = [
        {"seq": 1, "kind": "write", "ok": True, "ref": "LD/LN.A.b", "fc": "SP", "before": 1, "after": 2},
        {"seq": 2, "kind": "write", "ok": True, "ref": "LD/LN.A.b", "fc": "SP", "before": 2, "after": 3},
        {"seq": 3, "kind": "write", "ok": True, "ref": "LD/LN.A.b", "fc": "SP", "before": 3, "after": 2,
         "restored_from": 2, "restored_from_session": "S1"},
    ]
    assert [w.seq for w in restorable_writes(entries)] == [2, 1]
    assert [w.seq for w in restorable_writes(entries, already_restored={2})] == [1]


# ------------------------------------------------------------------------- IDN-2/3/6: edition sources (Version-1.01)
def test_scl_edition_is_confirmed_only_when_the_file_states_it_for_the_ied(tmp_path):
    from pathlib import Path

    from mms_client.core.identity import Confidence, IdentityReport, determine_edition
    from mms_client.verify.reference import Reference

    scl = Path(__file__).resolve().parents[1] / "fixtures" / "scl"
    # one IED in the file (CID): the file states that IED's edition
    assert Reference.load("cid", scl / "bcu_ed2.cid").scl_edition().confirmed
    # several IEDs, each with its own originalSclVersion
    own = Reference.load("scd", scl / "rack_mixed.scd", "IED3").scl_edition()
    assert own is not None and own.edition == "Ed1" and own.confirmed
    # several IEDs, and IED3 does not declare its own version: only the SCD's schema version is known
    text = (scl / "rack_mixed.scd").read_text(encoding="utf-8").replace(' originalSclVersion="2003"', "")
    (tmp_path / "rack.scd").write_text(text, encoding="utf-8")
    ed = Reference.load("scd", tmp_path / "rack.scd", "IED3").scl_edition()
    assert ed is not None and not ed.confirmed and "does not state IED3's own edition" in ed.reason
    rep = IdentityReport()
    determine_edition(rep, None, scl_edition=ed)
    assert rep.edition.confidence is Confidence.INFERRED  # IDN-3: never shown as confirmed


def test_operator_edition_is_the_last_resort_and_survives_a_new_identity():
    from mms_client.core.identity import Attr, Confidence, IdentityReport, determine_edition

    answer = Attr(Edition.ED1, "operator", Confidence.OPERATOR)
    rep = IdentityReport()
    determine_edition(rep, None, operator_edition=answer)
    assert rep.edition == answer
    # a device-reported namespace still takes precedence over the answer (IDN-2 order)
    rep = IdentityReport(ld_namespaces={"LD": "IEC 61850-7-4:2007B"})
    determine_edition(rep, None, operator_edition=answer)
    assert rep.edition.value is Edition.ED21 and rep.edition.confidence is Confidence.INFERRED
