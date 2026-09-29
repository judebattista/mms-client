"""tools/ingest_field_data.py (FLD-5): classify a field bundle, review it, write it into a repository copy."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml

from ied_client import localdata
from ied_client.explain import Catalogue, reset_default_catalogue
from tools import ingest_field_data as ingest

SRC = Path(__file__).resolve().parents[2] / "src"

HINTS = """\
version: 1
entries:
- id: rack.slow-gi
  keys: [add-cause:blocked-by-mode]
  when: {service: operate, tags: {rack: "b"}}
  hint: "On rack B the bay is kept in test mode between runs; `read LLN0.Mod` first"
  certainty: check
- id: tool.refused-standard-mode
  keys: [tool:refused-standard-mode]
  title: Needs expert mode (rack wording)
  summary: The action is only allowed in expert mode.
  hint: "This action needs expert mode"
  certainty: fact
- id: rack.bay-b
  terms: [bay B]
  title: Bay B
  summary: The second bay of the rack.
- id: data-access.rack-denied
  keys: [data-access:object-access-denied]
  when: {service: write, fc: SP, tags: {rack: "b"}}
  hint: "Rack B relays accept setting writes only from 10.0.0.20"
  certainty: likely
"""
QUIRKS = """\
schema_version: 1
quirks:
  - match: {vendor: ACME, model: BCU-5, firmware: "2.3"}
    max_associations: 4
    source: incidents/assoc.json
  - match: {vendor: Existing, model: M}
    notes: known
    source: somewhere else
  - match: {vendor: Clash, model: M}
    max_associations: 3
    source: s
"""
REPO_QUIRKS = """
  - match: {vendor: Existing, model: M}
    notes: known
    source: spike
  - match: {vendor: Clash, model: M}
    max_associations: 5
    source: spike
"""


@pytest.fixture
def setup(tmp_path, monkeypatch):
    """(repo copy, bundle path) with a bundle made by `local export` from local layers."""
    repo = tmp_path / "repo"
    for pkg in ("ied_client", "mms_protocol"):
        shutil.copytree(SRC / pkg / "data", repo / "src" / pkg / "data")
    q = repo / "src" / "mms_protocol" / "data" / "quirks.yaml"
    q.write_text(q.read_text().rstrip("\n") + "\n" + REPO_QUIRKS)
    user = tmp_path / "laptop"
    monkeypatch.setenv("IED_CLIENT_LOCAL_DIRS", str(user))
    reset_default_catalogue()
    (user / "hints").mkdir(parents=True)
    (user / "hints" / "local.yaml").write_text(HINTS)
    (user / "quirks" / "mms").mkdir(parents=True)
    (user / "quirks" / "mms" / "local.yaml").write_text(QUIRKS)
    (user / "incidents").mkdir()
    (user / "incidents" / "assoc.json").write_text('{"kind": "ied-client-incident"}\n')
    bundle = tmp_path / "ied-client-field-laptop3-20261002.json"
    bundle.write_text(json.dumps(localdata.build_bundle()))
    yield ingest.load_repo(repo), bundle
    reset_default_catalogue()


def by_ref(cands):
    return {c.ref: c for c in cands}


def test_classify(setup):
    repo, bundle = setup
    cands, notes = ingest.classify(localdata.read_bundle(bundle), repo)
    assert notes == []
    c = by_ref(cands)
    data = repo.root / "src"
    assert c["rack.slow-gi"].action == "add"
    assert c["rack.slow-gi"].target == data / "ied_client" / "data" / "hints" / "field.yaml"  # add-cause: client domain
    assert c["data-access.rack-denied"].target == data / "mms_protocol" / "data" / "hints" / "field.yaml"  # MMS domain
    assert c["rack.bay-b"].target == data / "ied_client" / "data" / "glossary" / "field.yaml"  # a term
    assert c["tool.refused-standard-mode"].action == "change"
    assert c["tool.refused-standard-mode"].target == data / "ied_client" / "data" / "hints" / "tool.yaml"
    assert "+  title: Needs expert mode (rack wording)" in c["tool.refused-standard-mode"].diff
    assert c["ACME / BCU-5 / firmware 2.3"].action == "add"
    assert c["Existing / M / any firmware"].action == "duplicate"
    assert c["Clash / M / any firmware"].action == "conflict" and "max_associations" in c["Clash / M / any firmware"].diff


def test_apply_writes_everything_and_the_ledger_closes_the_loop(setup):
    repo, bundle_path = setup
    bundle = localdata.read_bundle(bundle_path)
    cands, _ = ingest.classify(bundle, repo)
    answers = iter(["a", "r", "no thanks", "a", "a", "a", "s"])  # in bundle order; the duplicate is not asked
    ingest.review(cands, accept_all=False, ask=lambda _prompt: next(answers))
    c = by_ref(cands)
    assert c["tool.refused-standard-mode"].decision == "rejected" and c["tool.refused-standard-mode"].reason == "no thanks"
    assert c["Existing / M / any firmware"].decision == "accepted"  # duplicate: nothing to write, recorded
    assert c["Clash / M / any firmware"].decision is None  # skipped

    changed, evidence, _lint = ingest.apply(cands, bundle, bundle_path, repo, "2026-10-05")
    for p, text in {**changed, **evidence}.items():
        localdata.atomic_write(p, text)
    root = repo.root
    tool_yaml = (root / "src/ied_client/data/hints/tool.yaml").read_text()
    assert "rack wording" not in tool_yaml  # rejected
    field = (root / "src/ied_client/data/hints/field.yaml").read_text()
    assert field.startswith("# Entries reviewed into the repository") and "# field: ied-client-field-laptop3-20261002.json" in field
    quirks = yaml.safe_load((root / "src/mms_protocol/data/quirks.yaml").read_text())["quirks"]
    acme = [q for q in quirks if q["match"]["vendor"] == "ACME"]
    assert acme and acme[0]["source"] == "field-data/incidents/ied-client-field-laptop3-20261002/assoc.json"
    assert (root / acme[0]["source"]).is_file()
    assert (root / "field-data/bundles" / bundle_path.name).is_file()

    # the repository still loads, with the new entries
    fresh = ingest.load_repo(root)
    cat = Catalogue.from_texts(fresh.catalogue_sources())
    assert "rack.slow-gi" in cat and "rack.bay-b" in cat and "data-access.rack-denied" in cat

    # the ledger: one row per decision, and the same bundle again has nothing left but the skipped conflict
    ledger = localdata.parse_ledger((root / "src/ied_client/data/field-ledger.yaml").read_text())
    assert len(ledger) == 6
    again, _ = ingest.classify(bundle, fresh)
    assert [x.ref for x in again if x.action != "reviewed"] == ["Clash / M / any firmware"]


def test_accept_all_never_takes_a_conflict(setup):
    repo, bundle_path = setup
    bundle = localdata.read_bundle(bundle_path)
    cands, _ = ingest.classify(bundle, repo)
    ingest.review(cands, accept_all=True, ask=None)
    c = by_ref(cands)
    assert c["Clash / M / any firmware"].decision is None
    assert c["tool.refused-standard-mode"].decision == "accepted"
    changed, _evidence, _ = ingest.apply(cands, bundle, bundle_path, repo, "2026-10-05")
    tool_yaml = changed[repo.root / "src/ied_client/data/hints/tool.yaml"]
    assert "title: Needs expert mode (rack wording)" in tool_yaml
    # every other entry of tool.yaml is untouched
    old = yaml.safe_load((SRC / "ied_client/data/hints/tool.yaml").read_text())["entries"]
    new = yaml.safe_load(tool_yaml)["entries"]
    assert len(new) == len(old) and [e for e in new if e["id"] != "tool.refused-standard-mode"] == [
        e for e in old if e["id"] != "tool.refused-standard-mode"]
    print(ingest.commit_message(cands, bundle, bundle_path))


def test_dry_run_main(setup, capsys):
    repo, bundle_path = setup
    assert ingest.main([str(bundle_path), "--repo-root", str(repo.root)]) == 0
    out = capsys.readouterr().out
    assert "Dry run: 7 to review" in out and "conflict" in out
