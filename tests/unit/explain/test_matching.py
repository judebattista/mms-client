"""Context matching (EXP-3), certainty (EXP-4) and hint rendering (EXP-1)."""

from __future__ import annotations

import textwrap

import pytest

from ied_client import codes
from ied_client.explain import Catalogue, Certainty, Hint, HintContext, default_catalogue
from mms_protocol import codes as mms_codes


@pytest.fixture(scope="module")
def cat() -> Catalogue:
    return default_catalogue()


# ---------------------------------------------------------------------------------------------------
# The spec's example: object-access-denied on FC=ST vs FC=SP, and the IED-level twin
# ---------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "error",
    [mms_codes.data_access_error(3), mms_codes.ied_error(21), mms_codes.mms_error(83)],
    ids=lambda e: e.key,
)
def test_access_denied_on_status_fc_is_a_fact_not_writable_by_design(cat: Catalogue, error) -> None:
    hint = cat.hint(error, HintContext(service="write", fc="ST"))
    assert hint is not None
    assert hint.key == error.key
    assert hint.certainty is Certainty.FACT
    assert "read-only by design" in hint.text
    assert hint.render() == hint.text  # facts are shown as they are
    assert "FC=ST" in hint.text


@pytest.mark.parametrize(
    "error",
    [mms_codes.data_access_error(3), mms_codes.ied_error(21), mms_codes.mms_error(83)],
    ids=lambda e: e.key,
)
def test_access_denied_on_setting_fc_says_check_access_rights_and_source_ip(cat: Catalogue, error) -> None:
    hint = cat.hint(error, HintContext(service="write", fc="SP"))
    assert hint is not None
    assert hint.certainty is Certainty.CHECK
    assert hint.render().startswith("Check: ")
    assert "access rights" in hint.text and "source-IP" in hint.text
    assert "FC=SP" in hint.text


def test_access_denied_on_measurement_is_also_by_design(cat: Catalogue) -> None:
    hint = cat.hint(mms_codes.ied_error(21), HintContext(service="write", fc="MX", mode="expert"))
    assert hint is not None and hint.certainty is Certainty.FACT and "FC=MX" in hint.text


def test_access_denied_on_a_control_points_to_orcat_and_authority(cat: Catalogue) -> None:
    hint = cat.hint(mms_codes.ied_error(21), HintContext(service="operate", cdc="DPC", ctl_model=4))
    assert hint is not None and hint.certainty is Certainty.LIKELY
    assert "orCat" in hint.text and "authority-probe" in hint.text


def test_unknown_context_never_produces_a_fact(cat: Catalogue) -> None:
    # service known, FC unknown: the FC=ST fact must not be claimed
    hint = cat.hint(mms_codes.data_access_error(3), HintContext(service="write"))
    assert hint is not None and hint.certainty is not Certainty.FACT
    assert hint.entry_id == "data-access.object-access-denied"
    assert cat.hint(mms_codes.data_access_error(3)).entry_id == "data-access.object-access-denied"  # type: ignore[union-attr]


def test_fc_in_context_is_case_insensitive(cat: Catalogue) -> None:
    hint = cat.hint(mms_codes.data_access_error(3), HintContext(service="WRITE", fc="st"))
    assert hint is not None and hint.certainty is Certainty.FACT and "FC=ST" in hint.text


# ---------------------------------------------------------------------------------------------------
# Edition-dependent check
# ---------------------------------------------------------------------------------------------------
def test_locsta_missing_is_not_an_issue_on_ed1(cat: Catalogue) -> None:
    hint = cat.hint(codes.check("locsta-missing"), HintContext(edition="Ed1"))
    assert hint is not None and hint.entry_id == "check.locsta-missing.ed1"
    assert hint.certainty is Certainty.FACT and "not an issue" in hint.text


@pytest.mark.parametrize("edition", ["Ed2", "Ed2.1", "ed2"])
def test_locsta_missing_needs_checking_on_ed2(cat: Catalogue, edition: str) -> None:
    hint = cat.hint(codes.check("locsta-missing"), HintContext(edition=edition))
    assert hint is not None and hint.entry_id == "check.locsta-missing.ed2"
    assert hint.certainty is Certainty.CHECK


def test_locsta_missing_with_unknown_edition(cat: Catalogue) -> None:
    hint = cat.hint(codes.check("locsta-missing"), HintContext(edition="unknown"))
    assert hint is not None and hint.entry_id == "check.locsta-missing.unknown"
    assert cat.hint("check:locsta-missing").entry_id == "check.locsta-missing"  # type: ignore[union-attr]


# ---------------------------------------------------------------------------------------------------
# Other context-dependent hints
# ---------------------------------------------------------------------------------------------------
def test_temporarily_unavailable_on_rcb_points_to_the_owner(cat: Catalogue) -> None:
    hint = cat.hint(mms_codes.data_access_error(2), HintContext(service="set-rcb", fc="BR"))
    assert hint is not None and "rcb" in hint.text and "Owner" in hint.text
    assert hint.certainty is Certainty.LIKELY


def test_type_inconsistent_on_write_mentions_the_model(cat: Catalogue) -> None:
    hint = cat.hint(mms_codes.ied_error(25), HintContext(service="write", fc="SP"))
    assert hint is not None and hint.entry_id == "data-access.type-inconsistent.write"
    assert "model" in hint.text


def test_time_limit_over_depends_on_the_step(cat: Catalogue) -> None:
    after_select = cat.hint(codes.add_cause(16), HintContext(service="operate", ctl_model=2))
    termination = cat.hint(codes.add_cause(16), HintContext(service="command-termination", ctl_model=4))
    assert after_select is not None and "sboTimeout" in after_select.text
    assert termination is not None and "execution time" in termination.text
    # ctlModel given as a number or as its name gives the same result
    by_name = cat.hint(
        codes.add_cause(16), HintContext(service="operate", ctl_model="sbo-with-normal-security")
    )
    assert by_name == after_select


def test_select_refused_on_sbo_normal_has_no_reason(cat: Catalogue) -> None:
    """The core reports an empty SBO answer (ied ok, no AddCause) as add-cause:select-failed."""
    ctx = HintContext(service="select", cdc="DPC", ctl_model="sbo-with-normal-security", mode="standard")
    hint = cat.hint(codes.add_cause(3), ctx)
    assert hint is not None and hint.entry_id == "add-cause.select-failed.sbo-normal"
    assert hint.certainty is Certainty.CHECK and "no reason" in hint.text
    exp = cat.explain("add-cause:select-failed", ctx)
    assert exp is not None and "empty value" in exp.summary


def test_blocked_by_switching_hierarchy_uses_the_orcat_tag(cat: Catalogue) -> None:
    remote = cat.hint(codes.add_cause(2), HintContext(service="operate", tags={"or_cat": "remote-control"}))
    bay = cat.hint(codes.add_cause(2), HintContext(service="operate", tags={"or_cat": "bay-control"}))
    assert remote is not None and "LocSta" in remote.text
    assert bay is not None and bay.entry_id.endswith(".bay")


def test_mode_changes_the_rcb_owned_hint(cat: Catalogue) -> None:
    standard = cat.hint(codes.tool("rcb-owned-by-other-client"), HintContext(mode="standard"))
    expert = cat.hint(codes.tool("rcb-owned-by-other-client"), HintContext(mode="expert"))
    assert standard is not None and "expert mode" in standard.text
    assert expert is not None and "--takeover" in expert.text


def test_abrupt_disconnect_pattern_outranks_ip_advice(cat: Catalogue) -> None:
    ctx = HintContext(
        service="associate", tags={"pattern": "refused-after-abrupt-disconnect", "bound_ip": "no"}
    )
    hint = cat.hint(codes.association("tcp-closed-immediately"), ctx)
    assert hint is not None and hint.entry_id == "association.refusal.stale-association"
    assert hint.certainty is Certainty.LIKELY
    plain = cat.hint(codes.association("tcp-closed-immediately"), HintContext(tags={"bound_ip": "no"}))
    assert plain is not None and "--as <client>" in plain.text


def test_unknown_numbers_fall_back_to_a_domain_entry(cat: Catalogue) -> None:
    hint = cat.hint(mms_codes.ied_error(4242))
    assert hint is not None and hint.key == "ied:unknown-4242" and hint.entry_id == "code.unlisted"
    tool_hint = cat.hint(codes.tool("something-new"))
    assert tool_hint is not None and tool_hint.entry_id == "tool.unlisted"
    assert cat.hint(codes.check("new-check")).entry_id == "check.unlisted"  # type: ignore[union-attr]


def test_hint_accepts_error_info_key_string_and_numbers(cat: Catalogue) -> None:
    a = cat.hint(codes.add_cause(10))
    b = cat.hint("add-cause:blocked-by-interlocking")
    c = cat.hint("add-cause:10")
    assert a == b == c and a is not None and a.key == "add-cause:blocked-by-interlocking"
    assert cat.hint("not a code at all") is None


# ---------------------------------------------------------------------------------------------------
# Certainty and rendering
# ---------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("certainty", "rendered"),
    [
        (Certainty.FACT, "The attribute is read-only by design"),
        (Certainty.LIKELY, "Likely cause: The attribute is read-only by design"),
        (Certainty.CHECK, "Check: The attribute is read-only by design"),
    ],
)
def test_hint_render_prefix(certainty: Certainty, rendered: str) -> None:
    hint = Hint(
        key="ied:access-denied",
        entry_id="x",
        text="The attribute is read-only by design",
        certainty=certainty,
    )
    assert hint.render() == rendered
    assert str(hint) == rendered
    data = hint.to_json()
    assert data == {
        "key": "ied:access-denied",
        "entry_id": "x",
        "certainty": certainty.value,
        "text": "The attribute is read-only by design",
        "rendered": rendered,
    }


def test_every_likely_and_check_hint_is_rendered_with_its_prefix(cat: Catalogue) -> None:
    for entry in cat.entries:
        if not entry.keys or entry.keys[0].endswith(":*"):
            continue
        hint = cat.hint(entry.keys[0])
        assert hint is not None
        if hint.certainty is Certainty.LIKELY:
            assert hint.render().startswith("Likely cause: ")
        elif hint.certainty is Certainty.CHECK:
            assert hint.render().startswith("Check: ")


def test_placeholders_are_filled_from_tags_or_shown_as_names(cat: Catalogue) -> None:
    ctx = HintContext(tags={"local_ip": "10.1.2.3", "interface": "eth1", "prefix": "24"})
    hint = cat.hint(codes.tool("local-address-missing"), ctx)
    assert hint is not None and "sudo ip addr add 10.1.2.3/24 dev eth1" in hint.text
    bare = cat.hint(codes.tool("local-address-missing"))
    assert bare is not None and "{" not in bare.text and "<local_ip>" in bare.text


def test_context_is_hashable_and_serialisable() -> None:
    ctx = HintContext(service="write", fc="sp", tags={"bound_ip": "yes"}, ctl_model=4)
    assert ctx.fc == "SP" and ctx.ctl_model == "sbo-with-enhanced-security"
    hash(ctx)
    assert ctx.to_json() == {
        "service": "write",
        "fc": "SP",
        "ctl_model": "sbo-with-enhanced-security",
        "tags": {"bound_ip": "yes"},
    }


# ---------------------------------------------------------------------------------------------------
# The specificity rule itself, on a synthetic catalogue
# ---------------------------------------------------------------------------------------------------
SYNTHETIC = textwrap.dedent(
    """
    version: 1
    entries:
      - id: wild
        keys: ["ied:*"]
        title: Anything
        summary: Fallback.
        hint: "wildcard"
        certainty: check
      - id: base
        keys: [ied:access-denied]
        title: Base
        summary: Base entry.
        hint: "base"
        certainty: check
      - id: wild-specific
        keys: ["ied:*"]
        when: {service: write, fc: ST, mode: standard}
        hint: "wildcard with three conditions"
        certainty: check
      - id: one
        keys: [ied:access-denied]
        when: {service: write}
        hint: "one condition"
        certainty: likely
      - id: two
        keys: [ied:access-denied]
        when: {service: write, fc: [ST, MX]}
        hint: "two conditions"
        certainty: fact
      - id: tag-a
        keys: [ied:access-denied]
        when: {tags: {pattern: x}}
        hint: "tag a"
        certainty: check
      - id: tag-b
        keys: [ied:access-denied]
        when: {tags: {bound_ip: "yes"}}
        hint: "tag b (later, same specificity)"
        certainty: check
      - id: boosted
        keys: [ied:access-denied]
        when: {mode: expert}
        priority: 5
        hint: "priority wins"
        certainty: check
    """
)


@pytest.fixture()
def synthetic() -> Catalogue:
    return Catalogue.from_texts([("synthetic.yaml", SYNTHETIC, False)])


def test_rule_more_conditions_win(synthetic: Catalogue) -> None:
    assert synthetic.hint("ied:access-denied").entry_id == "base"  # type: ignore[union-attr]
    assert synthetic.hint("ied:access-denied", HintContext(service="write")).entry_id == "one"  # type: ignore[union-attr]
    assert synthetic.hint("ied:access-denied", HintContext(service="write", fc="MX")).entry_id == "two"  # type: ignore[union-attr]


def test_rule_exact_key_beats_wildcard_even_with_fewer_conditions(synthetic: Catalogue) -> None:
    ctx = HintContext(service="write", fc="ST", mode="standard")
    assert synthetic.hint("ied:access-denied", ctx).entry_id == "two"  # type: ignore[union-attr]
    assert synthetic.hint("ied:timeout", ctx).entry_id == "wild-specific"  # type: ignore[union-attr]
    assert synthetic.hint("ied:timeout").entry_id == "wild"  # type: ignore[union-attr]


def test_rule_ties_go_to_the_later_entry_and_lint_reports_them(synthetic: Catalogue) -> None:
    ctx = HintContext(tags={"pattern": "x", "bound_ip": "yes"})
    assert synthetic.hint("ied:access-denied", ctx).entry_id == "tag-b"  # type: ignore[union-attr]
    assert any("'tag-a' and 'tag-b'" in p for p in synthetic.lint())


def test_rule_priority_comes_first(synthetic: Catalogue) -> None:
    ctx = HintContext(service="write", fc="ST", mode="expert")
    assert synthetic.hint("ied:access-denied", ctx).entry_id == "boosted"  # type: ignore[union-attr]
