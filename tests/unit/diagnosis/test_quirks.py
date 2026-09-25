"""The quirks data file (IDN-9)."""

from __future__ import annotations

import datetime as dt

import pytest
import yaml

from ied_client.quirks import QuirkDB, QuirkInfo, QuirksError, load_quirks
from mms_protocol import protocol as mms


def builtin_quirks_text() -> str:
    """The MMS module's quirks file (PROTO-12)."""
    [(label, text)] = mms.quirks_sources()
    assert label == "mms_protocol/data/quirks.yaml"
    return text


FILE = """\
# test quirks
schema_version: 1
quirks:
  - match: {vendor: Acme, model: "BCU-5*"}
    max_associations: 4
    source: spike-1
  - match: {vendor: Acme, model: BCU-500, firmware: "2.3.*"}
    slot_release_after_abrupt_disconnect_s: 90
    resv_tms_behaviour: read-only
    source: incident-7
    added: 2026-09-01
  - match: {vendor: "acme", model: "*"}
    notes: vendor-wide note
    custom_field: kept
"""


@pytest.fixture
def qfile(tmp_path):
    p = tmp_path / "quirks.yaml"
    p.write_text(FILE)
    return p


def test_builtin_file_is_empty_but_documented():
    text = builtin_quirks_text()
    assert "max_associations" in text and "slot_release_after_abrupt_disconnect_s" in text
    doc = yaml.safe_load(text)
    assert doc == {"schema_version": 1, "quirks": None}
    db = load_quirks(sources=mms.quirks_sources())
    assert len(db) == 0 and db.sources == ["mms_protocol/data/quirks.yaml"]
    assert db.lookup("Acme", "BCU-500", "2.3.1") is None


def test_most_specific_match_wins(qfile):
    db = load_quirks([qfile])
    assert len(db) == 3 and db.sources[-1] == str(qfile)
    q = db.lookup("ACME ", "bcu-500", "2.3.1")
    assert (
        q.source == "incident-7"
        and q.slot_release_after_abrupt_disconnect_s == 90.0
        and q.max_associations is None
    )
    assert q.added == "2026-09-01" and q.origin.endswith("quirks.yaml#1")
    assert db.lookup("Acme", "BCU-500", "3.0").source == "spike-1"
    assert db.lookup("Acme", "BCU-500", None).source == "spike-1"  # firmware unknown: no firmware entries
    q = db.lookup("Acme", "Relay-9")
    assert q.notes == "vendor-wide note" and q.extra == {"custom_field": "kept"}
    assert db.lookup("Other", "BCU-500") is None
    assert db.lookup(None, None) is None
    assert [m.source for m in db.matches("Acme", "BCU-500", "2.3.1")] == ["incident-7", "spike-1", None]


def test_resolve_fills_gaps_from_less_specific_entries(qfile):
    q = load_quirks([qfile]).resolve("Acme", "BCU-500", "2.3.1")
    assert q.max_associations == 4 and q.slot_release_after_abrupt_disconnect_s == 90.0
    assert q.notes == "vendor-wide note" and q.source == "incident-7"
    assert "#1" in q.origin and "#0" in q.origin


def test_later_file_wins_at_equal_specificity(qfile, tmp_path):
    p2 = tmp_path / "exp.yaml"
    p2.write_text(
        "schema_version: 1\nquirks:\n  - match: {vendor: Acme, model: 'BCU-5*'}\n    max_associations: 2\n"
    )
    assert load_quirks([qfile, p2]).lookup("Acme", "BCU-510").max_associations == 2
    assert load_quirks([p2, qfile]).lookup("Acme", "BCU-510").max_associations == 4


@pytest.mark.parametrize(
    "bad",
    [
        "schema_version: 2\nquirks:\n",
        "quirks: {}\n",
        "- 1\n",
        "quirks:\n  - notes: no match\n",
        "quirks:\n  - match: {vendor: A}\n",
        "quirks:\n  - match: {vendor: A, model: B, serial: C}\n",
        "quirks:\n  - match: {vendor: A, model: B}\n    max_associations: 0\n",
        "quirks:\n  - match: {vendor: A, model: B}\n    slot_release_after_abrupt_disconnect_s: soon\n",
        "quirks: [\n",
    ],
)
def test_malformed_files_are_rejected(tmp_path, bad):
    p = tmp_path / "bad.yaml"
    p.write_text(bad)
    with pytest.raises(QuirksError):
        load_quirks([p])


def test_append_entry_keeps_comments_and_round_trips(qfile):
    info = QuirkDB.append_entry(
        qfile,
        {
            "match": {"vendor": "Zeta", "model": "Z1", "firmware": "1.0"},
            "max_associations": 8,
            "source": "incident-9",
        },
    )
    assert isinstance(info, QuirkInfo) and info.added == dt.date.today().isoformat()
    text = qfile.read_text()
    assert text.startswith("# test quirks") and "incident-9" in text
    db = load_quirks([qfile])
    assert len(db) == 4 and db.lookup("zeta", "z1", "1.0").max_associations == 8
    assert db.lookup("Acme", "BCU-500", "2.3.1").source == "incident-7"  # older entries untouched


def test_append_entry_to_new_file_and_to_builtin_copy(tmp_path):
    p = tmp_path / "new.yaml"
    QuirkDB.append_entry(
        p, {"match": {"vendor": "A", "model": "B"}, "notes": "x", "source": "s", "added": "2026-01-02"}
    )
    QuirkDB.append_entry(p, {"match": {"vendor": "A", "model": "C"}, "source": "s"})
    db = load_quirks([p])
    assert [q.model for q in db.entries] == ["B", "C"] and db.entries[0].added == "2026-01-02"
    copy = tmp_path / "builtin.yaml"
    copy.write_text(builtin_quirks_text())
    QuirkDB.append_entry(copy, {"match": {"vendor": "A", "model": "B"}, "source": "s"})
    assert len(load_quirks([copy])) == 1 and copy.read_text().startswith("# Known vendor")


def test_append_entry_validation(tmp_path, qfile):
    with pytest.raises(QuirksError, match="source"):
        QuirkDB.append_entry(qfile, {"match": {"vendor": "A", "model": "B"}})
    with pytest.raises(QuirksError):
        QuirkDB.append_entry(qfile, {"match": {"vendor": "A"}, "source": "s"})
    flow = tmp_path / "flow.yaml"
    flow.write_text("schema_version: 1\nquirks: []\n")
    with pytest.raises(QuirksError, match="block style"):
        QuirkDB.append_entry(flow, {"match": {"vendor": "A", "model": "B"}, "source": "s"})
    assert flow.read_text() == "schema_version: 1\nquirks: []\n"
    notlast = tmp_path / "notlast.yaml"
    notlast.write_text("quirks:\nschema_version: 1\n")
    with pytest.raises(QuirksError, match="last top-level key"):
        QuirkDB.append_entry(notlast, {"match": {"vendor": "A", "model": "B"}, "source": "s"})


def test_to_json():
    q = QuirkInfo(vendor="A", model="B", firmware="1", max_associations=3, extra={"x": 1})
    assert q.to_json() == {
        "match": {"vendor": "A", "model": "B", "firmware": "1"},
        "max_associations": 3,
        "x": 1,
    }
    assert q.describe_match() == "A B firmware 1"
