"""YAML loading, validation (EXP-5) and layering of extra catalogue files."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from mms_client import codes
from mms_client.explain import Catalogue, CatalogueError, Certainty, HintContext


def _write(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------------------------------
# Layering
# ---------------------------------------------------------------------------------------------------
def test_extra_file_overrides_an_entry_by_id(tmp_path: Path) -> None:
    extra = _write(
        tmp_path,
        "rack.yaml",
        """
        version: 1
        entries:
          - id: add-cause.blocked-by-interlocking
            keys: [add-cause:blocked-by-interlocking]
            title: Interlocking (rack 7)
            summary: On rack 7 the interlocking inputs come from the BCU in bay 2 over GOOSE.
            hint: "Check the bay 2 BCU GOOSE publisher first"
            certainty: check
        """,
    )
    base = Catalogue.load()
    layered = Catalogue.load(extra_paths=[extra])
    assert len(layered) == len(base)  # replaced, not added
    hint = layered.hint(codes.add_cause(10))
    assert hint is not None and hint.text == "Check the bay 2 BCU GOOSE publisher first"
    exp = layered.explain("add-cause:10")
    assert exp is not None and exp.title == "Interlocking (rack 7)" and exp.next_checks == ()
    # the built-in context variant still exists and still wins in its context
    expert = layered.hint(codes.add_cause(10), HintContext(mode="expert"))
    assert expert is not None and expert.entry_id == "add-cause.blocked-by-interlocking.expert"


def test_extra_file_adds_a_more_specific_variant_and_later_files_win(tmp_path: Path) -> None:
    first = _write(
        tmp_path,
        "a.yaml",
        """
        version: 1
        entries:
          - id: rack.denied-sp
            keys: [data-access:object-access-denied]
            when: {service: write, fc: SP}
            hint: "first file"
            certainty: check
        """,
    )
    second = _write(
        tmp_path,
        "b.yaml",
        """
        version: 1
        entries:
          - id: rack.denied-sp
            keys: [data-access:object-access-denied]
            when: {service: write, fc: SP}
            hint: "On this rack the relays only accept setting writes from 10.0.0.20"
            certainty: likely
        """,
    )
    cat = Catalogue.load(extra_paths=[first, second])
    hint = cat.hint(codes.data_access_error(3), HintContext(service="write", fc="SP"))
    assert hint is not None and hint.entry_id == "rack.denied-sp"
    assert hint.render() == "Likely cause: On this rack the relays only accept setting writes from 10.0.0.20"
    # the layered variant ties with the built-in one on specificity and wins by position
    assert any("rack.denied-sp" in p for p in cat.lint())


def test_priority_lets_an_experiment_file_win_everywhere(tmp_path: Path) -> None:
    extra = _write(
        tmp_path,
        "prio.yaml",
        """
        version: 1
        entries:
          - id: rack.ip-filter
            keys: [data-access:object-access-denied, ied:access-denied]
            priority: 10
            title: Rack IP filter
            summary: All relays on this rack filter by source IP.
            hint: "All relays here filter by source IP; use `--as <client>`"
            certainty: likely
        """,
    )
    cat = Catalogue.load(extra_paths=[extra])
    hint = cat.hint(codes.ied_error(21), HintContext(service="write", fc="ST"))
    assert hint is not None and hint.entry_id == "rack.ip-filter"


def test_extra_file_may_shadow_a_builtin_term(tmp_path: Path) -> None:
    extra = _write(
        tmp_path,
        "terms.yaml",
        """
        version: 1
        entries:
          - id: rack.xcbr
            terms: [XCBR]
            title: Breaker on rack 7
            summary: The rack's breakers are simulated by the test set on port 3.
        """,
    )
    cat = Catalogue.load(extra_paths=[extra])
    exp = cat.explain("XCBR")
    assert exp is not None and exp.id == "rack.xcbr"
    assert cat.explain("ln.xcbr") is not None  # still reachable by id


def test_empty_extra_file_is_allowed(tmp_path: Path) -> None:
    extra = _write(tmp_path, "empty.yaml", "")
    assert len(Catalogue.load(extra_paths=[extra])) == len(Catalogue.load())


def test_builtin_can_be_left_out(tmp_path: Path) -> None:
    extra = _write(
        tmp_path,
        "only.yaml",
        """
        version: 1
        entries:
          - id: only
            keys: [ied:timeout]
            title: Timeout
            summary: Only entry.
            hint: "only"
            certainty: check
        """,
    )
    cat = Catalogue.load(extra_paths=[extra], include_builtin=False)
    assert len(cat) == 1
    assert cat.coverage()["ied"]  # nearly everything missing
    assert cat.hint("ied:access-denied") is None


def test_missing_extra_file_is_a_clear_error(tmp_path: Path) -> None:
    with pytest.raises(CatalogueError, match="nope.yaml: cannot read"):
        Catalogue.load(extra_paths=[tmp_path / "nope.yaml"])


# ---------------------------------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------------------------------
GOOD = """
  - id: good
    keys: [ied:timeout]
    title: Timeout
    summary: Something.
    hint: "a hint"
    certainty: check
"""


@pytest.mark.parametrize(
    ("entries", "message"),
    [
        (
            """
            - id: bad
              keys: [ied:timeout]
              title: T
              summary: S
              hint: "h"
              certainty: sure
            """,
            "entry 'bad': bad certainty 'sure'",
        ),
        (
            """
            - id: bad
              keys: [ied:timeout]
              title: T
              summary: S
              hint: "h"
              certainty: check
              colour: red
            """,
            "entry 'bad': unknown field(s) colour",
        ),
        (
            """
            - id: bad
              keys: [ied:no-such-error]
              title: T
              summary: S
              hint: "h"
              certainty: check
            """,
            "entry 'bad': key 'ied:no-such-error' does not parse",
        ),
        (
            """
            - id: bad
              keys: [nonsense:thing]
              title: T
              summary: S
              hint: "h"
              certainty: check
            """,
            "unknown domain 'nonsense'",
        ),
        (
            """
            - id: bad
              keys: [ied:21]
              title: T
              summary: S
              hint: "h"
              certainty: check
            """,
            "must be written by name, as 'ied:access-denied'",
        ),
        (
            """
            - id: bad
              keys: [association:no-such-outcome]
              title: T
              summary: S
              hint: "h"
              certainty: check
            """,
            "does not parse",
        ),
        (
            """
            - id: bad
              keys: [ied:timeout]
              title: T
              summary: S
              hint: |
                line one
                line two
              certainty: check
            """,
            "'hint' must be a single line",
        ),
        (
            """
            - id: bad
              keys: [ied:timeout]
              title: T
              summary: S
              hint: "Likely cause: something"
              certainty: likely
            """,
            "must not start with a certainty prefix",
        ),
        (
            """
            - id: bad
              keys: [ied:timeout]
              title: T
              summary: S
            """,
            "need a one-line 'hint'",
        ),
        (
            """
            - id: bad
              keys: [ied:timeout]
              title: T
              summary: S
              hint: "h"
            """,
            "need a 'certainty'",
        ),
        (
            """
            - id: bad
              terms: [thing]
              title: T
              summary: S
              hint: "h"
              certainty: check
            """,
            "only allowed on entries with 'keys'",
        ),
        (
            """
            - id: bad
              keys: [ied:timeout]
              when: {service: fly}
              hint: "h"
              certainty: check
            """,
            "'when.service' value 'fly' is not one of",
        ),
        (
            """
            - id: bad
              keys: [ied:timeout]
              when: {fc: XX}
              hint: "h"
              certainty: check
            """,
            "'when.fc' value 'XX'",
        ),
        (
            """
            - id: bad
              keys: [ied:timeout]
              when: {weather: sunny}
              hint: "h"
              certainty: check
            """,
            "unknown context field 'weather'",
        ),
        (
            """
            - id: bad
              keys: [ied:timeout]
              when: {tags: {bound_ip: yes}}
              hint: "h"
              certainty: check
            """,
            "'when.tags.bound_ip' has a true/false value; YAML reads unquoted yes/no/on/off as booleans",
        ),
        (
            """
            - id: bad
              keys: [ied:timeout]
              title: T
              summary: ""
              hint: "h"
              certainty: check
            """,
            "'summary' is empty",
        ),
        (
            """
            - id: bad
              keys: [ied:timeout]
              summary: S
              hint: "h"
              certainty: check
            """,
            "'title' is required",
        ),
        (
            """
            - id: Bad Id
              title: T
              summary: S
            """,
            "entry #2: 'id' must be a lower-case identifier",
        ),
        (
            """
            - id: good
              title: T
              summary: S
            """,
            "entry 'good': duplicate id in the same file",
        ),
        (
            """
            - id: bad
              kind: widget
              title: T
              summary: S
            """,
            "'kind' must be one of error, term, concept",
        ),
        (
            """
            - id: t1
              terms: [Widget]
              title: T
              summary: S
            - id: t2
              terms: [widget]
              title: T
              summary: S
            """,
            "term 'widget' is already used by entry 't1'",
        ),
        (
            """
            - id: bad
              keys: [ied:timeout]
              title: T
              summary: S
              hint: "h"
              certainty: check
              priority: high
            """,
            "'priority' must be an integer",
        ),
    ],
)
def test_validation_errors_name_the_file_and_the_entry(tmp_path: Path, entries: str, message: str) -> None:
    body = "version: 1\nentries:" + GOOD + textwrap.indent(textwrap.dedent(entries), "  ")
    path = tmp_path / "broken.yaml"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(CatalogueError) as info:
        Catalogue.load(extra_paths=[path])
    text = str(info.value)
    assert str(path) in text
    assert message in text


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("entries: []\n", "'version' must be 1"),
        ("version: 1\nentries: []\nextra: 1\n", "unknown top-level key(s) extra"),
        ("- just\n- a list\n", "top level must be a mapping"),
        ("version: 1\nentries: {a: 1}\n", "'entries' must be a list"),
        ("version: 1\nentries:\n  - id: x\n    title: [unclosed\n", "invalid YAML"),
        ("version: 1\nentries:\n  - 42\n", "entry #1: must be a mapping"),
    ],
)
def test_file_level_validation_errors(tmp_path: Path, body: str, message: str) -> None:
    path = tmp_path / "file.yaml"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(CatalogueError, match=None) as info:
        Catalogue.load(extra_paths=[path], include_builtin=False)
    assert str(path) in str(info.value)
    assert message in str(info.value)


def test_multiline_text_is_reflowed_into_paragraphs_and_list_items(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "flow.yaml",
        """
        version: 1
        entries:
          - id: flow
            title: Flow
            summary: >-
              One sentence
              over two lines.
            details: |
              First paragraph
              continues here.

              - item one
                continues
              - item two
        """,
    )
    cat = Catalogue.load(extra_paths=[path], include_builtin=False)
    exp = cat.explain("flow")
    assert exp is not None
    assert exp.summary == "One sentence over two lines."
    assert exp.details == "First paragraph continues here.\n\n- item one continues\n- item two"


def test_builtin_catalogue_loads_with_certainty_values_only_from_the_enum() -> None:
    cat = Catalogue.load()
    assert {e.certainty for e in cat.entries if e.certainty} <= set(Certainty)
