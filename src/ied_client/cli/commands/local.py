"""local status / edit / add-quirk / export / import / prune: this machine's field data (FLD-1 … FLD-6).

What an operator learns about the rack on an offline laptop is kept in local layers
(:mod:`ied_client.localdata`), marked [local] wherever it is used, and taken back to the repository
as one bundle file (`local export`) for review (``tools/ingest_field_data.py``).
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from ied_client import TOOL_NAME, codes, localdata
from ied_client.localdata import INGESTED, REJECTED, STATE_TEXT, LocalDataError
from ied_client.protocol import registry as protocols

from ..context import CliContext
from ..registry import Needs, UsageError, command
from ..render import STYLE_ERROR, STYLE_NOTE, STYLE_WARN, Output
from ..result import EXIT_REFUSED, CommandResult, failure

AREA = "Local data"

# Quirks fields an operator fills in (`source` and `added` are handled separately), with a prompt each.
QUIRK_FIELDS: dict[str, str] = {
    "max_associations": "simultaneous associations the device accepts (a whole number)",
    "slot_release_after_abrupt_disconnect_s": "seconds until a slot held by a client that vanished is freed",
    "refusal_behaviour": "what the device does when all association slots are in use",
    "rcb_reservation_notes": "how report control block reservation behaves",
    "resv_tms_behaviour": "how ResvTms behaves",
    "setting_group_notes": "setting-group behaviour",
    "identity_notes": "how the identity (Identify / PhyNam / ldNs) is filled in",
    "notes": "anything else",
}


def _short(p: Path) -> str:
    try:
        return "~/" + p.relative_to(Path.home()).as_posix()
    except ValueError:
        return str(p)


def _reload_catalogue() -> None:
    from ied_client.explain import reset_default_catalogue

    reset_default_catalogue()


def _target_layer(system: bool) -> localdata.Layer:
    ly = localdata.layer(system)
    if not ly.writable():
        how = "run it with sudo" if system else "check the permissions of that directory"
        raise UsageError(f"{ly.path} is not writable for you: {how}" + ("" if system else ", or use --system with sudo"))
    return ly


def _protocol_name(ctx: CliContext) -> str:
    module = ctx.protocol_module()
    return module.name if module is not None else protocols.DEFAULT_PROTOCOL


def _plural(n: int, word: str, plural: str | None = None) -> str:
    return f"{n} {word if n == 1 else (plural or word + 's')}"


# ---------------------------------------------------------------------------------- status
@command(
    "local status",
    "Show this machine's own quirks and explanation entries: where they are, whether they load, "
    "and which ones the package has since reviewed (FLD-1, FLD-6)",
    area=AREA,
    needs=Needs.NOTHING,
    examples=("local status",),
)
def status(ctx: CliContext, args) -> CommandResult:
    from ied_client.buildinfo import build_info

    lys = localdata.layers()
    files = localdata.status()
    incidents = localdata.incident_files()
    info = build_info()
    invalid = [f for f in files if not f.valid]
    reviewed = [e for f in files for e in f.entries if e.state in (INGESTED, REJECTED)]
    data = {
        "layers": [{"name": ly.name, "path": str(ly.path), "exists": ly.path.is_dir(), "writable": ly.writable()}
                   for ly in lys],
        "files": [fs.to_json() for fs in files],
        "incidents": [{"layer": ly.name, "path": str(p)} for ly, p in incidents],
        "package": info.get("package_version") if info else None,
        "ledger_entries": len(localdata.ledger()),
        "reviewed_entries": len(reviewed),
    }

    def text(out: Output) -> None:
        out.table(
            ("layer", "directory", "access", "files"),
            [
                (ly.name, _short(ly.path),
                 ("writable" if ly.writable() else "read-only (sudo … --system)") if ly.path.is_dir() else "not created yet",
                 sum(1 for f in files if f.file.layer == ly))
                for ly in lys
            ],
            title="Local data layers (loaded after the package's own data; a later layer wins)",
        )
        for fs in files:
            out.line("")
            head = f"{fs.file.layer.name}: {_short(fs.file.path)}  ({_plural(len(fs.entries), 'entry', 'entries')})"
            if fs.valid:
                out.line(head, style="bold")
            else:
                out.line(head + "  INVALID: left out until fixed", style=STYLE_ERROR)
                out.line(f"    {fs.error}", style=STYLE_ERROR)
            for e in fs.entries:
                style = STYLE_NOTE if e.state in (INGESTED, REJECTED) else None
                detail = f": {e.detail}" if e.detail else ""
                out.line(f"    {e.ref:<44} {STATE_TEXT[e.state]}{detail}", style=style)
        if not files:
            out.note(f"No local files yet: `{TOOL_NAME} local add-quirk <device>` records a quirk, "
                     f"`{TOOL_NAME} local edit hints` adds explanation entries.")
        if incidents:
            out.line("")
            out.line(f"Incident files: {len(incidents)}", style="bold")
            for ly, p in incidents:
                out.line(f"    {ly.name}: {_short(p)}", style=STYLE_NOTE)
        out.line("")
        pkg = f"package {info['package_version']}" if info and info.get("package_version") else "a development checkout"
        out.note(f"This is {pkg}; its ledger lists {_plural(len(localdata.ledger()), 'reviewed field entry', 'reviewed field entries')}.")
        if reviewed:
            out.note(f"{_plural(len(reviewed), 'local entry has', 'local entries have')} been reviewed into this package: "
                     f"`{TOOL_NAME} local prune` removes them.")
        if files:
            out.note(f"`{TOOL_NAME} local export` puts all of this in one file to take back to the repository.")

    if invalid:
        return failure(codes.tool("local-data-invalid"),
                       f"{_plural(len(invalid), 'local file is', 'local files are')} invalid and left out; "
                       f"fix with `{TOOL_NAME} local edit`", data=data, text=text)
    return CommandResult(data=data, text=text)


# ---------------------------------------------------------------------------------- edit
def _edit_args(p, oneshot: bool) -> None:
    p.add_argument("what", choices=("hints", "quirks"), help="explanation entries, or quirks of the protocol module (--protocol, default mms)")
    p.add_argument("--from", dest="from_id", metavar="ID", help="hints: start from a copy of this built-in entry, to change it on this machine")
    p.add_argument("--file", metavar="NAME", default=localdata.DEFAULT_FILE, help="file name in the layer (default local.yaml)")
    p.add_argument("--system", action="store_true", help="edit the machine layer (/etc/ied-client; needs sudo) instead of your own")


def find_editor() -> list[str]:
    for var in ("VISUAL", "EDITOR"):
        value = os.environ.get(var, "").strip()
        if value:
            return shlex.split(value)
    for name in ("sensible-editor", "editor", "nano", "vi"):
        if shutil.which(name):
            return [name]
    raise UsageError("no text editor found: set EDITOR (e.g. `export EDITOR=nano`)")


def builtin_entry_text(entry_id: str) -> tuple[str, str]:
    """(source label, the entry's YAML text at column 0) of a built-in catalogue entry."""
    from ied_client import yamledit
    from ied_client.explain.catalogue import _builtin_sources

    found: tuple[str, str] | None = None
    for label, text in _builtin_sources():
        for item in yamledit.items(text, "entries"):
            if isinstance(item.value, dict) and str(item.value.get("id", "")).casefold() == entry_id.casefold():
                found = (label, yamledit.item_text(text, item))  # a later file replaces an earlier one
    if found is None:
        raise UsageError(f"no built-in catalogue entry {entry_id!r} (`explain {entry_id}` searches for close ones)")
    return found


@command(
    "local edit",
    "Edit this machine's own explanation entries or quirks in a text editor; checked before it is saved (FLD-1)",
    area=AREA,
    needs=Needs.NOTHING,
    configure=_edit_args,
    examples=("local edit hints", "local edit hints --from add-cause.blocked-by-interlocking", "local edit quirks",
              "sudo ied-client local edit quirks --system"),
)
def edit(ctx: CliContext, args) -> CommandResult:
    from ied_client import yamledit

    if ctx.json or not ctx.ui.interactive:
        raise UsageError("`local edit` opens a text editor, so it needs a terminal")
    if not re.fullmatch(r"[A-Za-z0-9._-]+\.ya?ml", args.file):
        raise UsageError("--file must be a plain file name ending in .yaml")
    kind = args.what
    proto = _protocol_name(ctx) if kind == "quirks" else None
    ly = _target_layer(args.system)
    path = (ly.hints_dir() if kind == "hints" else ly.quirks_dir(proto)) / args.file
    if args.from_id and kind != "hints":
        raise UsageError("--from copies a built-in explanation entry; it only applies to `local edit hints`")
    if path.exists():
        original = path.read_text(encoding="utf-8")
    elif kind == "hints":
        original = localdata.hints_template(path)
    else:
        original = localdata.quirks_template(path, proto, protocols.get(proto).quirks_sources())
    text = original
    if args.from_id:
        if any(isinstance(e, dict) and str(e.get("id", "")).casefold() == args.from_id.casefold()
               for e in localdata.raw_entries(text, "hints")):
            raise UsageError(f"{path} already has an entry {args.from_id!r}: edit it there")
        label, entry_text = builtin_entry_text(args.from_id)
        text = yamledit.append(text, "entries", [f"# copied from {label}: keep the id to replace it on this machine\n" + entry_text])

    editor = find_editor()
    fd, tmp_name = tempfile.mkstemp(prefix=f"{TOOL_NAME}-{kind}-", suffix=".yaml")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        while True:
            try:
                subprocess.run([*editor, str(tmp)], check=False)
            except OSError as exc:
                raise UsageError(f"cannot start the editor {editor[0]!r}: {exc}; set EDITOR") from exc
            new = tmp.read_text(encoding="utf-8")
            try:
                localdata.validate_text(kind, new, str(path))
            except LocalDataError as exc:
                ctx.out.error(str(exc))
                if ctx.ui.confirm("The file does not load. Edit it again? (No discards your changes)", default=True):
                    continue
                return failure(codes.tool("not-confirmed"), f"changes discarded; {path} is unchanged",
                               exit_code=EXIT_REFUSED, show_hint=False)
            break
        if new == original:
            what = f"{path} is unchanged" if path.exists() else f"{path} was not created"
            return CommandResult(data={"file": str(path), "changed": False}, text=lambda out: out.note(f"No changes; {what}."))
        localdata.atomic_write(path, new)
    finally:
        tmp.unlink(missing_ok=True)
    _reload_catalogue()
    n = len(localdata.raw_entries(new, kind))
    data = {"file": str(path), "changed": True, "entries": n, "layer": ly.name}

    def text_out(out: Output) -> None:
        out.ok(f"Saved {path} ({_plural(n, 'entry', 'entries')}).")
        if kind == "hints":
            out.note("Entries from this file show as [local] in hints and in `explain`.")
        out.note(f"`{TOOL_NAME} local export` takes it back to the repository for review.")

    return CommandResult(data=data, text=text_out)


# ---------------------------------------------------------------------------------- add-quirk
def _add_quirk_args(p, oneshot: bool) -> None:
    p.add_argument("--vendor", help="vendor, exactly as the device reports it (default: read from the device)")
    p.add_argument("--model", help="model, exactly as the device reports it (default: read from the device)")
    p.add_argument("--firmware", help="firmware/revision to match (default: the device's; a glob such as 2.3.* is allowed)")
    p.add_argument("--any-firmware", action="store_true", help="match every firmware of this model")
    p.add_argument("--set", dest="fields", action="append", default=[], metavar="FIELD=VALUE",
                   help="a quirks field: " + ", ".join(QUIRK_FIELDS) + " (repeatable)")
    src = p.add_argument_group("evidence (one is required)")
    src.add_argument("--save-incident", action="store_true", help="save an incident file from this session now and cite it")
    src.add_argument("--incident", metavar="FILE", help="cite an existing incident file (copied into the local incidents directory)")
    src.add_argument("--source", metavar="TEXT", help="say in words where and when this was observed")
    p.add_argument("--note", help="with --save-incident: what was being done and what went wrong")
    p.add_argument("--no-connect", action="store_true", help="do not contact the device (give --vendor and --model)")
    p.add_argument("--system", action="store_true", help="write to the machine layer (/etc/ied-client; needs sudo)")


def _device_identity(ctx: CliContext, s: Any, no_connect: bool) -> tuple[dict[str, str | None], str | None]:
    """(vendor/model/firmware the device reports, why it could not be read)."""
    from ied_client.core.identity import identify

    rep = s.identity
    problem = None
    if rep is None and not no_connect:
        try:
            if not s.connected:
                ctx.connect()
            ctx.ensure_model()
            rep = identify(s)
        except Exception as exc:  # the device may be the very thing that misbehaves
            problem = f"could not read the identity from {s.device_name}: {exc}"
    ident: dict[str, str | None] = {"vendor": None, "model": None, "firmware": None}
    if rep is not None:
        for k in ident:
            v = getattr(rep, k).value
            ident[k] = None if v is None else str(v)
    return ident, problem


def _parse_field(item: str) -> tuple[str, Any]:
    name, sep, value = item.partition("=")
    name, value = name.strip(), value.strip()
    if not sep or name not in QUIRK_FIELDS:
        raise UsageError(f"--set {item!r}: use FIELD=VALUE with FIELD one of {', '.join(QUIRK_FIELDS)}")
    if not value:
        raise UsageError(f"--set {name}: empty value")
    if name == "max_associations":
        if not value.isdigit() or int(value) < 1:
            raise UsageError("max_associations must be a positive whole number")
        return name, int(value)
    if name == "slot_release_after_abrupt_disconnect_s":
        try:
            num = float(value)
        except ValueError:
            raise UsageError("slot_release_after_abrupt_disconnect_s must be a number of seconds") from None
        if num < 0:
            raise UsageError("slot_release_after_abrupt_disconnect_s must be >= 0")
        return name, int(num) if num.is_integer() else num
    return name, value


def _copy_incident(src: Path, ly: localdata.Layer) -> Path:
    if not src.is_file():
        raise UsageError(f"no such incident file: {src}")
    content = src.read_text(encoding="utf-8")
    dest = ly.incidents_dir() / src.name
    if dest.exists() and dest.resolve() != src.resolve() and dest.read_text(encoding="utf-8") != content:
        dest = dest.with_name(f"{dest.stem}-{_dt.datetime.now():%H%M%S}{dest.suffix}")
    if not dest.exists() or dest.resolve() != src.resolve():
        localdata.atomic_write(dest, content)
    return dest


@command(
    "local add-quirk",
    "Record a vendor/model/firmware quirk seen on this device, matched on the identity it reports (IDN-9, FLD-1)",
    area=AREA,
    needs=Needs.TARGET,
    configure=_add_quirk_args,
    service="identify",
    examples=("local add-quirk relay-F12",
              "local add-quirk relay-F12 --set max_associations=4 --save-incident --note 'third client refused'",
              "local add-quirk 10.0.0.12 --no-connect --vendor ACME --model 'BCU 5' --any-firmware "
              "--set notes='ResvTms ignored' --source 'FAT 2026-10-02, logbook p.12'"),
)
def add_quirk(ctx: CliContext, args) -> CommandResult:
    import yaml

    from ied_client.quirks import QuirkDB

    s = ctx.session
    if s is None:
        raise UsageError(f"no device: `connect <device>` first, or run `{TOOL_NAME} local add-quirk <device>`")
    ui = ctx.ui
    ly = _target_layer(args.system)
    proto = s.protocol.name
    fields = dict(_parse_field(f) for f in args.fields)
    chosen = [x for x in (args.save_incident, args.incident, args.source) if x]
    if len(chosen) > 1:
        raise UsageError("give one of --save-incident, --incident and --source")

    ident, problem = ({"vendor": None, "model": None, "firmware": None}, None)
    if not (args.vendor and args.model and (args.firmware or args.any_firmware)):
        ident, problem = _device_identity(ctx, s, args.no_connect)
    if problem:
        ui.notify(f"warning: {problem}")
    match: dict[str, str] = {}
    for k in ("vendor", "model"):
        value = getattr(args, k) or ident[k]
        if not value:
            value = ui.ask(f"{k.capitalize()} of {s.device_name}, exactly as the device reports it (see `info`):")
        if not value:
            raise UsageError(f"the {k} is needed: give --{k} (exactly as the device reports it)")
        match[k] = value
    if args.firmware:
        match["firmware"] = args.firmware
    elif not args.any_firmware and ident["firmware"]:
        which = ui.choose(
            f"The device reports firmware {ident['firmware']!r}. Which firmware does the quirk apply to?",
            [("exact", f"only firmware {ident['firmware']}"), ("any", "every firmware of this model")],
            default="exact",
        )
        if which != "any":
            match["firmware"] = ident["firmware"]
    shown = ", ".join(f"{k} {v!r}" for k, v in match.items()) + ("" if "firmware" in match else ", any firmware")
    ui.notify(f"Match: {shown}")

    if not fields:
        if not ui.interactive:
            raise UsageError("say what the quirk is: --set FIELD=VALUE (" + ", ".join(QUIRK_FIELDS) + ")")
        for name, what in QUIRK_FIELDS.items():
            value = ui.ask(f"{name}: {what} (Enter to skip)")
            if value:
                try:
                    fields.update([_parse_field(f"{name}={value}")])
                except UsageError as exc:
                    ui.notify(f"{exc}; skipped")
        if not fields:
            raise UsageError("nothing recorded: every field was skipped")

    source: str | None = None
    if args.save_incident or args.incident or args.source:
        how = "save" if args.save_incident else "file" if args.incident else "words"
    elif ui.interactive:
        how = ui.choose("Where was this observed? A quirk always names its evidence.",
                        [("save", "save an incident file from this session now"),
                         ("file", "an existing incident file"),
                         ("words", "describe it in words")], default="save")
    else:
        raise UsageError("a quirk needs its evidence: --save-incident, --incident FILE or --source TEXT")
    if how == "save":
        from .session import write_incident_file

        dest = ly.incidents_dir() / f"{_dt.datetime.now():%Y-%m-%d-%H%M%S}-{re.sub(r'[^A-Za-z0-9._-]+', '-', s.device_name)}.json"
        note = args.note or (None if not ui.interactive else ui.ask("What was being done, and what went wrong? (Enter to skip)"))
        write_incident_file(ctx, s, dest, note=note)
        source = f"{localdata.INCIDENTS_DIR}/{dest.name}"
    elif how == "file":
        given = args.incident or ui.ask("Incident file:")
        if not given:
            raise UsageError("no incident file given")
        source = f"{localdata.INCIDENTS_DIR}/{_copy_incident(Path(given).expanduser(), ly).name}"
    else:
        source = args.source or ui.ask("Where and when was it observed?")
        if not source:
            raise UsageError("no source given")

    entry: dict[str, Any] = {"match": match, **fields, "source": source}
    preview = yaml.safe_dump([entry], sort_keys=False, allow_unicode=True, default_flow_style=False)
    path = ly.default_file("quirks", proto)
    if ui.interactive and not ctx.opt("yes") and not ui.confirm(f"Add this quirk to {path}?\n{preview}", default=True):
        return failure(codes.tool("not-confirmed"), "nothing recorded", exit_code=EXIT_REFUSED, show_hint=False)
    if not path.exists():
        localdata.atomic_write(path, localdata.quirks_template(path, proto, s.protocol.quirks_sources()))
    info = QuirkDB.append_entry(path, entry)
    s.log.write("note", action="local-add-quirk", path=str(path), quirk=info.to_json())
    data = {"file": str(path), "layer": ly.name, "quirk": info.to_json()}

    def text(out: Output) -> None:
        out.ok(f"Quirk recorded in {path}")
        out.line(preview.rstrip(), style=STYLE_NOTE)
        out.note("`diagnose` uses it from now on (shown as from a local quirks file); "
                 f"`{TOOL_NAME} local export` takes it back to the repository.")

    return CommandResult(data=data, text=text)


# ---------------------------------------------------------------------------------- export / import
def _export_args(p, oneshot: bool) -> None:
    p.add_argument("--out", "-o", metavar="FILE", help="bundle file (default: ied-client-field-<host>-<date>.json here)")
    p.add_argument("--overwrite", action="store_true", help="replace an existing file")


@command(
    "local export",
    "Put this machine's local quirks, explanation entries and incident files in one bundle file for review (FLD-4)",
    area=AREA,
    needs=Needs.NOTHING,
    configure=_export_args,
    examples=("local export", "local export --out /media/usb/field-laptop3.json"),
)
def export(ctx: CliContext, args) -> CommandResult:
    bundle = localdata.build_bundle()
    path = Path(args.out).expanduser() if args.out else Path(localdata.default_bundle_name())
    if path.is_dir():
        path = path / localdata.default_bundle_name()
    if path.exists() and not args.overwrite:
        raise FileExistsError(f"{path} exists (use --overwrite)")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(bundle, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    invalid = [f for f in bundle["files"] if not f["valid"]]
    new = [e for e in bundle["entries"] if e["state"] not in (INGESTED, REJECTED)]
    data = {"file": str(path.resolve()), "files": len(bundle["files"]), "entries": len(bundle["entries"]),
            "new_entries": len(new), "incidents": len(bundle["incidents"]), "invalid_files": [f["path"] for f in invalid]}

    def text(out: Output) -> None:
        out.ok(f"Field bundle written: {data['file']}")
        out.line(f"  {_plural(data['files'], 'file')}, {_plural(data['entries'], 'entry', 'entries')} "
                 f"({data['new_entries']} not yet reviewed), {_plural(data['incidents'], 'incident file')}", style=STYLE_NOTE)
        for f in invalid:
            out.warn(f"{f['layer']}:{f['path']} is invalid; it is in the bundle but cannot be taken as it is")
        if not bundle["files"]:
            out.note("There is no local data yet: the bundle is empty.")
        else:
            out.note("Take it to the repository; `uv run python tools/ingest_field_data.py <file>` reviews it there.")

    return CommandResult(data=data, text=text)


def _import_args(p, oneshot: bool) -> None:
    p.add_argument("bundle", help="a field bundle from `local export` on another machine")
    p.add_argument("--system", action="store_true", help="import into the machine layer (/etc/ied-client; needs sudo)")


@command(
    "local import",
    "Add another machine's field bundle to this machine's local data (laptop to laptop, FLD-4)",
    area=AREA,
    needs=Needs.NOTHING,
    configure=_import_args,
    examples=("local import /media/usb/ied-client-field-laptop3-20261002.json",),
)
def import_cmd(ctx: CliContext, args) -> CommandResult:
    doc = localdata.read_bundle(args.bundle)
    _target_layer(args.system)
    written = localdata.import_bundle(doc, system=args.system)
    _reload_catalogue()
    skipped = [f["path"] for f in doc.get("files") or [] if not f.get("valid", True)]
    data = {"written": [str(p) for p in written], "skipped_invalid": skipped, "from_host": doc.get("host"),
            "created_at": doc.get("created_at")}

    def text(out: Output) -> None:
        out.ok(f"Imported the bundle from {doc.get('host')} ({doc.get('created_at')}): {_plural(len(written), 'file')}")
        for p in written:
            out.line(f"  {_short(p)}", style=STYLE_NOTE)
        for p in skipped:
            out.warn(f"{p} was invalid on {doc.get('host')} and was not imported")
        out.note("This machine's own local.yaml files still win over imported entries with the same id.")

    return CommandResult(data=data, text=text)


# ---------------------------------------------------------------------------------- prune
def _prune_args(p, oneshot: bool) -> None:
    p.add_argument("--system", action="store_true", help="prune the machine layer (/etc/ied-client; needs sudo)")


@command(
    "local prune",
    "Remove local entries that the installed package's ledger shows as reviewed (FLD-6)",
    area=AREA,
    needs=Needs.NOTHING,
    configure=_prune_args,
    examples=("local prune", "sudo ied-client local prune --system"),
)
def prune(ctx: CliContext, args) -> CommandResult:
    plan = localdata.prune_plan(args.system)
    rows = [(f.label, e.ref, STATE_TEXT[e.state] + (f": {e.detail}" if e.detail else "")) for f, es in plan for e in es]
    data: dict[str, Any] = {"entries": [{"file": str(f.path), **e.to_json()} for f, es in plan for e in es], "removed": 0}
    if not plan:
        return CommandResult(data=data, text=lambda out: out.note("Nothing to prune: no local entry has been reviewed into this package."))
    _target_layer(args.system)
    listing = "\n".join(f"  {label}: {ref} ({state})" for label, ref, state in rows)
    if not ctx.opt("yes") and not ctx.ui.confirm(f"Remove these reviewed entries from the local files?\n{listing}"):
        return failure(codes.tool("not-confirmed"), "nothing removed" + ("" if ctx.ui.interactive else " (use --yes)"),
                       exit_code=EXIT_REFUSED, data=data, show_hint=False)
    data["removed"] = localdata.apply_prune(plan)
    _reload_catalogue()

    def text(out: Output) -> None:
        out.ok(f"Removed {_plural(data['removed'], 'entry', 'entries')} now covered by the package's review.")
        for label, ref, state in rows:
            style = STYLE_WARN if state.startswith(STATE_TEXT[REJECTED]) else STYLE_NOTE
            out.line(f"  {label}: {ref} ({state})", style=style)

    return CommandResult(data=data, text=text)
