"""Snapshots and diffs against the simulated IED."""

from __future__ import annotations

import json

import pytest

from mms_client.core import readwrite, reports
from mms_client.core.results import Status
from mms_client.core.safety import Policy, Scripted
from mms_client.core.session import Session, Target
from mms_client.verify.diff import IgnoreRules, diff_snapshots
from mms_client.verify.snapshot import Snapshot, capture

pytestmark = pytest.mark.integration
LD = "SIMCTRL"


def session(sim, answers=()):
    s = Session(Target("127.0.0.1", sim.port, "sim"), policy=Policy(yes=True), ui=Scripted(list(answers)))
    s.connect()
    return s


def test_capture_layers_and_stable_file(sim, tmp_path):
    s = session(sim)
    try:
        snap = capture(s, experiment="unit")
    finally:
        s.close()
    assert snap.configuration[f"{LD}/GGIO1.Setp1.setVal[SP]"] in (10, 33)
    assert snap.configuration[f"{LD}/LLN0.NamPlt.configRev[DC]"] == "cfg-7"
    assert snap.configuration["identity:mms-identify.vendor"] == "SimVendor"
    assert f"{LD}/GGIO1.OpDlTmms.setVal[SG]" in snap.configuration
    assert not any("[SE]" in k for k in snap.configuration), "SE values need an edit session: never captured"
    assert snap.operational[f"{LD}/GGIO2.Loc.stVal[ST]"] is True
    assert snap.operational[f"{LD}/LLN0.RP.urcbEvents01.RptEna"] is False
    assert f"{LD}/GGIO1.SPCSO1.stVal[ST]" not in snap.operational  # ordinary ST values are left out
    cbs = snap.structure["control_blocks"]
    assert cbs[f"{LD}/LLN0.BR.brcbMeas01"]["ConfRev"] == 5
    assert cbs[f"{LD}/LLN0.SP.SGCB"]["NumOfSG"] == 3
    assert snap.structure["datasets"][f"{LD}/LLN0$Events"][0] == f"{LD}/GGIO1$ST$SPCSO1$stVal"
    assert snap.structure["model"][f"{LD}/GGIO1.AnIn1.mag.f[MX]"] == "float(32)"
    md = snap.metadata
    assert md["edition"]["confidence"] == "inferred" and md["tool"]["name"] == "mms-client"
    assert md["experiment"] == "unit" and md["active_setting_groups"] == {LD: 1}
    p = snap.save(tmp_path / "s.json")
    text = p.read_text()
    assert json.loads(text)["kind"] == "mms-client-snapshot"
    assert text == Snapshot.load(p).dumps()  # stable round trip (VER-13)


def test_diff_detects_changes_by_layer(sim):
    s = session(sim, [True, True])
    try:
        before = capture(s)
        readwrite.write(s, f"{LD}/GGIO1.Cfg1.cfgVal", "6", confirm=False)
        sub = reports.subscribe(s, f"{LD}/LLN0.RP.urcbEvents01", gi=False)
        after = capture(s)
        sub.stop()
        readwrite.write(s, f"{LD}/GGIO1.Cfg1.cfgVal", "5", confirm=False)
    finally:
        s.close()
    d = diff_snapshots(before, after)
    by_key = {e.key: e for e in d.entries}
    cfg = by_key[f"{LD}/GGIO1.Cfg1.cfgVal[CF]"]
    assert (cfg.old, cfg.new, cfg.severity) == (5, 6, Status.WARN)
    op = by_key[f"{LD}/LLN0.RP.urcbEvents01.RptEna"]
    assert op.severity is Status.INFO and op.new is True
    assert d.status is Status.WARN
    rules = IgnoreRules(patterns=[("configuration", "*Cfg1*"), (None, "*.Owner"), (None, "*.RptEna"), (None, "*.Resv")])
    d2 = diff_snapshots(before, after, ignore=rules)
    assert d2.status is Status.PASS and d2.ignored >= 2


def test_structure_change_is_fail(sim):
    s = session(sim)
    try:
        a = capture(s)
    finally:
        s.close()
    b = Snapshot.from_json(json.loads(a.dumps()))
    b.structure["model"].pop(f"{LD}/GGIO1.AnIn1.mag.f[MX]")
    b.structure["control_blocks"][f"{LD}/LLN0.BR.brcbMeas01"]["ConfRev"] = 6
    d = diff_snapshots(a, b)
    assert d.status is Status.FAIL
    changes = {(e.section, e.change) for e in d.entries}
    assert ("model", "removed") in changes and ("control_blocks", "changed") in changes
