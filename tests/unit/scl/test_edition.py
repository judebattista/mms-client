"""edition_of(): IDN-2 source 2 (SCL), including per-IED original versions in mixed SCDs."""

from __future__ import annotations

import pytest

from ied_client.scl import SclDocument, SclError, edition_of, edition_of_version, expand_server, load_scl

from ._data import bcu_text


@pytest.mark.parametrize(
    ("version", "revision", "release", "edition"),
    [
        (None, None, None, "Ed1"),
        ("2003", None, None, "Ed1"),
        ("2007", "A", None, "Ed2"),
        ("2007", None, None, "Ed2"),
        ("2007", "B", None, "Ed2"),
        ("2007", "B", "1", "Ed2"),
        ("2007", "B", "4", "Ed2.1"),
        ("2007", "B", 5, "Ed2.1"),
        ("2007", "C", "5", "Ed2.1"),
    ],
)
def test_edition_of_version(version, revision, release, edition) -> None:
    assert edition_of_version(version, revision, release) == edition


def test_document_editions(bcu_doc: SclDocument, ed1_doc: SclDocument) -> None:
    ed2 = edition_of(bcu_doc, "BCU1")
    assert (ed2.edition, ed2.scl_version, ed2.source) == ("Ed2", "2007B", "document")
    assert "2007B" in ed2.reason
    assert str(ed2) == "Ed2"
    ed1 = edition_of(ed1_doc, "TEMPLATE")
    assert (ed1.edition, ed1.scl_version, ed1.source) == ("Ed1", "2003", "document")
    assert "no schema version" in ed1.reason


def test_mixed_scd_uses_each_ieds_original_version(rack_doc: SclDocument) -> None:
    assert rack_doc.scl_version == "2007B4"
    sm1 = edition_of(rack_doc, "SM1")
    bcu = edition_of(rack_doc, "BCU1")
    ied3 = edition_of(rack_doc, "IED3")
    assert (sm1.edition, sm1.scl_version, sm1.source) == ("Ed2.1", "2007B4", "ied-original-scl")
    assert (bcu.edition, bcu.scl_version) == ("Ed2", "2007B")
    assert (ied3.edition, ied3.scl_version) == ("Ed1", "2003")
    assert "originalSclVersion 2003" in ied3.reason and "2007B4 file" in ied3.reason


def test_ied_without_original_version_in_ed21_file() -> None:
    text = bcu_text().replace('version="2007" revision="B">', 'version="2007" revision="B" release="4">')
    doc = load_scl(text)
    info = edition_of(doc, "BCU1")
    assert (info.edition, info.source) == ("Ed2.1", "document")


def test_edition_is_on_the_expanded_server(rack_doc: SclDocument) -> None:
    assert expand_server(rack_doc, "IED3").edition.edition == "Ed1"


def test_unknown_ied(bcu_doc: SclDocument) -> None:
    with pytest.raises(SclError, match="IED 'X' not found"):
        edition_of(bcu_doc, "X")
