"""Value types of the explanation system (§13): certainty, context, hints and explanations."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from mms_client import codes


class Certainty(StrEnum):
    """How sure a hint is (EXP-4).

    ``FACT`` is reserved for statements the tool has proved (for example: the device model says the
    attribute has FC=ST, so it is read-only by design). Anything inferred is ``LIKELY``; anything
    the operator has to go and look at is ``CHECK``.
    """

    FACT = "fact"
    LIKELY = "likely"
    CHECK = "check"

    @property
    def prefix(self) -> str:
        """Text put in front of a hint of this certainty when it is rendered."""
        return _PREFIXES[self]


_PREFIXES: dict[Certainty, str] = {
    Certainty.FACT: "",
    Certainty.LIKELY: "Likely cause: ",
    Certainty.CHECK: "Check: ",
}

# Prefixes that must not be written into catalogue text, because rendering adds them.
RESERVED_PREFIXES: tuple[str, ...] = ("likely cause", "likely:", "check:", "fact:")


def _norm_ctl_model(value: object) -> str | None:
    """Accept a ctlModel as its canonical name, its number, or a numeric string."""
    if value is None:
        return None
    if isinstance(value, int):
        return codes.CTL_MODELS.get(value, str(value))
    text = str(value).strip()
    if text.isdigit():
        return codes.CTL_MODELS.get(int(text), text)
    return text


@dataclass(frozen=True)
class HintContext:
    """What the tool knew when the error happened (EXP-3).

    All fields are optional. A catalogue entry that constrains a field only matches when the field
    is known *and* has one of the listed values, so an unknown context never produces a fact.

    ``service`` is one of :data:`mms_client.explain.SERVICES`; ``mode`` is ``standard`` or
    ``expert``; ``edition`` is ``Ed1``, ``Ed2``, ``Ed2.1`` or ``unknown``; ``ctl_model`` is a name
    from :data:`mms_client.codes.CTL_MODELS` (a number is converted). ``tags`` carries free-form
    facts such as ``{"bound_ip": "yes"}`` or ``{"pattern": "refused-after-abrupt-disconnect"}``;
    tag values may also be substituted into hint text (``{local_ip}`` placeholders).
    """

    service: str | None = None
    fc: str | None = None
    cdc: str | None = None
    ln_class: str | None = None
    mode: str | None = None
    edition: str | None = None
    ctl_model: str | None = None
    tags: Mapping[str, str] = field(default_factory=dict, hash=False)

    def __post_init__(self) -> None:
        for name in ("fc", "cdc", "ln_class"):  # upper case by convention; keeps {fc} placeholders tidy
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, str(value).strip().upper())
        object.__setattr__(self, "ctl_model", _norm_ctl_model(self.ctl_model))
        object.__setattr__(self, "tags", {str(k): str(v) for k, v in dict(self.tags or {}).items()})

    def value(self, name: str) -> str | None:
        """The value of a context field, or of a tag when ``name`` is ``tags.<tag>``."""
        if name.startswith("tags."):
            return self.tags.get(name[5:])
        v = getattr(self, name)
        return None if v is None else str(v)

    def to_json(self) -> dict:
        out: dict = {
            k: getattr(self, k)
            for k in ("service", "fc", "cdc", "ln_class", "mode", "edition", "ctl_model")
            if getattr(self, k) is not None
        }
        if self.tags:
            out["tags"] = dict(self.tags)
        return out


@dataclass(frozen=True)
class Hint:
    """A one-line hint for one error in one context (EXP-1)."""

    key: str  # the error key it was matched for, e.g. "data-access:object-access-denied"
    entry_id: str  # the catalogue entry it came from; `explain <entry_id>` gives the full text
    text: str  # one line, without the certainty prefix
    certainty: Certainty

    def render(self) -> str:
        """The hint as shown to the operator: ``Likely cause: …``, ``Check: …`` or the fact itself."""
        return f"{self.certainty.prefix}{self.text}"

    def __str__(self) -> str:
        return self.render()

    def to_json(self) -> dict:
        return {
            "key": self.key,
            "entry_id": self.entry_id,
            "certainty": self.certainty.value,
            "text": self.text,
            "rendered": self.render(),
        }


@dataclass(frozen=True)
class Explanation:
    """The long form behind ``explain <code>`` / ``explain <term>`` (EXP-2)."""

    id: str
    title: str
    kind: str  # "error" | "term" | "concept"
    summary: str
    details: str
    next_checks: tuple[str, ...]
    related: tuple[str, ...]
    keys: tuple[str, ...] = ()  # error keys the entry covers (errors only)
    hint: Hint | None = None  # the one-line hint for the queried error/context (errors only)

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "kind": self.kind,
            "summary": self.summary,
            "details": self.details,
            "next_checks": list(self.next_checks),
            "related": list(self.related),
            "keys": list(self.keys),
            "hint": self.hint.to_json() if self.hint else None,
        }
