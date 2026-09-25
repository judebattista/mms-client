"""The hint and explanation catalogue (§13, EXP-1 … EXP-7).

The catalogue is YAML data (EXP-5). The built-in files live in ``ied_client/data`` and are loaded
in this order:

1. ``hints.yaml`` (its header comment documents the file format),
2. ``hints/*.yaml`` in file-name order,
3. ``glossary.yaml``,
4. ``glossary/*.yaml`` in file-name order,
5. each protocol module's catalogue files (PROTO-13), protocols in name order,

followed by any ``extra_paths`` given to :meth:`Catalogue.load`, in the order given. An entry in a
later file with the same ``id`` as an earlier one replaces it completely.

The code domains an entry may name are those in :mod:`ied_client.codes`, including the ones the
installed protocol modules register.

Matching rule (EXP-3)
---------------------
For an error key and a :class:`HintContext`, the candidates are the entries that list the key
(exact entries) plus the entries that list ``<domain>:*`` (wildcard entries). A candidate matches
when every condition in its ``when`` block holds; a condition on a field the context does not know
never holds. Among the matching candidates the winner is the one with the highest rank, where rank
compares, in order:

1. ``priority`` (default 0; higher wins) — an explicit override for experiment files;
2. exact key before wildcard key;
3. specificity: the number of conditions in ``when`` (each tag counts as one condition);
4. load order: the entry that appears later wins (a later file, or further down the same file), so
   layered files beat the built-in catalogue at equal specificity.

The rule is deterministic. :meth:`Catalogue.lint` reports pairs of entries that could both match
the same situation with equal rank, so that authors do not rely on rule 4 by accident.
"""

from __future__ import annotations

import difflib
import functools
import importlib.resources
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml

from ied_client import codes
from ied_client.codes import Domain, ErrorInfo
from ied_client.protocol import registry as protocols

from .model import RESERVED_PREFIXES, Certainty, Explanation, Hint, HintContext

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------
SERVICES: tuple[str, ...] = (
    "associate",
    "identify",
    "browse",
    "read",
    "write",
    "watch",
    "setgroup",
    "select",
    "operate",
    "cancel",
    "command-termination",
    "get-rcb",
    "set-rcb",
    "subscribe",
    "gi",
    "file-dir",
    "file-get",
    "dataset",
)
MODES: tuple[str, ...] = ("standard", "expert")
EDITIONS: tuple[str, ...] = ("Ed1", "Ed2", "Ed2.1", "unknown")
KINDS: tuple[str, ...] = ("error", "term", "concept")
CONTEXT_FIELDS: tuple[str, ...] = ("service", "fc", "cdc", "ln_class", "mode", "edition", "ctl_model")

# Tool-level codes and check ids that the rest of the system raises. The catalogue must cover them
# (see Catalogue.coverage); other tool/check codes fall back to the `tool:*` / `check:*` entries.
REQUIRED_TOOL_CODES: tuple[str, ...] = (
    "refused-standard-mode",
    "write-type-unsupported",
    "write-se-needs-setgroup",
    "write-co-needs-operate",
    "write-readback-mismatch",
    "status-only-control",
    "not-probeable-direct-control",
    "rcb-no-free-instance",
    "rcb-owned-by-other-client",
    "takeover-requires-expert",
    "command-termination-timeout",
    "command-termination-negative",
    "select-timeout-expired",
    "edition-unknown",
    "non-interactive-no-prompt",
    "local-address-missing",
    "association-slot-occupied-by-tool",
    "slots-probably-exhausted",
    "rcb-cleanup-incomplete",
    "security-not-supported",
    "goose-out-of-scope",
    # raised by ied_client.core
    "orcat-required",
    "invalid-orcat",
    "cdc-not-supported",
    "control-refused",
    "rcb-not-found",
    "invalid-trgops",
    "no-setting-groups",
    "ambiguous-sgcb",
    "invalid-setting-group",
    "sgcb-attribute-missing",
    "not-a-setting",
    "nothing-to-edit",
    "inventory-required",
    "unknown-client",
    "restore-device-mismatch",
    # raised by protocol modules' diagnosis (DIA-4)
    "known-association-limit",
    # raised by ied_client.cli
    "usage-error",
    "invalid-reference",
    "object-not-in-model",
    "not-connected",
    "not-confirmed",
    "interrupted",
    "internal-error",
    "inventory-invalid",
    "local-file-error",
    "unknown-protocol",
    "connection-lost",
)
REQUIRED_CHECK_IDS: tuple[str, ...] = (
    "rcb-dataset-missing",
    "dataset-member-unresolved",
    "rcb-enabled-by-unknown-client",
    "rcb-reserved-by-unknown-client",
    "brcb-buffer-overflow",
    "writable-attribute-refused",
    "confrev-mismatch",
    "model-object-missing",
    "model-object-unexpected",
    "model-type-mismatch",
    "model-fc-mismatch",
    "config-value-mismatch",
    "dataset-mismatch",
    "rcb-attribute-mismatch",
    "address-mismatch",
    "identity-disagreement",
    "edition-mismatch",
    "object-reference-too-long",
    "locsta-missing",
    "client-rcb-missing",
    "client-rcb-not-available",
    "device-supplied-reference",
    "snapshot-difference",
)

def required_names() -> dict[str, tuple[str, ...]]:
    """Names every domain must cover (EXP-7 plus the tool/check codes above), for the domains
    registered now (the client's and the installed protocol modules')."""
    out: dict[str, tuple[str, ...]] = {d: tuple(t.values()) for d, t in codes.numbered_tables().items()}
    out[Domain.ASSOCIATION] = tuple(codes.ASSOCIATION_OUTCOMES)
    out[Domain.TOOL] = tuple(dict.fromkeys((*REQUIRED_TOOL_CODES, *codes.required_tool_codes())))
    out[Domain.CHECK] = REQUIRED_CHECK_IDS
    return out


def bare_name_preference() -> tuple[str, ...]:
    """`explain <bare-name>`: when a name exists in several domains, prefer them in this order."""
    return tuple(d.name for d in codes.domains())


_ENTRY_FIELDS = frozenset(
    {
        "id",
        "keys",
        "terms",
        "kind",
        "title",
        "summary",
        "details",
        "next_checks",
        "related",
        "hint",
        "certainty",
        "when",
        "priority",
    }
)
_TOP_FIELDS = frozenset({"version", "entries"})
_FORMAT_VERSION = 1
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._+-]*$")
_CODE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_TAG_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
_PLACEHOLDER_RE = re.compile(r"\{([a-z_][a-z0-9_]*)\}")
_LN_NAME_RE = re.compile(r"^[A-Z0-9_]*?([A-Z]{4})\d+$")

def _domain_aliases() -> dict[str, str]:
    """Normalised spellings of every registered domain name and alias → the domain."""
    out: dict[str, str] = {}
    for d in codes.domains():
        out[re.sub(r"[^a-z0-9]", "", str(d.name))] = d.name
        for a in d.aliases:
            out[a] = d.name
    return out


try:  # the C loader is much faster when libyaml is available
    _Loader: type = yaml.CSafeLoader
except AttributeError:  # pragma: no cover - depends on the PyYAML build
    _Loader = yaml.SafeLoader


class CatalogueError(ValueError):
    """A catalogue file is unreadable or invalid. The message names the file and the entry."""


def _norm(text: str) -> str:
    """Lookup form of a term: case-folded, with everything but letters and digits removed."""
    return re.sub(r"[^a-z0-9]", "", text.casefold())


def _reflow(text: str) -> str:
    """Normalise multi-line YAML text.

    Paragraphs are separated by blank lines and come out joined by ``"\\n\\n"``. Inside a
    paragraph, source line breaks are replaced by spaces, except that a line starting with ``- ``
    starts a new list item (items are joined by ``"\\n"``).
    """
    paragraphs: list[str] = []
    for block in re.split(r"\n[ \t]*\n", text.strip()):
        items: list[str] = []
        for line in block.splitlines():
            s = line.strip()
            if not s:
                continue
            if s.startswith("- ") or not items:
                items.append(s)
            else:
                items[-1] = f"{items[-1]} {s}"
        if items:
            paragraphs.append("\n".join(items))
    return "\n\n".join(paragraphs)


def _fill(text: str, ctx: HintContext) -> str:
    """Replace ``{name}`` placeholders with context fields or tags; unknown ones become ``<name>``."""

    def repl(m: re.Match[str]) -> str:
        name = m.group(1)
        value = ctx.tags.get(name)
        if value is None and name in CONTEXT_FIELDS:
            value = getattr(ctx, name)
        return str(value) if value is not None else f"<{name}>"

    return _PLACEHOLDER_RE.sub(repl, text)


def _dedup(items: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return tuple(out)


# ---------------------------------------------------------------------------
# Entries
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Condition:
    """One ``when`` condition: the context field must have one of ``values`` (``*`` = any value)."""

    field: str  # a CONTEXT_FIELDS name, or "tags.<tag>"
    values: frozenset[str]  # case-folded
    display: tuple[str, ...]  # as written, for titles and messages

    def holds(self, ctx: HintContext) -> bool:
        value = ctx.value(self.field)
        if value is None:
            return False
        return "*" in self.values or value.casefold() in self.values

    def overlaps(self, other: Condition) -> bool:
        return "*" in self.values or "*" in other.values or bool(self.values & other.values)

    def label(self) -> str:
        name = self.field.removeprefix("tags.")
        return f"{name}={'|'.join(self.display)}"


@dataclass(frozen=True)
class Entry:
    """One catalogue entry, as validated from YAML."""

    id: str
    source: str
    builtin: bool
    index: int
    keys: tuple[str, ...]
    terms: tuple[str, ...]
    kind: str
    title: str | None
    summary: str | None
    details: str | None
    next_checks: tuple[str, ...] | None
    related: tuple[str, ...] | None
    hint: str | None
    certainty: Certainty | None
    when: tuple[Condition, ...]
    priority: int

    @property
    def specificity(self) -> int:
        return len(self.when)

    def matches(self, ctx: HintContext) -> bool:
        return all(c.holds(ctx) for c in self.when)

    def condition_label(self) -> str:
        return ", ".join(c.label() for c in self.when)


class _Parser:
    """Validates one YAML file's entries and turns them into :class:`Entry` objects."""

    def __init__(self, label: str, builtin: bool, first_index: int) -> None:
        self.label = label
        self.builtin = builtin
        self.index = first_index

    def fail(self, where: str, message: str) -> CatalogueError:
        return CatalogueError(f"{self.label}: {where}: {message}")

    def entries(self, text: str) -> list[Entry]:
        try:
            data = yaml.load(text, Loader=_Loader)  # a safe loader (CSafeLoader or SafeLoader)
        except yaml.YAMLError as exc:
            raise CatalogueError(f"{self.label}: invalid YAML: {exc}") from exc
        if data is None:
            return []
        if not isinstance(data, dict):
            raise CatalogueError(f"{self.label}: top level must be a mapping with 'version' and 'entries'")
        unknown = sorted(str(k) for k in set(data) - _TOP_FIELDS)
        if unknown:
            raise CatalogueError(
                f"{self.label}: unknown top-level key(s) {', '.join(unknown)}; allowed: version, entries"
            )
        if data.get("version") != _FORMAT_VERSION:
            raise CatalogueError(
                f"{self.label}: 'version' must be {_FORMAT_VERSION}, got {data.get('version')!r}"
            )
        raw_entries = data.get("entries") or []
        if not isinstance(raw_entries, list):
            raise CatalogueError(f"{self.label}: 'entries' must be a list")
        out: list[Entry] = []
        seen: set[str] = set()
        for position, raw in enumerate(raw_entries):
            entry = self.entry(position, raw)
            if entry.id in seen:
                raise self.fail(f"entry {entry.id!r}", "duplicate id in the same file")
            seen.add(entry.id)
            out.append(entry)
        return out

    # -- one entry ----------------------------------------------------------------------------
    def entry(self, position: int, raw: object) -> Entry:
        where = f"entry #{position + 1}"
        if not isinstance(raw, dict):
            raise self.fail(where, f"must be a mapping, got {type(raw).__name__}")
        eid = raw.get("id")
        if not isinstance(eid, str) or not _ID_RE.match(eid):
            raise self.fail(where, f"'id' must be a lower-case identifier ([a-z0-9._+-]), got {eid!r}")
        where = f"entry {eid!r}"
        unknown = sorted(str(k) for k in set(raw) - _ENTRY_FIELDS)
        if unknown:
            raise self.fail(
                where, f"unknown field(s) {', '.join(unknown)}; allowed: {', '.join(sorted(_ENTRY_FIELDS))}"
            )

        keys = self.str_list(where, raw, "keys") or ()
        for key in keys:
            self.check_key(where, key)
        if len(set(keys)) != len(keys):
            raise self.fail(where, "'keys' lists the same key twice")
        terms = self.str_list(where, raw, "terms") or ()
        for term in terms:
            if not _norm(term):
                raise self.fail(where, f"term {term!r} has no letters or digits")

        kind = raw.get("kind", "error" if keys else "term")
        if kind not in KINDS:
            raise self.fail(where, f"'kind' must be one of {', '.join(KINDS)}, got {kind!r}")
        if keys and kind != "error":
            raise self.fail(where, "entries with 'keys' must have kind 'error'")
        if not keys and kind == "error":
            raise self.fail(where, "kind 'error' needs 'keys'")

        hint = self.text(where, raw, "hint", single_line=True)
        certainty_raw = raw.get("certainty")
        certainty: Certainty | None = None
        if keys:
            if hint is None:
                raise self.fail(where, "entries with 'keys' need a one-line 'hint'")
            if certainty_raw is None:
                raise self.fail(where, "entries with 'keys' need a 'certainty' (fact, likely or check)")
        elif hint is not None or certainty_raw is not None:
            raise self.fail(where, "'hint' and 'certainty' are only allowed on entries with 'keys'")
        if certainty_raw is not None:
            try:
                certainty = Certainty(certainty_raw)
            except ValueError:
                allowed = ", ".join(c.value for c in Certainty)
                raise self.fail(where, f"bad certainty {certainty_raw!r}; must be one of {allowed}") from None
        if hint is not None and hint.casefold().startswith(RESERVED_PREFIXES):
            raise self.fail(
                where, "'hint' must not start with a certainty prefix; it is added when rendering"
            )

        when = self.conditions(where, raw.get("when"))
        if when and not keys:
            raise self.fail(where, "'when' is only allowed on entries with 'keys'")
        if when and terms:
            raise self.fail(where, "context variants (entries with 'when') cannot have 'terms'")

        priority = raw.get("priority", 0)
        if not isinstance(priority, int) or isinstance(priority, bool):
            raise self.fail(where, f"'priority' must be an integer, got {priority!r}")

        title = self.text(where, raw, "title", single_line=True)
        summary = self.text(where, raw, "summary")
        if not when:
            if title is None:
                raise self.fail(where, "'title' is required (it may only be omitted on context variants)")
            if summary is None:
                raise self.fail(where, "'summary' is required (it may only be omitted on context variants)")

        entry = Entry(
            id=eid,
            source=self.label,
            builtin=self.builtin,
            index=self.index,
            keys=tuple(keys),
            terms=tuple(terms),
            kind=kind,
            title=title,
            summary=summary,
            details=self.text(where, raw, "details"),
            next_checks=self.str_list(where, raw, "next_checks"),
            related=self.str_list(where, raw, "related"),
            hint=hint,
            certainty=certainty,
            when=when,
            priority=priority,
        )
        self.index += 1
        return entry

    # -- field helpers ------------------------------------------------------------------------
    def text(self, where: str, raw: dict, name: str, single_line: bool = False) -> str | None:
        if name not in raw or raw[name] is None:
            return None
        value = raw[name]
        if not isinstance(value, str):
            raise self.fail(where, f"'{name}' must be text, got {type(value).__name__}")
        value = value.strip()
        if not value:
            raise self.fail(where, f"'{name}' is empty")
        if single_line:
            if "\n" in value:
                raise self.fail(where, f"'{name}' must be a single line")
            return value
        return _reflow(value)

    def str_list(self, where: str, raw: dict, name: str) -> tuple[str, ...] | None:
        if name not in raw or raw[name] is None:
            return None
        value = raw[name]
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list):
            raise self.fail(where, f"'{name}' must be a list of text")
        out: list[str] = []
        for item in value:
            if not isinstance(item, str) or not item.strip():
                raise self.fail(where, f"'{name}' contains an empty or non-text item: {item!r}")
            out.append(item.strip())
        return tuple(out)

    def check_key(self, where: str, key: str) -> None:
        domain_s, sep, name = key.partition(":")
        if not sep or not name:
            raise self.fail(where, f"key {key!r} must look like '<domain>:<name>'")
        try:
            domain = codes.domain(domain_s)
        except ValueError:
            domains = ", ".join(str(d.name) for d in codes.domains())
            raise self.fail(where, f"key {key!r}: unknown domain {domain_s!r} (known: {domains})") from None
        if name == "*":
            return
        try:
            parsed = codes.parse_key(key)
        except (KeyError, ValueError) as exc:
            raise self.fail(where, f"key {key!r} does not parse: {exc}") from None
        if parsed.key != key:
            raise self.fail(where, f"key {key!r} must be written by name, as {parsed.key!r}")
        if domain in (Domain.TOOL, Domain.CHECK) and not _CODE_NAME_RE.match(name):
            raise self.fail(where, f"key {key!r}: {domain} names are lower-case-kebab")

    def conditions(self, where: str, raw: object) -> tuple[Condition, ...]:
        if raw is None:
            return ()
        if not isinstance(raw, dict) or not raw:
            raise self.fail(where, "'when' must be a non-empty mapping of context fields")
        out: list[Condition] = []
        for name, value in raw.items():
            if name == "tags":
                if not isinstance(value, dict) or not value:
                    raise self.fail(where, "'when.tags' must be a non-empty mapping")
                for tag, tag_value in value.items():
                    if not isinstance(tag, str) or not _TAG_RE.match(tag):
                        raise self.fail(where, f"bad tag name {tag!r} (use lower_snake_case)")
                    out.append(self.condition(where, f"tags.{tag}", tag_value, vocabulary=None))
                continue
            if name not in CONTEXT_FIELDS:
                allowed = ", ".join((*CONTEXT_FIELDS, "tags"))
                raise self.fail(where, f"unknown context field {name!r} in 'when'; allowed: {allowed}")
            out.append(self.condition(where, name, value, vocabulary=_VOCABULARY.get(name)))
        return tuple(out)

    def condition(
        self, where: str, name: str, value: object, vocabulary: tuple[str, ...] | None
    ) -> Condition:
        values = [value] if isinstance(value, str | bool) else value
        if isinstance(values, list) and any(isinstance(v, bool) for v in values):
            raise self.fail(
                where,
                f"'when.{name}' has a true/false value; YAML reads unquoted yes/no/on/off as booleans, "
                'so quote it (e.g. "yes")',
            )
        if not isinstance(values, list) or not values:
            raise self.fail(where, f"'when.{name}' must be text or a non-empty list of text")
        display: list[str] = []
        for v in values:
            if not isinstance(v, str) or not v.strip():
                raise self.fail(where, f"'when.{name}' has a non-text or empty value: {v!r}")
            v = v.strip()
            if vocabulary is not None and v != "*" and v.casefold() not in {x.casefold() for x in vocabulary}:
                raise self.fail(where, f"'when.{name}' value {v!r} is not one of: {', '.join(vocabulary)}")
            display.append(v)
        return Condition(name, frozenset(v.casefold() for v in display), tuple(display))


_VOCABULARY: dict[str, tuple[str, ...]] = {
    "service": SERVICES,
    "fc": tuple(codes.FCS),
    "mode": MODES,
    "edition": EDITIONS,
    "ctl_model": tuple(codes.CTL_MODELS.values()),
}


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
def _builtin_sources() -> list[tuple[str, str]]:
    """(label, text) of the built-in catalogue files, in load order: the client's, then each protocol
    module's (PROTO-13)."""
    data = importlib.resources.files("ied_client").joinpath("data")
    out: list[tuple[str, str]] = []
    for name in ("hints.yaml", "hints", "glossary.yaml", "glossary"):
        node = data.joinpath(name)
        if name.endswith(".yaml"):
            if not node.is_file():
                raise CatalogueError(f"built-in catalogue file ied_client/data/{name} is missing")
            out.append((f"ied_client/data/{name}", node.read_text(encoding="utf-8")))
        elif node.is_dir():
            for child in sorted(node.iterdir(), key=lambda c: c.name):
                if child.name.endswith(".yaml") and child.is_file():
                    out.append((f"ied_client/data/{name}/{child.name}", child.read_text(encoding="utf-8")))
    for module in protocols.load_all():
        out.extend(module.catalogue_sources())
    return out


class Catalogue:
    """Hints (EXP-1, EXP-3, EXP-4) and explanations (EXP-2) loaded from YAML (EXP-5)."""

    def __init__(self, entries: Iterable[Entry]) -> None:
        self._entries: dict[str, Entry] = {}
        for entry in sorted(entries, key=lambda e: e.index):
            self._entries.pop(entry.id, None)
            self._entries[entry.id] = entry
        self._by_key: dict[str, list[Entry]] = {}
        self._wildcards: dict[str, list[Entry]] = {}
        self._code_names: dict[str, set[str]] = {}
        for entry in self._entries.values():
            for key in entry.keys:
                domain, _, name = key.partition(":")
                if name == "*":
                    self._wildcards.setdefault(domain, []).append(entry)
                else:
                    self._by_key.setdefault(key, []).append(entry)
                    self._code_names.setdefault(domain, set()).add(name)
        self._terms = self._build_term_index()

    def _build_term_index(self) -> dict[str, str]:
        owners: dict[str, Entry] = {}
        for entry in self._entries.values():
            for term in entry.terms:
                n = _norm(term)
                prev = owners.get(n)
                # A term may belong to one entry per file, and to one entry across the built-in
                # files; an extra (layered) file may shadow a term from an earlier file.
                if (
                    prev is not None
                    and prev.id != entry.id
                    and (prev.source == entry.source or (prev.builtin and entry.builtin))
                ):
                    raise CatalogueError(
                        f"{entry.source}: entry {entry.id!r}: term {term!r} is already used by entry "
                        f"{prev.id!r} ({prev.source})"
                    )
                owners[n] = entry  # later files shadow earlier ones
        return {n: e.id for n, e in owners.items()}

    # -- construction ------------------------------------------------------------------------
    @classmethod
    def load(cls, extra_paths: Sequence[Path | str] = (), *, include_builtin: bool = True) -> Catalogue:
        """Load the built-in catalogue, then layer ``extra_paths`` on top (later files win by id)."""
        sources: list[tuple[str, str, bool]] = []
        if include_builtin:
            sources.extend((label, text, True) for label, text in _builtin_sources())
        for p in extra_paths:
            path = Path(p)
            try:
                text = path.read_text(encoding="utf-8")
            except OSError as exc:
                raise CatalogueError(f"{path}: cannot read catalogue file: {exc}") from exc
            sources.append((str(path), text, False))
        return cls.from_texts(sources)

    @classmethod
    def from_texts(cls, sources: Iterable[tuple[str, str, bool]]) -> Catalogue:
        """Build from ``(label, yaml_text, builtin)`` triples, in load order."""
        entries: list[Entry] = []
        index = 0
        for label, text, builtin in sources:
            parsed = _Parser(label, builtin, index).entries(text)
            index += len(parsed)
            entries.extend(parsed)
        return cls(entries)

    # -- introspection -----------------------------------------------------------------------
    @property
    def entries(self) -> tuple[Entry, ...]:
        """All entries after layering, in load order."""
        return tuple(self._entries.values())

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, entry_id: object) -> bool:
        return isinstance(entry_id, str) and entry_id.casefold() in self._entries

    def entry(self, entry_id: str) -> Entry | None:
        return self._entries.get(entry_id.casefold())

    def vocabulary(self) -> list[str]:
        """Every word `explain` understands directly (ids, terms, error keys), for completion."""
        words: set[str] = set()
        for entry in self._entries.values():
            words.add(entry.id)
            words.update(entry.terms)
            words.update(k for k in entry.keys if not k.endswith(":*"))
        return sorted(words, key=str.casefold)

    # -- hints -------------------------------------------------------------------------------
    def hint(self, error: ErrorInfo | str, ctx: HintContext | None = None) -> Hint | None:
        """The one-line hint for an error in a context, or None if the catalogue has nothing."""
        key = self._key_of(error)
        if key is None:
            return None
        ctx = ctx or HintContext()
        entry = self._best(key, ctx)
        if entry is None:
            return None
        return self._make_hint(entry, key, ctx)

    def _key_of(self, error: ErrorInfo | str) -> str | None:
        if isinstance(error, ErrorInfo):
            return error.key
        key = self._resolve_key(error)
        if key is not None:
            return key
        keys = self._bare_name_keys(error)
        return keys[0] if keys else None

    def _candidates(self, key: str) -> list[tuple[Entry, bool]]:
        domain = key.partition(":")[0]
        exact = [(e, True) for e in self._by_key.get(key, ())]
        wild = [(e, False) for e in self._wildcards.get(domain, ())]
        return exact + wild

    @staticmethod
    def _rank(entry: Entry, exact: bool) -> tuple[int, int, int, int]:
        return (entry.priority, int(exact), entry.specificity, entry.index)

    def _best(self, key: str, ctx: HintContext) -> Entry | None:
        matching = [(self._rank(e, exact), e) for e, exact in self._candidates(key) if e.matches(ctx)]
        if not matching:
            return None
        return max(matching, key=lambda t: t[0])[1]

    def _base(self, key: str) -> Entry | None:
        """The context-free entry for a key (what a variant inherits missing fields from)."""
        return self._best(key, HintContext())

    def _make_hint(self, entry: Entry, key: str, ctx: HintContext) -> Hint:
        assert entry.hint is not None and entry.certainty is not None
        return Hint(key=key, entry_id=entry.id, text=_fill(entry.hint, ctx), certainty=entry.certainty)

    # -- explanations ------------------------------------------------------------------------
    def explain(self, query: str, ctx: HintContext | None = None) -> Explanation | None:
        """Explain an error key, bare error name, ``domain:number``, term or entry id.

        Lookup order: entry id, error key (``domain:name`` or ``domain:number``), term, libiec61850
        constant name (``IED_ERROR_ACCESS_DENIED``), bare error name (preferring data-access, then
        ied, then mms, … with the other domains listed in ``related``), and finally a logical
        node name (``Q0XCBR1`` → XCBR) or the parts of an object reference. Case-insensitive.
        """
        q = query.strip()
        if not q:
            return None
        ctx = ctx or HintContext()

        entry = self._entries.get(q.casefold())
        if entry is not None:
            return self._explanation(entry, None, ctx)

        key = self._resolve_key(q)
        if key is not None:
            best = self._best(key, ctx)
            return self._explanation(best, key, ctx) if best is not None else None

        entry_id = self._terms.get(_norm(q))
        if entry_id is not None:
            return self._explanation(self._entries[entry_id], None, ctx)

        key = self._resolve_constant(q)
        if key is not None:
            best = self._best(key, ctx)
            if best is not None:
                return self._explanation(best, key, ctx)

        keys = self._bare_name_keys(q)
        if keys:
            best = self._best(keys[0], ctx)
            if best is not None:
                return self._explanation(best, keys[0], ctx, extra_related=keys[1:])

        return self._explain_reference(q, ctx)

    def _explain_reference(self, q: str, ctx: HintContext) -> Explanation | None:
        """`XCBR1` → XCBR; `CTRL/CSWI1.Pos.Oper` → Oper, then Pos, then CSWI."""
        parts = [p for p in re.split(r"[/.$\s]+", q) if p]
        for part in reversed(parts):
            entry_id = self._terms.get(_norm(part))
            if entry_id is None:
                m = _LN_NAME_RE.match(part.upper())
                if m:
                    entry_id = self._terms.get(_norm(m.group(1)))
            if entry_id is not None:
                return self._explanation(self._entries[entry_id], None, ctx)
        return None

    def _explanation(
        self, entry: Entry, key: str | None, ctx: HintContext, extra_related: Sequence[str] = ()
    ) -> Explanation:
        key = key or (entry.keys[0] if entry.keys and not entry.keys[0].endswith(":*") else None)
        hint = self._make_hint(entry, key or entry.keys[0], ctx) if entry.hint else None
        base = self._base(key) if (key and entry.when) else None
        if base is entry:
            base = None

        if entry.title:
            title = entry.title
        elif base is not None and base.title:
            title = f"{base.title} ({entry.condition_label()})"
        else:
            title = f"{key or entry.id} ({entry.condition_label()})"
        summary = entry.summary or (base.summary if base else None) or (hint.render() if hint else title)
        details = entry.details or (base.details if base else None) or ""
        if entry.next_checks is not None:
            next_checks = entry.next_checks
        else:
            next_checks = (base.next_checks if base else None) or ()
        if entry.related is not None:
            related = entry.related
        else:
            related = (base.related if base else None) or ()
        related = _dedup((*extra_related, *related, *((base.id,) if base else ())))
        related = tuple(r for r in related if r.casefold() != entry.id)

        return Explanation(
            id=entry.id,
            title=_fill(title, ctx),
            kind=entry.kind,
            summary=_fill(summary, ctx),
            details=_fill(details, ctx),
            next_checks=tuple(_fill(c, ctx) for c in next_checks),
            related=related,
            keys=tuple(k for k in entry.keys if not k.endswith(":*")),
            hint=hint,
        )

    # -- query resolution --------------------------------------------------------------------
    def _names_in(self, domain: str) -> Iterable[str]:
        table = codes.numbered_tables().get(domain)
        if table is not None:
            return table.values()
        if domain == Domain.ASSOCIATION:
            return codes.ASSOCIATION_OUTCOMES.keys()
        return sorted(set(self._code_names.get(str(domain), ())) | set(required_names().get(domain, ())))

    def _name_in(self, domain: str, name: str) -> str | None:
        n = _norm(name)
        if not n:
            return None
        for candidate in self._names_in(domain):
            if _norm(candidate) == n:
                return candidate
        return None

    def _resolve_key(self, q: str) -> str | None:
        """``domain:name`` or ``domain:number`` (with a few domain aliases) → canonical key."""
        domain_s, sep, rest = q.partition(":")
        if not sep:
            return None
        domain = _domain_aliases().get(_norm(domain_s))
        rest = rest.strip()
        if domain is None or not rest:
            return None
        tables = codes.numbered_tables()
        if rest.lstrip("-").isdigit():
            if domain in tables:
                return codes.info(domain, int(rest)).key
            return None
        name = self._name_in(domain, rest)
        if name is not None:
            return f"{domain}:{name}"
        if domain in (Domain.TOOL, Domain.CHECK) and _CODE_NAME_RE.match(rest.casefold()):
            return f"{domain}:{rest.casefold()}"  # unknown tool/check code: wildcard entry
        if domain in tables and re.fullmatch(r"unknown-\d+", rest.casefold()):
            return f"{domain}:{rest.casefold()}"
        return None

    def _resolve_constant(self, q: str) -> str | None:
        upper = q.upper()
        for prefix, domain in codes.constant_prefixes():
            if upper.startswith(prefix):
                rest = upper[len(prefix) :].replace("NONE_EXISTENT", "NON_EXISTENT")
                name = self._name_in(domain, rest)
                if name is not None:
                    return f"{domain}:{name}"
        return None

    def _bare_name_keys(self, q: str) -> list[str]:
        """All keys whose name matches ``q``, in :func:`bare_name_preference` order."""
        out: list[str] = []
        for domain in bare_name_preference():
            name = self._name_in(domain, q)
            if name is not None:
                out.append(f"{domain}:{name}")
        return out

    # -- search ------------------------------------------------------------------------------
    def search(self, text: str, limit: int = 10) -> list[Explanation]:
        """Substring and fuzzy search over ids, terms, keys, titles and text, best first."""
        n = _norm(text)
        words = [w for w in re.split(r"\W+", text.casefold()) if w]
        if not n:
            return []
        scored: list[tuple[float, int, Entry]] = []
        for entry in self._entries.values():
            if entry.when:  # variants are reachable through their base entry
                continue
            names = [entry.id, *entry.terms, *entry.keys, entry.title or ""]
            normed = [_norm(x) for x in names if _norm(x)]
            body = " ".join(x for x in (entry.summary, entry.details) if x).casefold()
            title = (entry.title or "").casefold()
            score = 0.0
            if n in normed:
                score = 100
            elif any(n in x for x in normed):
                score = 70
            elif words and all(w in title for w in words):
                score = 50
            elif words and all(w in title or w in body for w in words):
                score = 30
            if score < 70 and normed:
                ratio = max(difflib.SequenceMatcher(None, n, x).ratio() for x in normed)
                if ratio >= 0.75:
                    score = max(score, 60 * ratio)
            if score:
                scored.append((score, entry.index, entry))
        scored.sort(key=lambda t: (-t[0], t[1]))
        return [self._explanation(e, None, HintContext()) for _, _, e in scored[:limit]]

    # -- coverage and lint -------------------------------------------------------------------
    def coverage(self) -> dict[str, list[str]]:
        """Domain → keys that have no context-free entry (EXP-7). Empty lists mean full coverage."""
        out: dict[str, list[str]] = {}
        for domain, names in required_names().items():
            missing = []
            for name in names:
                key = f"{domain}:{name}"
                if not any(not e.when for e in self._by_key.get(key, ())):
                    missing.append(key)
            out[str(domain)] = missing
        return out

    def lint(self) -> list[str]:
        """Authoring problems that are legal but probably unintended. Empty is good."""
        problems: list[str] = []
        for entry in self._entries.values():
            for ref in entry.related or ():
                if self.explain(ref) is None:
                    problems.append(f"{entry.source}: {entry.id}: related {ref!r} does not resolve")
            if entry.when:
                for key in entry.keys:
                    if key.endswith(":*"):
                        continue
                    if not any(not e.when for e in self._by_key.get(key, ())):
                        problems.append(
                            f"{entry.source}: {entry.id}: context variant for {key} has no context-free "
                            "entry to inherit from"
                        )
        for key, entries in self._by_key.items():
            for i, a in enumerate(entries):
                for b in entries[i + 1 :]:
                    if (a.priority, a.specificity) != (b.priority, b.specificity):
                        continue
                    if self._can_both_match(a, b):
                        problems.append(
                            f"{key}: entries {a.id!r} and {b.id!r} can match the same context with equal "
                            f"rank; {b.id!r} wins only because it comes later"
                        )
        bare = {_norm(n) for d in bare_name_preference() for n in self._names_in(d)}
        for term, entry_id in self._terms.items():
            if term in bare:
                problems.append(f"{entry_id}: term {term!r} hides the error name of the same spelling")
        return problems

    @staticmethod
    def _can_both_match(a: Entry, b: Entry) -> bool:
        conds_b = {c.field: c for c in b.when}
        for c in a.when:
            other = conds_b.get(c.field)
            if other is not None and not c.overlaps(other):
                return False
        return True


@functools.cache
def default_catalogue() -> Catalogue:
    """The built-in catalogue, loaded once per process."""
    return Catalogue.load()
