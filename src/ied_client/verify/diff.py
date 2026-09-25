"""Compare snapshots (VER-6) with an optional per-experiment ignore file (VER-14).

Ignore file (YAML)::

    # attributes left out of diffs, e.g. counters
    ignore:
      - "*/MMXU1.*"                      # any layer
      - "configuration:*.OpCnt.*"        # one layer only (structure / configuration / operational)
      - "operational:*.RptEna"

Patterns are shell-style globs matched against the key (``LD/LN.DO.DA[FC]``, control block
references, dataset references, ``identity:…``).
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from ied_client.core.results import Status, worst

from .snapshot import Snapshot, model_structure

if TYPE_CHECKING:
    from ied_client.core.model import DeviceModel

LAYER_SEVERITY: dict[str, Status] = {
    "structure": Status.FAIL,
    "configuration": Status.WARN,
    "operational": Status.INFO,
}


@dataclass(slots=True)
class IgnoreRules:
    patterns: list[tuple[str | None, str]] = field(default_factory=list)  # (layer or None, glob)
    source: str | None = None

    @classmethod
    def load(cls, path: str | Path | None) -> IgnoreRules:
        if path is None:
            return cls()
        p = Path(path)
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        items = data.get("ignore", []) if isinstance(data, dict) else data
        rules = cls(source=str(p))
        for item in items or []:
            s = str(item)
            layer, sep, pat = s.partition(":")
            if sep and layer in LAYER_SEVERITY:
                rules.patterns.append((layer, pat))
            else:
                rules.patterns.append((None, s))
        return rules

    def ignored(self, layer: str, key: str) -> bool:
        return any((lay is None or lay == layer) and fnmatch.fnmatchcase(key, pat) for lay, pat in self.patterns)


@dataclass(slots=True)
class DiffEntry:
    layer: str
    section: str | None
    key: str
    change: str  # "added" | "removed" | "changed"
    old: Any
    new: Any
    severity: Status

    def to_json(self) -> dict:
        return {
            "layer": self.layer,
            "section": self.section,
            "key": self.key,
            "change": self.change,
            "old": self.old,
            "new": self.new,
            "severity": self.severity.value,
        }


@dataclass(slots=True)
class DiffResult:
    left: str
    right: str
    entries: list[DiffEntry] = field(default_factory=list)
    ignored: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def status(self) -> Status:
        return worst([e.severity for e in self.entries]) if self.entries else Status.PASS

    def counts(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for e in self.entries:
            c = out.setdefault(e.layer, {"added": 0, "removed": 0, "changed": 0})
            c[e.change] += 1
        return out

    def to_json(self) -> dict:
        return {
            "left": self.left,
            "right": self.right,
            "status": self.status.value,
            "counts": self.counts(),
            "ignored": self.ignored,
            "notes": self.notes,
            "entries": [e.to_json() for e in self.entries],
        }


def _flat(snap: Snapshot) -> dict[str, dict[tuple[str | None, str], Any]]:
    s = snap.structure
    structure: dict[tuple[str | None, str], Any] = {}
    for k, v in s.get("model", {}).items():
        structure[("model", k)] = v
    for ln in s.get("logical_nodes", []):
        structure[("logical_nodes", ln)] = True
    for k, v in s.get("datasets", {}).items():
        structure[("datasets", k)] = v
    for cb, attrs in s.get("control_blocks", {}).items():
        if isinstance(attrs, dict):
            for a, v in attrs.items():
                structure[("control_blocks", f"{cb}.{a}")] = v
    return {
        "structure": structure,
        "configuration": {(None, k): v for k, v in snap.configuration.items()},
        "operational": {(None, k): v for k, v in snap.operational.items()},
    }


def _equal(a: Any, b: Any) -> bool:
    if isinstance(a, float) and isinstance(b, float):
        return abs(a - b) <= 1e-6 * max(1.0, abs(a), abs(b))
    return a == b


def diff_snapshots(
    left: Snapshot, right: Snapshot, *, ignore: IgnoreRules | None = None, left_label: str = "left", right_label: str = "right"
) -> DiffResult:
    """Changes going from ``left`` (the reference / older) to ``right`` (live / newer)."""
    ignore = ignore or IgnoreRules()
    res = DiffResult(left_label, right_label)
    lf, rf = _flat(left), _flat(right)
    for layer, sev in LAYER_SEVERITY.items():
        a, b = lf[layer], rf[layer]
        for key in sorted(set(a) | set(b), key=lambda k: (k[0] or "", k[1])):
            section, name = key
            if ignore.ignored(layer, name):
                if key not in a or key not in b or not _equal(a[key], b[key]):
                    res.ignored += 1
                continue
            if key not in b:
                res.entries.append(DiffEntry(layer, section, name, "removed", a[key], None, sev))
            elif key not in a:
                res.entries.append(DiffEntry(layer, section, name, "added", None, b[key], sev))
            elif not _equal(a[key], b[key]):
                res.entries.append(DiffEntry(layer, section, name, "changed", a[key], b[key], sev))
    le = left.metadata.get("edition", {})
    re_ = right.metadata.get("edition", {})
    if le.get("value") != re_.get("value"):
        res.notes.append(f"edition differs: {le.get('value')} ({le.get('source')}) vs {re_.get('value')} ({re_.get('source')})")
    if left.unreadable_count or right.unreadable_count:
        res.notes.append(
            f"unreadable attributes: {left.unreadable_count} in {left_label}, {right.unreadable_count} in {right_label} "
            "(changes in access rights appear as value changes to/from an error)"
        )
    return res


def model_differences(reference: Snapshot, model: DeviceModel, *, ignore: IgnoreRules | None = None) -> list[DiffEntry]:
    """Differences between the model tree of a snapshot and a browsed model (no device reads). Used by
    ``diagnose`` when the reference is a snapshot (DIA-1); ``check`` compares the full snapshot."""
    ref = Snapshot(structure={
        "model": reference.structure.get("model", {}),
        "logical_nodes": reference.structure.get("logical_nodes", []),
    })
    return diff_snapshots(ref, model_structure(model), ignore=ignore, left_label="snapshot", right_label="live").entries
