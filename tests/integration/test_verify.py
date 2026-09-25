"""Verification (§12) against a simulator built from an SCL file."""

from __future__ import annotations

import pytest

from ied_client.core.results import Category, Status
from ied_client.core.safety import NonInteractive, Policy, Scripted
from ied_client.core.session import Session, Target
from ied_client.verify.checks import CheckOptions, run_checks
from ied_client.verify.reference import Reference
from ied_client.verify.snapshot import capture
from tests.conftest import FIXTURES

pytestmark = pytest.mark.integration
SCL = FIXTURES / "scl"


@pytest.fixture(scope="module")
def bcu_sim():
    from tests.sim.fixture import SimProcess

    with SimProcess(scl=SCL / "bcu_ed2.cid", ied="BCU1", options={"identity": ["Rackworks", "BCU-100", "2.4"]}) as p:
        yield p


def session(sim, ui=None):
    s = Session(Target("127.0.0.1", sim.port, "bcu1"), policy=Policy(), ui=ui or NonInteractive())
    s.connect()
    return s


def by_id(report, check_id):
    return [r for r in report.results if r.id == check_id]


def test_cid_reference_matches(bcu_sim):
    s = session(bcu_sim)
    try:
        ref = Reference.load("cid", SCL / "bcu_ed2.cid", live_model=s.model())
        assert ref.ied_name == "BCU1"
        s.reference = ref
        rep = run_checks(s, ref)
    finally:
        s.close()
    fails = [r for r in rep.results if r.status is Status.FAIL]
    # The simulator listens on 127.0.0.1, the CID says 10.0.0.11: the only expected failure.
    assert {r.id for r in fails} == {"address-mismatch"}, [r.to_json() for r in fails]
    assert any(r.id == "model-structure" and r.status is Status.PASS for r in rep.results)
    assert all(r.status is Status.PASS for r in by_id(rep, "confrev-mismatch"))
    assert all(r.status is Status.PASS for r in by_id(rep, "dataset-mismatch"))
    summary = rep.summary()
    assert summary["Configuration"] == "fail" and summary["device_passes"] == "no"
    assert set(summary) == {"Configuration", "Communication", "device_passes"}


def test_broken_reference_is_detected(bcu_sim):
    s = session(bcu_sim)
    try:
        ref = Reference.load("cid", SCL / "broken_refs.cid", live_model=s.model())
        rep = run_checks(s, ref)
    finally:
        s.close()
    ids = {r.id for r in rep.results if r.status in (Status.FAIL, Status.WARN)}
    assert "dataset-mismatch" in ids or "rcb-attribute-mismatch" in ids


def test_scd_cross_device_checks(bcu_sim):
    s = session(bcu_sim)
    try:
        ref = Reference.load("scd", SCL / "rack_mixed.scd", "BCU1")
        rep = run_checks(s, ref)
    finally:
        s.close()
    comm = [r for r in rep.results if r.category is Category.COMMUNICATION]
    rcb_checks = [r for r in comm if r.id == "client-rcb-not-available"]
    assert rcb_checks and all(r.status is Status.PASS for r in rcb_checks)
    assert any("SM1" in (r.subject or "") for r in rcb_checks)


def test_self_consistency_only_without_reference(bcu_sim):
    s = session(bcu_sim)
    try:
        rep = run_checks(s, None, CheckOptions())
    finally:
        s.close()
    ref_rows = by_id(rep, "reference")
    assert ref_rows and ref_rows[0].status is Status.NOT_RUN
    assert by_id(rep, "rcb-dataset-missing") and all(r.status is Status.PASS for r in by_id(rep, "rcb-dataset-missing"))
    assert all(r.status is Status.PASS for r in by_id(rep, "dataset-member-unresolved"))


def test_snapshot_reference(bcu_sim, tmp_path):
    s = session(bcu_sim)
    try:
        snap = capture(s)
        path = snap.save(tmp_path / "bcu.json")
        ref = Reference.load("snapshot", path)
        rep = run_checks(s, ref)
    finally:
        s.close()
    snap_rows = [r for r in rep.results if r.id.startswith("snapshot-")]
    assert snap_rows and all(r.status is Status.PASS for r in snap_rows), [r.to_json() for r in snap_rows if r.status is not Status.PASS]


def test_edition_question_when_unknown(bcu_sim):
    """IDN-4/5: unknown edition → asked interactively with "don't know"; non-interactive → not-run."""
    from ied_client.core.identity import Attr, Confidence, Edition, IdentityReport
    from ied_client.verify.checks import edition_for_check

    s = session(bcu_sim, ui=Scripted(["unknown"]))
    try:
        s.identity = IdentityReport(edition=Attr(Edition.UNKNOWN, "none", Confidence.UNKNOWN))
        assert edition_for_check(s, "test") is Edition.UNKNOWN
        assert ("unknown", "Don't know") in s.ui.options[0]
        assert "Where to look" in s.ui.prompts[0]
        s.ui = NonInteractive()
        assert edition_for_check(s, "test") is Edition.UNKNOWN
        s.ui = Scripted(["Ed1"])
        assert edition_for_check(s, "test") is Edition.ED1
        assert s.identity.edition.confidence is Confidence.OPERATOR
    finally:
        s.close()


def test_device_supplied_reference(tmp_path):
    """VER-8: an SCL file found on the device is offered and labelled device-supplied."""
    import shutil

    from ied_client.verify.reference import reference_for
    from tests.sim.fixture import SimProcess

    files = tmp_path / "files"
    files.mkdir()
    shutil.copy(SCL / "bcu_ed2.cid", files / "BCU1.cid")
    with SimProcess(scl=SCL / "bcu_ed2.cid", ied="BCU1", options={"files": str(files)}) as p:
        s = session(p, ui=Scripted(["BCU1.cid"]))
        try:
            ref = reference_for(s, device_supplied=True)
            assert ref is not None and ref.kind == "device-supplied" and "not an independent reference" in ref.label
            rep = run_checks(s, ref)
        finally:
            s.close()
    info = by_id(rep, "device-supplied-reference")
    assert info and info[0].status is Status.INFO
    assert rep.reference["kind"] == "device-supplied"


def test_operator_edition_is_offered_for_saving(bcu_sim, tmp_path):
    """IDN-6 (Version-1.01): the operator's edition answer can be stored at once, so it is asked only once."""
    from ied_client.core.identity import Attr, Confidence, Edition, IdentityReport, identify
    from ied_client.inventory import load_inventory
    from ied_client.verify.checks import edition_for_check

    path = tmp_path / "rack.yaml"
    path.write_text(f"schema_version: 1\ndevices:\n  - name: bcu1\n    ip: 127.0.0.1\n    port: {bcu_sim.port}\n")
    inv = load_inventory(path)
    s = Session(Target("127.0.0.1", bcu_sim.port, "bcu1", inv.devices[0]), inventory=inv, policy=Policy(),
                ui=Scripted(["Ed1", True]))
    try:
        s.connect()
        s.identity = IdentityReport(edition=Attr(Edition.UNKNOWN, "none", Confidence.UNKNOWN))
        assert edition_for_check(s, "test") is Edition.ED1
        assert s.operator_edition.value is Edition.ED1
        assert "edition: Ed1" in path.read_text()
        assert "Save edition Ed1" in s.ui.prompts[-1]
        # reading the identity again keeps it (now from the inventory, operator-supplied)
        rep = identify(s)
        assert rep.edition.value is Edition.ED1 and rep.edition.confidence is Confidence.OPERATOR
    finally:
        s.close()


def test_device_scl_is_offered_without_being_asked_for(tmp_path):
    """VER-8 (Version-1.01): with no reference, an interactive check looks on the device and offers what it finds;
    a non-interactive one does not touch the file service."""
    import shutil

    from ied_client.verify.reference import reference_for
    from tests.sim.fixture import SimProcess

    files = tmp_path / "files"
    files.mkdir()
    shutil.copy(SCL / "bcu_ed2.cid", files / "BCU1.cid")
    with SimProcess(scl=SCL / "bcu_ed2.cid", ied="BCU1", options={"files": str(files)}) as p:
        s = session(p, ui=Scripted(["BCU1.cid"]))
        try:
            ref = reference_for(s)
            assert ref is not None and ref.kind == "device-supplied"
            assert "BCU1.cid" in s.ui.options[0][0]
        finally:
            s.close()
        s = session(p)  # NonInteractive
        try:
            assert reference_for(s) is None
            assert not any(e.get("kind") == "file" for e in s.log.entries)
            rep = run_checks(s, None)
            assert "--device-scl" in by_id(rep, "reference")[0].reason
        finally:
            s.close()
