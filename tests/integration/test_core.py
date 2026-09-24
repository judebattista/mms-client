"""Core services against the simulated IED."""

from __future__ import annotations

import time

import pytest

from mms_client.adapter import BitString
from mms_client.core import controls, files, readwrite, reports, restore, setgroup
from mms_client.core.identity import Confidence, Edition, collect_identity
from mms_client.core.safety import ConfirmationDeclined, Mode, Policy, PolicyError, SafetyProfile, Scripted
from mms_client.core.session import Session, Target
from mms_client.core.sessionlog import SessionLog
from mms_client.inventory import ClientRelation, Device, Inventory

pytestmark = pytest.mark.integration
LD = "SIMCTRL"


def make_session(sim, answers=(), *, expert=False, safety=SafetyProfile.STRICT, tmp_path=None, inventory=None, yes=False):
    pol = Policy(mode=Mode.EXPERT if expert else Mode.STANDARD, safety=safety, yes=yes)
    log = SessionLog.create("sim", tmp_path) if tmp_path else None
    s = Session(Target("127.0.0.1", sim.port, "sim"), policy=pol, ui=Scripted(list(answers)), log=log, inventory=inventory)
    s.connect()
    return s


# ------------------------------------------------------------------ identity
def test_identity_sources_and_edition(sim):
    s = make_session(sim)
    try:
        rep = collect_identity(s.client, s.model())
        assert rep.vendor.value == "SimVendor" and rep.vendor.confidence is Confidence.CONFIRMED
        assert rep.model.value == "SimIED"
        srcs = {x.source for x in rep.sources}
        assert {"mms-identify", "LPHD.PhyNam", "LLN0.NamPlt"} <= srcs
        assert rep.edition.value is Edition.ED21 and rep.edition.confidence is Confidence.INFERRED
        assert rep.config_revs == {LD: "cfg-7"}
    finally:
        s.close()


# ------------------------------------------------------------------ controls
@pytest.mark.parametrize("obj,value", [("SPCSO1", "true"), ("SPCSO2", "true"), ("SPCSO3", "false"), ("SPCSO4", "true"), ("DPCSO1", "close"), ("IntIn1", "5")])
def test_operate_sequences(sim, obj, value):
    s = make_session(sim, [f"GGIO1.{obj}"])
    try:
        plan = controls.plan_control(s, f"{LD}/GGIO1.{obj}", value, or_cat=2)
        out = controls.execute(s, plan)
        assert out.ok, out.to_json()
        services = [st.service for st in out.steps]
        expected = {1: ["operate"], 2: ["select", "operate"], 3: ["operate"], 4: ["select-with-value", "operate"]}[plan.ctl_model]
        assert services == expected
        if plan.ctl_model in (3, 4):
            assert out.termination is not None and out.termination.positive
        assert all(st.duration_s >= 0 for st in out.steps)
        if obj == "DPCSO1":
            assert isinstance(out.final, BitString) and out.final.as_int_msb0() == 2
        origin = controls.last_origin(s, f"{LD}/GGIO1.{obj}")
        assert origin["origin"]["orCat"] == 2 and origin["orCat_name"] == "station-control"
        assert origin["orIdent_text"].startswith("mms-client/")
    finally:
        s.close()


def test_orcat_required_and_expert_values(sim):
    s = make_session(sim, [None])
    try:
        with pytest.raises(PolicyError) as ei:
            controls.plan_control(s, f"{LD}/GGIO1.SPCSO1", "true")  # operator declines to pick one
        assert ei.value.error.name == "orcat-required"
        with pytest.raises(PolicyError) as ei:
            controls.plan_control(s, f"{LD}/GGIO1.SPCSO1", "true", or_cat=7)
        assert ei.value.error.name == "refused-standard-mode"
        s.set_or_cat(1)
        assert controls.plan_control(s, f"{LD}/GGIO1.SPCSO1", "true").or_cat == 1
        with pytest.raises(PolicyError):
            controls.plan_control(s, f"{LD}/GGIO1.SPCSO1", "true", interlock_check=False)
    finally:
        s.close()


def test_control_confirmation_rules(sim):
    s = make_session(sim, ["wrong name"])
    try:
        plan = controls.plan_control(s, f"{LD}/GGIO1.SPCSO1", "true", or_cat=1)
        with pytest.raises(ConfirmationDeclined):
            controls.execute(s, plan)
        s.policy.yes = True  # --yes never skips control confirmation (SAF-3)
        s.ui = Scripted(["nope"])
        with pytest.raises(ConfirmationDeclined):
            controls.execute(s, plan)
    finally:
        s.close()
    lab = make_session(sim, [True], safety=SafetyProfile.LAB)
    try:
        plan = controls.plan_control(lab, f"{LD}/GGIO1.SPCSO1", "false", or_cat=1)
        assert controls.execute(lab, plan).ok
        assert "Send this control?" in lab.ui.prompts[0]
    finally:
        lab.close()


def test_blocked_command_warns_and_decodes_add_cause(sim):
    s = make_session(sim, ["GGIO2.SPCSO1"])
    try:
        plan = controls.plan_control(s, f"{LD}/GGIO2.SPCSO1", "true", or_cat=3)
        assert any("Loc is true" in w for w in plan.warnings)
        assert plan.authority.get("Loc") is True
        out = controls.execute(s, plan)
        assert not out.ok
        assert out.error.key == "add-cause:blocked-by-switching-hierarchy"
        assert s.last_error.error.key == "add-cause:blocked-by-switching-hierarchy"
    finally:
        s.close()


def test_status_only_refused_and_select_cancel(sim):
    s = make_session(sim, ["GGIO1.SPCSO4"])
    try:
        plan = controls.plan_control(s, f"{LD}/GGIO1.SPCSO4", "true", or_cat=1)
        step = controls.select_only(s, plan)
        assert step.ok and f"{LD}/GGIO1.SPCSO4" in s.selected
        assert controls.cancel(s, f"{LD}/GGIO1.SPCSO4").ok
        assert f"{LD}/GGIO1.SPCSO4" not in s.selected
        with pytest.raises(PolicyError):
            controls.select_only(s, controls.plan_control(s, f"{LD}/GGIO1.SPCSO1", "true", or_cat=1), confirm=False)
    finally:
        s.close()


def test_authority_probe_matrix(sim):
    s = make_session(sim, ["authority-probe"])
    try:
        rows = {r.ref.iec(): r for r in controls.authority_probe(s)}
        direct = rows[f"{LD}/GGIO1.SPCSO1"]
        assert all(c.accepted is None for c in direct.cells) and "not probeable" in direct.note
        loc = rows[f"{LD}/GGIO2.SPCSO1"]
        by_cat = {c.or_cat: c for c in loc.cells}
        assert by_cat[1].accepted is True
        assert by_cat[2].accepted is False and by_cat[2].add_cause.name == "blocked-by-switching-hierarchy"
        assert by_cat[3].accepted is False
        assert loc.authority.get("Loc") is True
        free = rows[f"{LD}/GGIO1.SPCSO4"]
        assert all(c.accepted for c in free.cells)
        # nothing was operated
        assert readwrite.read(s, f"{LD}/GGIO2.SPCSO1.stVal").value is False
    finally:
        s.close()


def test_command_termination_negative():
    from tests.conftest import FIXTURES
    from tests.sim.fixture import SimProcess

    with SimProcess(config=FIXTURES / "sim" / "basic.cfg", options={"fail_execution": {f"{LD}/GGIO1.SPCSO4": 9}}) as p:
        s = make_session(p, ["GGIO1.SPCSO4"])
        try:
            plan = controls.plan_control(s, f"{LD}/GGIO1.SPCSO4", "true", or_cat=2)
            out = controls.execute(s, plan)
            assert not out.ok
            assert out.termination is not None and not out.termination.positive
            assert out.error.key == "add-cause:blocked-by-process"
        finally:
            s.close()


# ------------------------------------------------------------------ reports
def test_rcb_listing_and_subscribe_cleanup(sim, tmp_path):
    inv = Inventory(devices=[Device("sim", "127.0.0.1")], clients=[ClientRelation("gw", "gateway-1", "sim", "127.0.0.9", [f"{LD}/LLN0.RP.urcbEvents02"])])
    s = make_session(sim, tmp_path=tmp_path, inventory=inv)
    try:
        rcbs = {st.cb.reference: st for st in reports.list_rcbs(s)}
        assert rcbs[f"{LD}/LLN0.RP.urcbEvents01"].state == "free"
        assert rcbs[f"{LD}/LLN0.RP.urcbEvents02"].assigned_to == ["gateway-1"]
        sub = reports.subscribe(s, "urcbEvents", trg_ops=reports.parse_trg_ops("dchg,gi"))
        assert sub.reference == f"{LD}/LLN0.RP.urcbEvents01"  # 02 is assigned to the gateway
        first = sub.next(timeout=3)
        assert first is not None and first.report.entries
        assert first.members[0][0] == f"{LD}/GGIO1.SPCSO1.stVal"
        # the tool's own RCB now shows as enabled by "this tool"
        st = {x.cb.reference: x for x in reports.list_rcbs(s)}[sub.reference]
        assert st.state == "enabled"
        notes = sub.stop()
        assert any("undone" in n for n in notes)
        v = s.client.get_rcb(sub.reference)
        assert v.rpt_ena is False and v.resv is False and v.trg_ops == rcbs[sub.reference].values.trg_ops
    finally:
        s.close()


def test_no_free_instance_lists_all_and_takeover_needs_expert(sim):
    holder = make_session(sim)
    s = make_session(sim, ["urcbEvents01"], expert=False)
    try:
        a = reports.subscribe(holder, f"{LD}/LLN0.RP.urcbEvents01")
        b = reports.subscribe(holder, f"{LD}/LLN0.RP.urcbEvents02")
        with pytest.raises(reports.NoFreeRcbError) as ei:
            reports.subscribe(s, "urcbEvents")
        assert len(ei.value.instances) == 2 and "enabled by" in str(ei.value)
        with pytest.raises(PolicyError) as ei2:
            reports.subscribe(s, "urcbEvents", takeover=True)
        assert ei2.value.error.name == "refused-standard-mode"
        a.stop()
        b.stop()
    finally:
        holder.close()
        s.close()


def test_cleanup_on_disconnect(sim):
    s = make_session(sim)
    reports.subscribe(s, f"{LD}/LLN0.BR.brcbMeas01", intg_pd=500)
    notes = s.disconnect()
    assert any("RptEna" in n and "undone" in n for n in notes)
    assert any("parameters" in n and "undone" in n for n in notes)
    check = make_session(sim)
    try:
        v = check.client.get_rcb(f"{LD}/LLN0.BR.brcbMeas01")
        assert v.rpt_ena is False and v.intg_pd == 1000
    finally:
        check.close()


def test_cleanup_reported_when_connection_lost():
    from tests.conftest import FIXTURES
    from tests.sim.fixture import SimProcess

    with SimProcess(config=FIXTURES / "sim" / "basic.cfg") as p:
        s = make_session(p)
        reports.subscribe(s, f"{LD}/LLN0.BR.brcbMeas01")
        p.kill()
        assert s.connection_lost.wait(5)
        notes = s.close()
        assert any("could not undo" in n and "ResvTms" in n for n in notes)


# ------------------------------------------------------------------ setting groups, files, restore
def test_setgroup_edit_and_activate(sim, tmp_path):
    s = make_session(sim, [True, True], tmp_path=tmp_path)
    try:
        st = setgroup.show(s)[0]
        assert st.values["NumOfSG"] == 3 and st.values["ActSG"] == 1
        res = setgroup.edit(s, 2, [(f"{LD}/GGIO1.OpDlTmms.setVal", "250")])
        assert res.confirmed and res.verified, res.to_json()
        after = setgroup.activate(s, 2)
        assert after.values["ActSG"] == 2
        assert readwrite.read(s, f"{LD}/GGIO1.OpDlTmms.setVal [SG]").value == 250
        with pytest.raises(PolicyError):
            readwrite.write(s, f"{LD}/GGIO1.OpDlTmms.setVal [SE]", "1")
        s.ui = Scripted([True])
        plan = restore.plan_restore(s)
        assert [i.write.kind for i in plan.items] == ["sgcb-actsg", "setgroup"]
        restore.run_restore(s, plan)
        assert all(i.ok for i in plan.items), plan.to_json()
        assert setgroup.show(s)[0].values["ActSG"] == 1
    finally:
        s.close()


def test_write_restore_roundtrip(sim, tmp_path):
    s = make_session(sim, [True, True, True], tmp_path=tmp_path)
    try:
        readwrite.write(s, f"{LD}/GGIO1.Setp1.setVal", "11")
        readwrite.write(s, f"{LD}/GGIO1.Setp1.setVal", "12")
        plan = restore.plan_restore(s)
        assert [i.write.before for i in plan.items] == [11, 10]
        restore.run_restore(s, plan)
        assert readwrite.read(s, f"{LD}/GGIO1.Setp1.setVal").value == 10
        assert s.log.path is not None and s.log.path.exists()
    finally:
        s.close()


def test_files(sim, tmp_path):
    s = make_session(sim)
    try:
        names = {e.name for e in files.list_files(s)}
        assert "COMTRADE_0001.dat" in names
        d = files.get_file(s, "COMTRADE_0001.dat", tmp_path)
        assert d.size == 150000 and d.local.stat().st_size == 150000
        assert [e.name for e in files.find_scl_files(s)] == ["device.cid"]
    finally:
        s.close()


def test_watch_yields_changes(sim):
    s = make_session(sim)
    try:
        evs = list(readwrite.watch(s, f"{LD}/GGIO1.AnIn1.mag.f", interval_s=0.06, count=6))
        assert evs and evs[0].changed
        assert len(evs) >= 2  # the simulator updates this value every 50 ms
    finally:
        s.close()
    time.sleep(0.1)
