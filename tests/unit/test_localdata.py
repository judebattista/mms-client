"""Local field data (FLD-1 … FLD-6): layers, loading on top of the built-in data, bundles, ledger, prune."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from ied_client import localdata, yamledit
from ied_client.explain import HintContext, default_catalogue, local_problems, reset_default_catalogue
from ied_client.quirks import QuirkDB, load_quirks_for
from mms_protocol import codes as mms_codes
from mms_protocol import protocol as mms

HINT = """\
version: 1
entries:
- id: rack.denied-sp
  keys: [data-access:object-access-denied]
  when: {service: write, fc: SP}
  priority: 1
  hint: "On this rack the relays only accept setting writes from 10.0.0.20"
  certainty: likely
"""
QUIRK = """\
schema_version: 1
quirks:
  - match: {vendor: SimVendor, model: SimIED}
    max_associations: 2
    source: incidents/seen.json
"""


@pytest.fixture
def layers(tmp_path, monkeypatch):
    """Two local layers (machine, user) in tmp_path; the default catalogue reloaded around the test."""
    machine, user = tmp_path / "etc", tmp_path / "home"
    monkeypatch.setenv("IED_CLIENT_LOCAL_DIRS", f"{machine}:{user}")
    reset_default_catalogue()
    yield machine, user
    reset_default_catalogue()


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------------- layers
def test_layers_default_to_etc_and_xdg_config(monkeypatch, tmp_path):
    monkeypatch.delenv("IED_CLIENT_LOCAL_DIRS", raising=False)
    monkeypatch.delenv("MMS_CLIENT_LOCAL_DIRS", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    ls = localdata.layers()
    assert [(ly.name, ly.path) for ly in ls] == [("machine", Path("/etc/ied-client")), ("user", tmp_path / "cfg" / "ied-client")]
    assert localdata.layer().name == "user" and localdata.layer(system=True).name == "machine"


def test_files_in_load_order(layers):
    machine, user = layers
    write(user / "hints" / "b.yaml", "version: 1\n")
    write(user / "hints" / "a.yaml", "version: 1\n")
    write(machine / "hints" / "z.yaml", "version: 1\n")
    write(user / "quirks" / "mms" / "local.yaml", QUIRK)
    write(user / "quirks" / "other" / "local.yaml", QUIRK)
    assert [f.label for f in localdata.local_files("hints")] == ["machine:hints/z.yaml", "user:hints/a.yaml", "user:hints/b.yaml"]
    assert localdata.quirks_files("mms") == [user / "quirks" / "mms" / "local.yaml"]


# ---------------------------------------------------------------------------------- loading
def test_local_hint_is_used_and_marked(layers):
    _, user = layers
    path = write(user / "hints" / "local.yaml", HINT)
    cat = default_catalogue()
    hint = cat.hint(mms_codes.data_access_error(3), HintContext(service="write", fc="SP"))
    assert hint.entry_id == "rack.denied-sp" and hint.source == str(path)
    assert hint.render().startswith("[local] Likely cause: On this rack")
    exp = cat.explain("rack.denied-sp")
    assert exp.source == str(path) and exp.to_json()["source"] == str(path)
    # built-in entries are unchanged, and say nothing about a source
    builtin = cat.explain("XCBR")
    assert builtin.source is None and "source" not in builtin.to_json()


def test_invalid_local_file_is_left_out_not_fatal(layers):
    _, user = layers
    write(user / "hints" / "a-broken.yaml", "version: 1\nentries:\n- id: Bad Id\n")
    write(user / "hints" / "b-good.yaml", HINT)
    cat = default_catalogue()
    assert "rack.denied-sp" in cat and cat.explain("XCBR") is not None
    problems = local_problems()
    assert len(problems) == 1 and "a-broken.yaml" in problems[0]


def test_local_quirks_layer_over_builtin(layers):
    _, user = layers
    write(user / "quirks" / "mms" / "local.yaml", QUIRK)
    write(user / "quirks" / "mms" / "broken.yaml", "quirks: 3\n")
    db = load_quirks_for(mms)
    info = db.resolve("SimVendor", "SimIED", "1.0")
    assert info.max_associations == 2 and info.local
    assert len(db.problems) == 1 and "broken.yaml" in db.problems[0]


def test_append_entry_makes_a_readable_file(layers):
    _, user = layers
    path = user / "quirks" / "mms" / "local.yaml"
    QuirkDB.append_entry(path, {"match": {"vendor": "V", "model": "M"}, "notes": "n", "source": "s"})
    assert path.stat().st_mode & 0o777 == 0o644  # the machine layer is read by every user


# ---------------------------------------------------------------------------------- yamledit
def test_yamledit_keeps_every_other_byte():
    text = (Path(__file__).parents[2] / "src/ied_client/data/hints/tool.yaml").read_text()
    items = yamledit.items(text, "entries")
    original = yaml.safe_load(text)["entries"]
    removed = yamledit.remove(text, "entries", [1])
    assert yaml.safe_load(removed)["entries"] == original[:1] + original[2:]
    assert removed.startswith(text[: sum(len(ln) for ln in text.splitlines(True)[: items[1].start])])
    changed = dict(original[2], title="Changed")
    replaced = yamledit.replace(text, "entries", 2, yamledit.dump_item(changed))
    assert yaml.safe_load(replaced)["entries"] == [*original[:2], changed, *original[3:]]
    appended = yamledit.append(text, "entries", [yamledit.dump_item({"id": "x.y"})])
    assert appended.startswith(text) and yaml.safe_load(appended)["entries"][-1] == {"id": "x.y"}
    # the item's own text, moved to column 0, parses back to the item
    assert yaml.safe_load(yamledit.item_text(text, items[3]))[0] == original[3]


def test_yamledit_empty_and_flow_lists():
    assert yamledit.append("version: 1\nentries: []\n", "entries", ["- id: a\n"]) == "version: 1\nentries:\n- id: a\n"
    assert yaml.safe_load(yamledit.append("schema_version: 1\nquirks:\n", "quirks", ["- n: 1\n", "- n: 2\n"],
                                          default_indent=2))["quirks"] == [{"n": 1}, {"n": 2}]
    assert yamledit.append("a: 1\n", "entries", ["- id: a\n"]).endswith("entries:\n- id: a\n")
    with pytest.raises(yamledit.YamlEditError):
        yamledit.items("entries: [{id: a}]\n", "entries")


# ---------------------------------------------------------------------------------- fingerprint, ledger, status
def test_fingerprint_is_about_content_only():
    a = yaml.safe_load("id: x\nkeys: [tool:foo]\n")
    b = yaml.safe_load("# a comment\nkeys:   [tool:foo]\nid: x\n")
    assert localdata.fingerprint(a) == localdata.fingerprint(b)
    assert localdata.fingerprint(a) != localdata.fingerprint({**a, "id": "y"})


def test_status_classes_and_prune(layers, monkeypatch):
    _, user = layers
    hints = write(user / "hints" / "local.yaml", HINT + """\
- id: tool.refused-standard-mode
  keys: [tool:refused-standard-mode]
  title: Needs expert mode here too
  summary: A local rewording of a built-in entry.
  hint: "Local wording"
  certainty: fact
- id: rack.other
  terms: [rack thing]
  title: Rack thing
  summary: Something about this rack.
""")
    write(user / "quirks" / "mms" / "local.yaml", QUIRK)
    entries = localdata.raw_entries(hints.read_text(), "hints")
    quirk = localdata.raw_entries(QUIRK, "quirks")[0]
    ledger_text = yamledit.append(
        "schema_version: 1\nreviewed:\n", "reviewed",
        [yamledit.dump_item({"fingerprint": localdata.fingerprint(entries[0]), "kind": "hint", "ref": "rack.denied-sp",
                             "decision": "accepted", "reviewed": "2026-10-05", "bundle": "b.json"}),
         yamledit.dump_item({"fingerprint": localdata.fingerprint(quirk), "kind": "quirk", "ref": "q",
                             "decision": "rejected", "reason": "seen once only"})])
    monkeypatch.setattr(localdata, "ledger", lambda: localdata.parse_ledger(ledger_text))
    st = {e.ref: e for fs in localdata.status() for e in fs.entries}
    assert st["rack.denied-sp"].state == localdata.INGESTED and "b.json" in st["rack.denied-sp"].detail
    assert st["tool.refused-standard-mode"].state == localdata.OVERRIDES
    assert st["rack.other"].state == localdata.LOCAL_ONLY
    assert st["SimVendor / SimIED / any firmware"].state == localdata.REJECTED
    assert st["SimVendor / SimIED / any firmware"].detail == "seen once only"

    plan = localdata.prune_plan()
    assert sum(len(es) for _, es in plan) == 2
    assert localdata.apply_prune(plan) == 2
    left = [e["id"] for e in localdata.raw_entries(hints.read_text(), "hints")]
    assert left == ["tool.refused-standard-mode", "rack.other"]
    assert localdata.raw_entries((user / "quirks" / "mms" / "local.yaml").read_text(), "quirks") == []


def test_builtin_ledger_loads():
    assert isinstance(localdata.ledger(), dict)


# ---------------------------------------------------------------------------------- bundles
def test_bundle_round_trip_and_import(layers, tmp_path, monkeypatch):
    machine, user = layers
    write(user / "hints" / "local.yaml", HINT)
    write(user / "quirks" / "mms" / "local.yaml", QUIRK)
    write(user / "incidents" / "seen.json", '{"kind": "ied-client-incident"}\n')
    bundle = localdata.build_bundle()
    assert bundle["kind"] == "ied-client-field-data" and len(bundle["files"]) == 2
    assert [i["path"] for i in bundle["incidents"]] == ["incidents/seen.json"]
    assert {e["ref"] for e in bundle["entries"]} == {"rack.denied-sp", "SimVendor / SimIED / any firmware"}
    path = tmp_path / "b.json"
    path.write_text(json.dumps(bundle))
    doc = localdata.read_bundle(path)

    # another laptop: an empty user layer
    other = tmp_path / "other"
    monkeypatch.setenv("IED_CLIENT_LOCAL_DIRS", f"{machine}:{other}")
    written = localdata.import_bundle(doc)
    names = sorted(p.relative_to(other).as_posix() for p in written)
    assert names[0].startswith("hints/imported-") and names[1] == "incidents/seen.json"
    assert names[2].startswith("quirks/mms/imported-")
    assert all(fs.valid for fs in localdata.status())

    # a damaged bundle is refused
    bundle["files"][0]["text"] += "# changed\n"
    path.write_text(json.dumps(bundle))
    with pytest.raises(localdata.LocalDataError, match="checksum"):
        localdata.read_bundle(path)
