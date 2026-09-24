"""`explain` lookups (EXP-2), search, JSON and plain-text rendering."""

from __future__ import annotations

import json

import pytest

from mms_client import codes
from mms_client.explain import Catalogue, HintContext, default_catalogue, render_explanation
from mms_client.explain.render import render, render_search


@pytest.fixture(scope="module")
def cat() -> Catalogue:
    return default_catalogue()


def test_default_catalogue_is_cached() -> None:
    assert default_catalogue() is default_catalogue()


@pytest.mark.parametrize(
    ("query", "entry_id"),
    [
        # error keys, by name and by number, with domain aliases
        ("data-access:object-access-denied", "data-access.object-access-denied"),
        ("ied:access-denied", "data-access.object-access-denied"),
        ("ied:21", "data-access.object-access-denied"),
        ("mms:83", "data-access.object-access-denied"),
        ("add-cause:10", "add-cause.blocked-by-interlocking"),
        ("ADD-CAUSE:10", "add-cause.blocked-by-interlocking"),
        ("AddCause:2", "add-cause.blocked-by-switching-hierarchy"),
        ("add-cause:Blocked_By_Interlocking", "add-cause.blocked-by-interlocking"),
        ("ctl-error:3", "ctl-error.operator-test-not-ok"),
        ("LastApplError:2", "ctl-error.timeout-test-not-ok"),
        ("acse-diag:14", "acse-diag.authentication"),
        ("association:tcp-refused", "association.tcp-refused"),
        ("tool:local-address-missing", "tool.local-address-missing"),
        ("check:confrev-mismatch", "check.confrev-mismatch"),
        # libiec61850 constant names
        ("IED_ERROR_ACCESS_DENIED", "data-access.object-access-denied"),
        ("DATA_ACCESS_ERROR_OBJECT_NONE_EXISTENT", "data-access.object-non-existent"),
        ("MMS_ERROR_FILE_FILE_BUSY", "mms.file-file-busy"),
        ("ADD_CAUSE_1_OF_N_CONTROL", "add-cause.1-of-n-control"),
        # bare names
        ("object-access-denied", "data-access.object-access-denied"),
        ("blocked-by-interlocking", "add-cause.blocked-by-interlocking"),
        ("tcp-refused", "association.tcp-refused"),
        ("authentication-required", "acse-diag.authentication"),
        # terms, any case and spelling
        ("XCBR", "ln.xcbr"),
        ("xcbr", "ln.xcbr"),
        ("dpc", "cdc.dpc"),
        ("ST", "fc.st"),
        ("ctlModel", "control.ctlmodel"),
        ("ctl model", "control.ctlmodel"),
        ("sbo-with-enhanced-security", "ctlmodel.sbo-with-enhanced-security"),
        ("SBO with enhanced security", "ctlmodel.sbo-with-enhanced-security"),
        ("ResvTms", "rcb.resvtms"),
        ("resvtms", "rcb.resvtms"),
        ("switching hierarchy", "concept.switching-hierarchy"),
        ("orCat=3", "orcat.remote-control"),
        ("remote", "orcat.remote-control"),
        ("Ed2.1", "edition.ed2-1"),
        ("goose", "scope.goose"),
        # entry ids
        ("data-access.object-access-denied.write-status", "data-access.object-access-denied.write-status"),
        ("LN.XCBR", "ln.xcbr"),
        # logical node names and object references
        ("Q0XCBR1", "ln.xcbr"),
        ("PROT_PTOC2", "ln.ptoc"),
        ("CTRL/CSWI1.Pos.Oper", "control.oper"),
        ("CTRL/CSWI1.Pos", "ln.cswi"),
    ],
)
def test_explain_lookup(cat: Catalogue, query: str, entry_id: str) -> None:
    exp = cat.explain(query)
    assert exp is not None, query
    assert exp.id == entry_id


def test_explain_returns_none_for_unknown_terms(cat: Catalogue) -> None:
    assert cat.explain("definitely-not-a-term") is None
    assert cat.explain("") is None
    assert cat.explain("add-cause:no-such-cause") is None


def test_ambiguous_bare_name_prefers_data_access_and_lists_the_others(cat: Catalogue) -> None:
    exp = cat.explain("type-inconsistent")
    assert exp is not None and exp.hint is not None
    assert exp.hint.key == "data-access:type-inconsistent"
    assert "ied:type-inconsistent" in exp.related

    unknown = cat.explain("unknown")
    assert unknown is not None and unknown.hint is not None
    assert unknown.hint.key == "data-access:unknown"
    for other in ("ied:unknown", "add-cause:unknown", "ctl-error:unknown", "association:unknown"):
        assert other in unknown.related

    # ied before mms
    lost = cat.explain("connection-lost")
    assert lost is not None and lost.hint is not None and lost.hint.key == "ied:connection-lost"
    assert "mms:connection-lost" in lost.related

    # "none" exists in mms and add-cause: mms comes first
    none = cat.explain("none")
    assert none is not None and none.hint is not None and none.hint.key == "mms:none"
    assert "add-cause:none" in none.related


def test_explain_with_context_picks_the_variant_and_inherits_the_rest(cat: Catalogue) -> None:
    ctx = HintContext(service="read", fc="SP")
    exp = cat.explain("data-access:object-access-denied", ctx)
    base = cat.explain("data-access:object-access-denied")
    assert exp is not None and base is not None
    assert exp.id == "data-access.object-access-denied.read"
    # the read variant only has a hint: title/summary/details/next checks come from the base entry
    assert exp.title.startswith(base.title)
    assert exp.summary == base.summary and exp.details == base.details
    assert exp.next_checks == base.next_checks
    assert "data-access.object-access-denied" in exp.related
    assert exp.hint is not None and exp.hint.entry_id == exp.id


def test_explain_error_has_keys_and_hint(cat: Catalogue) -> None:
    exp = cat.explain("add-cause:blocked-by-switching-hierarchy")
    assert exp is not None
    assert exp.kind == "error"
    assert exp.keys == ("add-cause:blocked-by-switching-hierarchy",)
    assert exp.hint is not None and exp.hint.render().startswith("Likely cause: ")
    assert exp.next_checks and any("authority-probe" in c for c in exp.next_checks)


def test_explanations_for_all_error_codes_give_next_checks(cat: Catalogue) -> None:
    missing = []
    for table, make in (
        (codes.IED_ERRORS, codes.ied_error),
        (codes.DATA_ACCESS_ERRORS, codes.data_access_error),
        (codes.ADD_CAUSES, codes.add_cause),
    ):
        for n in table:
            err = make(n)
            if err.name in ("ok", "none"):
                continue
            exp = cat.explain(err.key)
            if exp is None or not exp.next_checks:
                missing.append(err.key)
    assert missing == []


def test_search_finds_near_misses(cat: Catalogue) -> None:
    assert cat.search("interlok")[0].id == "control.interlocking"
    ids = [e.id for e in cat.search("buffer overflow", limit=5)]
    assert "rcb.bufovfl" in ids
    assert len(cat.search("report", limit=3)) == 3
    assert cat.search("") == []


def test_vocabulary_lists_ids_terms_and_keys(cat: Catalogue) -> None:
    words = cat.vocabulary()
    for w in ("XCBR", "ln.xcbr", "add-cause:blocked-by-interlocking", "ResvTms"):
        assert w in words
    assert not any(w.endswith(":*") for w in words)


def test_explanation_to_json_is_serialisable(cat: Catalogue) -> None:
    exp = cat.explain("ied:access-denied", HintContext(service="write", fc="ST"))
    assert exp is not None
    data = json.loads(json.dumps(exp.to_json()))
    assert data["id"] == "data-access.object-access-denied.write-status"
    assert data["kind"] == "error"
    assert data["hint"]["certainty"] == "fact"
    assert data["hint"]["key"] == "ied:access-denied"
    assert "ied:access-denied" in data["keys"]
    term = cat.explain("DPC")
    assert term is not None and term.to_json()["hint"] is None


def test_render_explanation_has_all_sections(cat: Catalogue) -> None:
    exp = cat.explain("add-cause:10")
    assert exp is not None
    text = render_explanation(exp)
    assert text.splitlines()[0].startswith("AddCause: blocked by interlocking")
    assert "Codes: add-cause:blocked-by-interlocking (10)" in text
    assert "Hint: Likely cause: " in text
    for section in ("Summary", "Details", "Next checks", "Related:"):
        assert section in text
    assert "  1. " in text
    assert all(len(line) <= 88 or "`" in line for line in text.splitlines())
    assert render(exp) == text


def test_render_term_and_search(cat: Catalogue) -> None:
    text = render(cat.explain("XCBR"))  # type: ignore[arg-type]
    assert "Codes:" not in text and "Hint:" not in text and "Summary" in text
    listing = render_search(cat.search("tap changer"), "tap changer")
    assert "ln.yltc" in listing
    assert "Nothing in the catalogue" in render_search([], "zzz")
