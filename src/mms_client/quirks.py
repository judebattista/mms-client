"""Known vendor / model / firmware quirks (IDN-9).

A YAML data file keyed by device identity (see the header of ``mms_client/data/quirks.yaml`` for
the format). The built-in file starts empty and grows from the spikes (RSK-6) and incident files
(LOG-3); extra files can be layered on top. Checks and hints look entries up by the identity the
device reports (MMS Identify: vendor, model, revision).

Lookup rule: an entry matches when its ``vendor`` and ``model`` patterns match and, if it has one,
its ``firmware`` pattern matches (an entry with a firmware condition never matches an unknown
firmware). Patterns are exact strings or globs, compared case-insensitively after stripping
spaces. The most specific matching entry wins: each exact field scores 2, each glob field 1 (a
bare ``*`` scores 0); at equal score the entry loaded last wins, so layered files override the
built-in one.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import fnmatch
import importlib.resources
import os
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

SCHEMA_VERSION = 1
MATCH_FIELDS = ("vendor", "model", "firmware")
KNOWN_FIELDS = (
    "max_associations",
    "slot_release_after_abrupt_disconnect_s",
    "refusal_behaviour",
    "rcb_reservation_notes",
    "resv_tms_behaviour",
    "setting_group_notes",
    "identity_notes",
    "notes",
    "source",
    "added",
)
_GLOB_CHARS = set("*?[")


class QuirksError(ValueError):
    """A quirks file is malformed (the message names the file and entry)."""


def _norm(s: str | None) -> str:
    return (s or "").strip().casefold()


def _is_glob(pattern: str) -> bool:
    return any(c in _GLOB_CHARS for c in pattern)


def _field_score(pattern: str | None) -> int:
    if pattern is None:
        return 0
    if pattern.strip() == "*":
        return 0
    return 1 if _is_glob(pattern) else 2


def _matches(pattern: str | None, value: str | None) -> bool:
    if pattern is None:
        return True
    if value is None:
        return pattern.strip() == "*"
    p, v = _norm(pattern), _norm(value)
    return fnmatch.fnmatchcase(v, p) if _is_glob(p) else v == p


@dataclass(frozen=True)
class QuirkInfo:
    """One quirks entry. ``origin`` is the file (and position) it came from."""

    vendor: str
    model: str
    firmware: str | None = None
    max_associations: int | None = None
    slot_release_after_abrupt_disconnect_s: float | None = None
    refusal_behaviour: str | None = None
    rcb_reservation_notes: str | None = None
    resv_tms_behaviour: str | None = None
    setting_group_notes: str | None = None
    identity_notes: str | None = None
    notes: str | None = None
    source: str | None = None
    added: str | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)
    origin: str = ""

    @property
    def specificity(self) -> int:
        return _field_score(self.vendor) + _field_score(self.model) + _field_score(self.firmware)

    def matches(self, vendor: str | None, model: str | None, firmware: str | None) -> bool:
        return (
            _matches(self.vendor, vendor)
            and _matches(self.model, model)
            and _matches(self.firmware, firmware)
        )

    def describe_match(self) -> str:
        s = f"{self.vendor} {self.model}"
        return s + (f" firmware {self.firmware}" if self.firmware else " (any firmware)")

    def to_json(self) -> dict[str, Any]:
        d: dict[str, Any] = {"match": {"vendor": self.vendor, "model": self.model}}
        if self.firmware is not None:
            d["match"]["firmware"] = self.firmware
        for k in KNOWN_FIELDS:
            v = getattr(self, k)
            if v is not None:
                d[k] = v
        d.update(self.extra)
        return d

    @classmethod
    def from_dict(cls, entry: Mapping[str, Any], origin: str = "") -> QuirkInfo:
        if not isinstance(entry, Mapping):
            raise QuirksError(f"{origin}: an entry must be a mapping, got {type(entry).__name__}")
        match = entry.get("match")
        if not isinstance(match, Mapping):
            raise QuirksError(f"{origin}: missing `match` block")
        unknown = set(match) - set(MATCH_FIELDS)
        if unknown:
            raise QuirksError(f"{origin}: unknown match field(s) {sorted(unknown)}")
        for k in ("vendor", "model"):
            if not isinstance(match.get(k), str) or not match[k].strip():
                raise QuirksError(f"{origin}: `match.{k}` is required (a string or glob pattern)")
        fw = match.get("firmware")
        if fw is not None and not isinstance(fw, str | int | float):
            raise QuirksError(f"{origin}: `match.firmware` must be a string")
        kw: dict[str, Any] = {}
        for k in KNOWN_FIELDS:
            if k in entry and entry[k] is not None:
                kw[k] = entry[k]
        if "max_associations" in kw and (
            not isinstance(kw["max_associations"], int)
            or isinstance(kw["max_associations"], bool)
            or kw["max_associations"] < 1
        ):
            raise QuirksError(f"{origin}: `max_associations` must be a positive integer")
        if "slot_release_after_abrupt_disconnect_s" in kw:
            v = kw["slot_release_after_abrupt_disconnect_s"]
            if not isinstance(v, int | float) or isinstance(v, bool) or v < 0:
                raise QuirksError(f"{origin}: `slot_release_after_abrupt_disconnect_s` must be a number >= 0")
            kw["slot_release_after_abrupt_disconnect_s"] = float(v)
        for k in KNOWN_FIELDS:
            if k in kw and k not in ("max_associations", "slot_release_after_abrupt_disconnect_s"):
                kw[k] = str(kw[k])  # dates and version-like numbers become text
        extra = {k: v for k, v in entry.items() if k not in KNOWN_FIELDS and k != "match"}
        return cls(
            vendor=str(match["vendor"]),
            model=str(match["model"]),
            firmware=None if fw is None else str(fw),
            extra=extra,
            origin=origin,
            **kw,
        )


def _load_text(text: str, origin: str) -> list[QuirkInfo]:
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise QuirksError(f"{origin}: not valid YAML ({e})") from e
    if doc is None:
        return []
    if not isinstance(doc, Mapping):
        raise QuirksError(f"{origin}: the top level must be a mapping with `schema_version` and `quirks`")
    version = doc.get("schema_version", SCHEMA_VERSION)
    if version != SCHEMA_VERSION:
        raise QuirksError(f"{origin}: unsupported schema_version {version!r} (expected {SCHEMA_VERSION})")
    entries = doc.get("quirks")
    if entries is None:
        entries = []
    if not isinstance(entries, list):
        raise QuirksError(f"{origin}: `quirks` must be a list")
    return [QuirkInfo.from_dict(e, f"{origin}#{i}") for i, e in enumerate(entries)]


@dataclass
class QuirkDB:
    """All loaded entries, in load order."""

    entries: list[QuirkInfo] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.entries)

    def matches(self, vendor: str | None, model: str | None, firmware: str | None = None) -> list[QuirkInfo]:
        """Every matching entry, most specific first (later-loaded first at equal specificity)."""
        found = [
            (e.specificity, i, e) for i, e in enumerate(self.entries) if e.matches(vendor, model, firmware)
        ]
        found.sort(key=lambda t: (t[0], t[1]), reverse=True)
        return [e for _, _, e in found]

    def lookup(self, vendor: str | None, model: str | None, firmware: str | None = None) -> QuirkInfo | None:
        """The most specific entry for this identity, or None."""
        if not vendor and not model:
            return None
        found = self.matches(vendor, model, firmware)
        return found[0] if found else None

    def resolve(self, vendor: str | None, model: str | None, firmware: str | None = None) -> QuirkInfo | None:
        """Like :meth:`lookup`, but fields the winning entry leaves empty are filled from the
        less specific matches (e.g. a model-wide ``max_associations`` under a firmware-specific
        entry). ``origin`` lists every contributing entry."""
        found = self.matches(vendor, model, firmware) if (vendor or model) else []
        if not found:
            return None
        best = found[0]
        filled: dict[str, Any] = {}
        origins = [best.origin]
        for k in KNOWN_FIELDS:
            if getattr(best, k) is None:
                for e in found[1:]:
                    if getattr(e, k) is not None:
                        filled[k] = getattr(e, k)
                        if e.origin not in origins:
                            origins.append(e.origin)
                        break
        if not filled:
            return best
        return dataclasses.replace(best, origin=", ".join(origins), **filled)

    def add_file(self, path: str | os.PathLike[str]) -> None:
        p = Path(path)
        try:
            text = p.read_text(encoding="utf-8")
        except OSError as e:
            raise QuirksError(f"{p}: cannot read ({e})") from e
        self.entries.extend(_load_text(text, str(p)))
        self.sources.append(str(p))

    @staticmethod
    def append_entry(path: str | os.PathLike[str], entry: Mapping[str, Any]) -> QuirkInfo:
        """Append one entry to a quirks file, keeping its comments; create the file if needed.

        ``entry`` uses the file format (``match`` block plus fields). ``source`` is required so
        that every recorded quirk can be traced to a spike or incident; ``added`` defaults to today.
        The file is rewritten atomically and re-read to prove the entry round-trips.
        """
        entry = dict(entry)
        if not entry.get("source"):
            raise QuirksError(
                "a quirks entry needs `source` (the spike or incident file that established it)"
            )
        entry.setdefault("added", _dt.date.today().isoformat())
        info = QuirkInfo.from_dict(entry, "new entry")  # validate before touching the file
        p = Path(path)
        if p.exists():
            text = p.read_text(encoding="utf-8")
            existing = _load_text(text, str(p))
            doc = yaml.safe_load(text) or {}
            if "quirks" not in doc:
                text = text.rstrip("\n") + ("\n" if text.strip() else "")
                text += (
                    "" if "schema_version" in doc else f"schema_version: {SCHEMA_VERSION}\n"
                ) + "quirks:\n"
            elif list(doc)[-1] != "quirks":
                raise QuirksError(
                    f"{p}: `quirks:` must be the last top-level key so that entries can be appended"
                )
        else:
            text = (
                "# Quirks file (IDN-9); see mms_client/data/quirks.yaml for the format.\n"
                f"schema_version: {SCHEMA_VERSION}\nquirks:\n"
            )
            existing = []
        block = yaml.safe_dump(
            [info.to_json()], sort_keys=False, allow_unicode=True, default_flow_style=False
        )
        new_text = text if text.endswith("\n") else text + "\n"
        new_text += "".join("  " + line + "\n" for line in block.rstrip("\n").split("\n"))
        try:
            after = _load_text(new_text, str(p))
        except QuirksError as e:
            raise QuirksError(
                f"{p}: cannot append (write the list in block style, `quirks:` followed by `- ` items): {e}"
            ) from e
        if len(after) != len(existing) + 1 or after[-1].to_json() != info.to_json():
            raise QuirksError(f"{p}: the appended entry did not read back correctly; file left unchanged")
        fd, tmp = tempfile.mkstemp(dir=p.parent if str(p.parent) else ".", prefix=".quirks-", suffix=".yaml")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(new_text)
            os.replace(tmp, p)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return after[-1]


def builtin_quirks_text() -> str:
    return importlib.resources.files("mms_client").joinpath("data", "quirks.yaml").read_text(encoding="utf-8")


def load_quirks(extra_paths: Iterable[str | os.PathLike[str]] = ()) -> QuirkDB:
    """The built-in quirks file followed by ``extra_paths`` (later files win at equal specificity)."""
    db = QuirkDB()
    db.entries.extend(_load_text(builtin_quirks_text(), "mms_client/data/quirks.yaml"))
    db.sources.append("mms_client/data/quirks.yaml")
    for p in extra_paths:
        db.add_file(p)
    return db
