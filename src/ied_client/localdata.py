"""Local field data (FLD-1 … FLD-6): quirks and explanation entries added on this machine.

An offline laptop cannot push to the repository, so what its operators learn about the rack is kept
in local layers that are loaded on top of the data built into the package:

    machine  /etc/ied-client/                      shared by every user (writing needs root)
    user     ${XDG_CONFIG_HOME:-~/.config}/ied-client/

``IED_CLIENT_LOCAL_DIRS`` (directories separated by ``:``) replaces both: the first is the machine
layer, the last the user layer. Each layer holds::

    hints/*.yaml               explanation catalogue files (format: ied_client/data/hints.yaml)
    quirks/<protocol>/*.yaml   quirks files of one protocol module (format: that module's quirks.yaml)
    incidents/                 incident files cited by quirk ``source`` fields

Later layers win: a catalogue entry replaces an earlier one with the same id, and a quirk beats an
equally specific earlier one (FLD-1). A local file that does not load is left out with a warning;
it never disables the tool (FLD-3).

The way back: ``local export`` writes every layer into one *field bundle* (FLD-4), which
``tools/ingest_field_data.py`` reviews into the repository (FLD-5). Every reviewed entry is recorded
by its fingerprint in the *ledger* (``ied_client/data/field-ledger.yaml``) that ships with the next
package, so ``local status`` and ``local prune`` on the laptops can tell which local entries the
package now covers, or why one was not taken (FLD-6).

This module never imports a protocol module (ARC-5): callers pass protocol names.
"""

from __future__ import annotations

import datetime as _dt
import functools
import getpass
import hashlib
import importlib.resources
import json
import os
import re
import socket
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ied_client import TOOL_NAME, __version__, env

from . import yamledit

MACHINE_DIR = Path("/etc") / TOOL_NAME
HINTS_DIR = "hints"
QUIRKS_DIR = "quirks"
INCIDENTS_DIR = "incidents"
DEFAULT_FILE = "local.yaml"
KINDS = ("hints", "quirks")
LIST_KEY = {"hints": "entries", "quirks": "quirks"}

BUNDLE_KIND = f"{TOOL_NAME}-field-data"
BUNDLE_SCHEMA_VERSION = 1
LEDGER_SCHEMA_VERSION = 1
LEDGER_DECISIONS = ("accepted", "rejected")


class LocalDataError(ValueError):
    """A local file, bundle or ledger is unusable (the message names it)."""


# ---------------------------------------------------------------------------------------- layers
@dataclass(frozen=True)
class Layer:
    name: str  # "machine" | "user"
    path: Path

    def hints_dir(self) -> Path:
        return self.path / HINTS_DIR

    def quirks_dir(self, protocol: str) -> Path:
        return self.path / QUIRKS_DIR / protocol

    def incidents_dir(self) -> Path:
        return self.path / INCIDENTS_DIR

    def default_file(self, kind: str, protocol: str | None = None) -> Path:
        if kind == "hints":
            return self.hints_dir() / DEFAULT_FILE
        if not protocol:
            raise ValueError("a quirks file belongs to a protocol module")
        return self.quirks_dir(protocol) / DEFAULT_FILE

    def writable(self) -> bool:
        """Whether this user can create or change files here (the machine layer needs root)."""
        p = self.path
        while not p.exists() and p != p.parent:
            p = p.parent
        return os.access(p, os.W_OK | os.X_OK)


def user_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / TOOL_NAME


def layers() -> list[Layer]:
    """The local layers, in load order (later wins)."""
    override = env("LOCAL_DIRS")
    if override:
        dirs = [Path(p).expanduser() for p in override.split(os.pathsep) if p.strip()]
        if not dirs:
            return []
        if len(dirs) == 1:
            return [Layer("user", dirs[0])]
        return [Layer("machine", dirs[0]), *(Layer(f"local{i}", d) for i, d in enumerate(dirs[1:-1], 2)),
                Layer("user", dirs[-1])]
    return [Layer("machine", MACHINE_DIR), Layer("user", user_dir())]


def layer(system: bool = False) -> Layer:
    """The layer a command writes to: the user's, or the machine's with ``--system``."""
    ls = layers()
    if not ls:
        raise LocalDataError(f"no local data directories ({TOOL_NAME.upper().replace('-', '_')}_LOCAL_DIRS is empty)")
    return ls[0] if system else ls[-1]


@dataclass(frozen=True)
class LocalFile:
    layer: Layer
    path: Path
    kind: str  # "hints" | "quirks"
    protocol: str | None = None  # quirks only

    @property
    def rel(self) -> str:
        """Path inside the layer, e.g. ``hints/local.yaml`` or ``quirks/mms/local.yaml``."""
        return self.path.relative_to(self.layer.path).as_posix()

    @property
    def label(self) -> str:
        return f"{self.layer.name}:{self.rel}"


def _yaml_files(d: Path) -> list[Path]:
    try:
        return sorted(p for p in d.iterdir() if p.suffix in (".yaml", ".yml") and p.is_file())
    except OSError:
        return []


def local_files(kind: str | None = None, protocol: str | None = None) -> list[LocalFile]:
    """Every local data file, in load order (layer by layer, then by file name)."""
    out: list[LocalFile] = []
    for ly in layers():
        if kind in (None, "hints"):
            out.extend(LocalFile(ly, p, "hints") for p in _yaml_files(ly.hints_dir()))
        if kind in (None, "quirks"):
            qdir = ly.path / QUIRKS_DIR
            try:
                protos = sorted(p.name for p in qdir.iterdir() if p.is_dir())
            except OSError:
                protos = []
            for proto in protos:
                if protocol is None or proto == protocol:
                    out.extend(LocalFile(ly, p, "quirks", proto) for p in _yaml_files(qdir / proto))
    return out


def catalogue_files() -> list[Path]:
    return [f.path for f in local_files("hints")]


def quirks_files(protocol: str) -> list[Path]:
    return [f.path for f in local_files("quirks", protocol)]


def incident_files() -> list[tuple[Layer, Path]]:
    out = []
    for ly in layers():
        try:
            out.extend((ly, p) for p in sorted(ly.incidents_dir().iterdir()) if p.is_file())
        except OSError:
            pass
    return out


def is_local(label: str) -> bool:
    """Whether a catalogue/quirks source label names a file in a local layer."""
    try:
        p = Path(label.split("#", 1)[0]).resolve()
    except (OSError, ValueError):
        return False
    for ly in layers():
        try:
            p.relative_to(ly.path.resolve())
            return True
        except (OSError, ValueError):
            continue
    return False


# ---------------------------------------------------------------------------------------- files
def atomic_write(path: Path, text: str, *, mode: int | None = None) -> None:
    """Write ``text`` to ``path`` through a temporary file in the same directory.

    The file keeps its mode, or gets 0644 (the machine layer is read by every user).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if mode is None:
        try:
            mode = path.stat().st_mode & 0o7777
        except OSError:
            mode = 0o644
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def raw_entries(text: str, kind: str) -> list[Any]:
    """The items of a file's list (``entries`` / ``quirks``) as plain YAML data."""
    doc = yaml.safe_load(text) if text.strip() else None
    if not isinstance(doc, Mapping):
        return []
    items = doc.get(LIST_KEY[kind])
    return list(items) if isinstance(items, list) else []


def fingerprint(entry: Any) -> str:
    """A stable identifier of one entry's content (FLD-6): sha256 of its canonical JSON."""
    canon = json.dumps(entry, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]


def quirk_match_key(entry: Mapping[str, Any]) -> tuple[str, str, str | None]:
    """(vendor, model, firmware) of a quirks entry, normalised for comparison."""
    m = entry.get("match") or {}
    fw = m.get("firmware")
    return (
        str(m.get("vendor", "")).strip().casefold(),
        str(m.get("model", "")).strip().casefold(),
        None if fw is None else str(fw).strip().casefold(),
    )


def entry_ref(kind: str, entry: Any) -> str:
    """How an entry is named to a person: a catalogue id, or a quirk's match."""
    if not isinstance(entry, Mapping):
        return "?"
    if kind == "hints":
        return str(entry.get("id", "?"))
    m = entry.get("match") or {}
    fw = m.get("firmware")
    return f"{m.get('vendor', '?')} / {m.get('model', '?')} / " + (f"firmware {fw}" if fw is not None else "any firmware")


# ---------------------------------------------------------------------------------------- ledger
@dataclass(frozen=True)
class LedgerRow:
    fingerprint: str
    kind: str  # "hint" | "quirk"
    ref: str
    decision: str  # "accepted" | "rejected"
    reason: str | None = None
    bundle: str | None = None
    reviewed: str | None = None

    def to_json(self) -> dict[str, Any]:
        d: dict[str, Any] = {"fingerprint": self.fingerprint, "kind": self.kind, "ref": self.ref,
                             "decision": self.decision}
        for k in ("reason", "bundle", "reviewed"):
            if getattr(self, k):
                d[k] = getattr(self, k)
        return d


def parse_ledger(text: str, label: str = "field-ledger.yaml") -> dict[str, LedgerRow]:
    doc = yaml.safe_load(text) if text.strip() else None
    if doc is None:
        return {}
    if not isinstance(doc, Mapping) or doc.get("schema_version", LEDGER_SCHEMA_VERSION) != LEDGER_SCHEMA_VERSION:
        raise LocalDataError(f"{label}: not a ledger of schema_version {LEDGER_SCHEMA_VERSION}")
    rows = doc.get("reviewed") or []
    if not isinstance(rows, list):
        raise LocalDataError(f"{label}: `reviewed` must be a list")
    out: dict[str, LedgerRow] = {}
    for i, r in enumerate(rows):
        if not isinstance(r, Mapping) or not r.get("fingerprint") or r.get("decision") not in LEDGER_DECISIONS:
            raise LocalDataError(f"{label}: row #{i + 1} needs `fingerprint` and `decision` (accepted or rejected)")
        out[str(r["fingerprint"])] = LedgerRow(
            fingerprint=str(r["fingerprint"]),
            kind=str(r.get("kind", "")),
            ref=str(r.get("ref", "")),
            decision=str(r["decision"]),
            reason=None if r.get("reason") is None else str(r["reason"]),
            bundle=None if r.get("bundle") is None else str(r["bundle"]),
            reviewed=None if r.get("reviewed") is None else str(r["reviewed"]),
        )
    return out


@functools.cache
def ledger() -> dict[str, LedgerRow]:
    """The ledger built into this package: fingerprint → review decision (FLD-6)."""
    node = importlib.resources.files("ied_client").joinpath("data", "field-ledger.yaml")
    try:
        text = node.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return {}
    return parse_ledger(text, "ied_client/data/field-ledger.yaml")


# ---------------------------------------------------------------------------------------- status
LOCAL_ONLY = "local-only"
OVERRIDES = "overrides-built-in"
INGESTED = "ingested"
REJECTED = "rejected"

STATE_TEXT = {
    LOCAL_ONLY: "local only",
    OVERRIDES: "overrides a built-in entry",
    INGESTED: "reviewed into this package",
    REJECTED: "reviewed, not accepted",
}


@dataclass
class EntryStatus:
    kind: str  # "hints" | "quirks"
    index: int  # position in its file's list
    ref: str
    fingerprint: str
    state: str
    detail: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {"index": self.index, "ref": self.ref, "fingerprint": self.fingerprint, "state": self.state,
                "detail": self.detail}


@dataclass
class FileStatus:
    file: LocalFile
    valid: bool
    error: str | None = None
    entries: list[EntryStatus] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {"layer": self.file.layer.name, "path": str(self.file.path), "kind": self.file.kind,
                "protocol": self.file.protocol, "valid": self.valid, "error": self.error,
                "entries": [e.to_json() for e in self.entries]}


def _builtin_catalogue_ids() -> set[str]:
    from ied_client.explain.catalogue import Catalogue, _builtin_sources

    return {e.id for e in Catalogue.from_texts((lbl, t, True) for lbl, t in _builtin_sources()).entries}


def _builtin_quirk_matches(protocol: str) -> set[tuple[str, str, str | None]]:
    from ied_client.protocol import registry as protocols

    try:
        module = protocols.get(protocol)
    except LookupError:
        return set()
    out = set()
    for _label, text in module.quirks_sources():
        out.update(quirk_match_key(e) for e in raw_entries(text, "quirks") if isinstance(e, Mapping))
    return out


def validate_text(kind: str, text: str, label: str) -> None:
    """Raise :class:`LocalDataError` if ``text`` would not load as a local ``kind`` file."""
    if kind == "hints":
        from ied_client.explain.catalogue import Catalogue, CatalogueError, _builtin_sources

        try:
            Catalogue.from_texts([*((lbl, t, True) for lbl, t in _builtin_sources()), (label, text, False)])
        except CatalogueError as exc:
            raise LocalDataError(str(exc)) from None
    else:
        from ied_client.quirks import QuirksError, _load_text

        try:
            _load_text(text, label)
        except QuirksError as exc:
            raise LocalDataError(str(exc)) from None
    try:
        yamledit.items(text, LIST_KEY[kind])
    except yamledit.YamlEditError as exc:
        raise LocalDataError(f"{label}: {exc}") from None


def status(files: Iterable[LocalFile] | None = None) -> list[FileStatus]:
    """Each local file: does it load, and what is each entry's standing against the package?"""
    files = local_files() if files is None else list(files)
    book = ledger()
    ids: set[str] | None = None
    matches: dict[str, set[tuple[str, str, str | None]]] = {}
    out: list[FileStatus] = []
    for f in files:
        try:
            text = f.path.read_text(encoding="utf-8")
        except OSError as exc:
            out.append(FileStatus(f, False, f"cannot read: {exc}"))
            continue
        fs = FileStatus(f, True)
        try:
            validate_text(f.kind, text, str(f.path))
        except LocalDataError as exc:
            fs.valid, fs.error = False, str(exc)
        try:
            entries = raw_entries(text, f.kind)
        except yaml.YAMLError:
            entries = []
        for i, e in enumerate(entries):
            fp = fingerprint(e)
            ref = entry_ref(f.kind, e)
            row = book.get(fp)
            if row is not None and row.decision == "accepted":
                st = EntryStatus(f.kind, i, ref, fp, INGESTED, f"accepted {row.reviewed or ''}".strip()
                                 + (f" from {row.bundle}" if row.bundle else ""))
            elif row is not None:
                st = EntryStatus(f.kind, i, ref, fp, REJECTED, row.reason or "no reason recorded")
            elif f.kind == "hints":
                if ids is None:
                    ids = _builtin_catalogue_ids()
                eid = str(e.get("id", "")).casefold() if isinstance(e, Mapping) else ""
                st = EntryStatus(f.kind, i, ref, fp, OVERRIDES if eid in ids else LOCAL_ONLY)
            else:
                proto = f.protocol or ""
                if proto not in matches:
                    matches[proto] = _builtin_quirk_matches(proto)
                key = quirk_match_key(e) if isinstance(e, Mapping) else ("", "", None)
                st = EntryStatus(f.kind, i, ref, fp, OVERRIDES if key in matches[proto] else LOCAL_ONLY)
            fs.entries.append(st)
        out.append(fs)
    return out


# ---------------------------------------------------------------------------------------- prune
def prune_plan(system: bool = False) -> list[tuple[LocalFile, list[EntryStatus]]]:
    """Entries of the chosen layer that the package's ledger has reviewed (accepted or rejected)."""
    target = layer(system)
    plan = []
    for fs in status(f for f in local_files() if f.layer == target):
        done = [e for e in fs.entries if e.state in (INGESTED, REJECTED)]
        if done and fs.valid:
            plan.append((fs.file, done))
    return plan


def apply_prune(plan: Iterable[tuple[LocalFile, list[EntryStatus]]]) -> int:
    removed = 0
    for f, entries in plan:
        text = f.path.read_text(encoding="utf-8")
        new = yamledit.remove(text, LIST_KEY[f.kind], [e.index for e in entries])
        validate_text(f.kind, new, str(f.path))
        atomic_write(f.path, new)
        removed += len(entries)
    return removed


# ---------------------------------------------------------------------------------------- templates
def _leading_comments(text: str) -> list[str]:
    out = []
    for ln in text.splitlines():
        if ln.startswith("#") or not ln.strip():
            out.append(ln)
        else:
            break
    while out and not out[-1].strip():
        out.pop()
    return out


def hints_template(path: Path) -> str:
    """A new local catalogue file: what it is, the format (from the built-in hints.yaml), no entries."""
    builtin = importlib.resources.files("ied_client").joinpath("data", "hints.yaml").read_text(encoding="utf-8")
    header = _leading_comments(builtin)
    start = next((i for i, ln in enumerate(header) if "FILE FORMAT" in ln), 0)
    start = max(0, start - 1)
    end = max((i for i, ln in enumerate(header) if ln.startswith("# ====")), default=len(header))
    fmt = header[start:end] if end > start else header
    intro = [
        f"# Local explanation catalogue entries for {TOOL_NAME} (FLD-1): {path}",
        "#",
        "# Loaded after the catalogue built into the package. An entry with the same `id` as a built-in",
        "# entry replaces it on this machine; `explain` marks entries from this file as [local].",
        f"# To change a built-in entry, start from a copy: `{TOOL_NAME} local edit hints --from <id>`.",
        f"# `{TOOL_NAME} local export` takes this file back to the repository for review.",
        "#",
    ]
    return "\n".join([*intro, *fmt, "", "version: 1", "entries:", ""]) + "\n"


def quirks_template(path: Path, protocol: str, module_sources: Iterable[tuple[str, str]] = ()) -> str:
    """A new local quirks file for ``protocol``: what it is, the module's format header, no entries."""
    header: list[str] = []
    for _label, text in module_sources:
        header = _leading_comments(text)
        break
    intro = [
        f"# Local quirks for protocol module {protocol!r} (FLD-1): {path}",
        "#",
        "# Loaded after the quirks built into the package; at equal specificity an entry here wins.",
        f"# `{TOOL_NAME} local add-quirk <device>` fills `match` with the identity the device reports.",
        "# Record only what was observed on a real device, and say where in `source` (an incident file",
        f"# in {INCIDENTS_DIR}/ next to this directory travels with `{TOOL_NAME} local export`).",
        "#",
        "# The format, from the protocol module's own quirks file:",
        "#",
    ]
    return "\n".join([*intro, *header, "", "schema_version: 1", "quirks:"]) + "\n"


# ---------------------------------------------------------------------------------------- bundle
def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _cited_incidents(files: Iterable[LocalFile]) -> list[tuple[Layer, Path]]:
    """Existing files named by quirk ``source`` fields (relative to the layer, or absolute)."""
    out = []
    for f in files:
        if f.kind != "quirks":
            continue
        try:
            entries = raw_entries(f.path.read_text(encoding="utf-8"), "quirks")
        except (OSError, yaml.YAMLError):
            continue
        for e in entries:
            src = e.get("source") if isinstance(e, Mapping) else None
            if not isinstance(src, str) or not src.strip():
                continue
            p = Path(src.strip()).expanduser()
            p = p if p.is_absolute() else f.layer.path / p
            if p.is_file():
                out.append((f.layer, p))
    return out


def default_bundle_name(now: _dt.datetime | None = None) -> str:
    now = now or _dt.datetime.now()
    host = re.sub(r"[^A-Za-z0-9._-]+", "-", socket.gethostname()) or "host"
    return f"{TOOL_NAME}-field-{host}-{now:%Y%m%d}.json"


def build_bundle() -> dict[str, Any]:
    """Every local layer as one self-contained field bundle (FLD-4)."""
    from ied_client.buildinfo import build_info

    files = local_files()
    statuses = {fs.file.path: fs for fs in status(files)}
    out_files = []
    entries = []
    for f in files:
        text = f.path.read_text(encoding="utf-8")
        fs = statuses[f.path]
        out_files.append({"layer": f.layer.name, "path": f.rel, "kind": f.kind, "protocol": f.protocol,
                          "sha256": _sha256(text), "valid": fs.valid, "error": fs.error, "text": text})
        entries.extend({"kind": f.kind, "file": f"{f.layer.name}:{f.rel}", **e.to_json()} for e in fs.entries)
    seen: set[Path] = set()
    incidents = []
    for ly, p in [*incident_files(), *_cited_incidents(files)]:
        rp = p.resolve()
        if rp in seen:
            continue
        seen.add(rp)
        try:
            content = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        try:
            rel = p.resolve().relative_to(ly.path.resolve()).as_posix()
        except ValueError:
            rel = str(p)
        incidents.append({"layer": ly.name, "path": rel, "name": p.name, "sha256": _sha256(content), "content": content})
    try:
        user = getpass.getuser()
    except Exception:  # pragma: no cover - no passwd entry
        user = None
    info = build_info()
    return {
        "kind": BUNDLE_KIND,
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "created_at": _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "host": socket.gethostname(),
        "user": user,
        "tool": {"name": TOOL_NAME, "version": __version__,
                 "package_version": info.get("package_version") if info else None,
                 "git_commit": info.get("git_commit") if info else None},
        "layers": [{"name": ly.name, "path": str(ly.path)} for ly in layers()],
        "files": out_files,
        "incidents": incidents,
        "entries": entries,
    }


def read_bundle(path: str | Path) -> dict[str, Any]:
    """Load a field bundle and check its kind, version and checksums."""
    p = Path(path)
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LocalDataError(f"{p}: cannot read the bundle ({exc})") from exc
    if not isinstance(doc, dict) or doc.get("kind") != BUNDLE_KIND:
        raise LocalDataError(f"{p}: not an {TOOL_NAME} field bundle (kind {BUNDLE_KIND!r})")
    if doc.get("schema_version") != BUNDLE_SCHEMA_VERSION:
        raise LocalDataError(f"{p}: unsupported bundle schema_version {doc.get('schema_version')!r}")
    for f in doc.get("files") or []:
        if f.get("kind") not in KINDS or not isinstance(f.get("text"), str):
            raise LocalDataError(f"{p}: file entry {f.get('path')!r} is malformed")
        if _sha256(f["text"]) != f.get("sha256"):
            raise LocalDataError(f"{p}: {f.get('path')}: checksum mismatch (the bundle was changed or damaged)")
    for inc in doc.get("incidents") or []:
        if not isinstance(inc.get("content"), str) or _sha256(inc["content"]) != inc.get("sha256"):
            raise LocalDataError(f"{p}: incident {inc.get('name')!r}: checksum mismatch")
    return doc


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.") or "file"


def import_bundle(doc: Mapping[str, Any], system: bool = False) -> list[Path]:
    """Copy a bundle's files into a local layer of this machine (laptop to laptop, FLD-4).

    Files land as ``imported-<host>-<date>-<name>``, so this machine's own ``local.yaml`` (loaded
    after them) keeps the last word; incident files keep their names unless one with other content
    exists. Invalid files are skipped. Returns the files written.
    """
    target = layer(system)
    host = _safe_name(str(doc.get("host") or "host"))
    date = str(doc.get("created_at") or "")[:10].replace("-", "") or "undated"
    written: list[Path] = []
    for f in doc.get("files") or []:
        if not f.get("valid", True) or not raw_entries(f["text"], f["kind"]):
            continue
        name = f"imported-{host}-{date}-{_safe_name(Path(f['path']).name)}"
        dest = target.hints_dir() / name if f["kind"] == "hints" else target.quirks_dir(_safe_name(f.get("protocol") or "mms")) / name
        validate_text(f["kind"], f["text"], str(dest))
        atomic_write(dest, f["text"])
        written.append(dest)
    for inc in doc.get("incidents") or []:
        dest = target.incidents_dir() / _safe_name(inc.get("name") or "incident.json")
        if dest.exists():
            if dest.read_text(encoding="utf-8", errors="replace") == inc["content"]:
                continue
            dest = dest.with_name(f"{dest.stem}-{host}{dest.suffix}")
        atomic_write(dest, inc["content"])
        written.append(dest)
    return written
