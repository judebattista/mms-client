"""check, snapshot, diff (§12, VER-1 … VER-14)."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from rich.text import Text

from ied_client.core.results import Category, Status

from ..context import CliContext
from ..registry import Needs, UsageError, command
from ..render import STYLE_NOTE, STYLE_OK, STYLE_WARN, Output
from ..result import CommandResult
from ._common import load_reference
from .diagnosis import render_result_line, status_text


@contextmanager
def progress_status(ctx: CliContext, what: str) -> Iterator[Callable[[str, int, int], None] | None]:
    """A progress callback ``(label, i, n)`` shown as a spinner on a terminal (VER-12)."""
    if ctx.json or not ctx.out.is_terminal:
        yield None
        return
    with ctx.out.console.status(what) as status:

        def progress(label: str, i: int, n: int) -> None:
            status.update(f"{what} {label} ({i}/{n})")

        yield progress


def _ignore(path: str | None) -> Any:
    from ied_client.verify.diff import IgnoreRules

    try:
        return IgnoreRules.load(path)
    except (OSError, ValueError, AttributeError) as e:
        raise UsageError(f"cannot read the ignore file {path}: {e}") from e


# ---------------------------------------------------------------------------------- check
def _check_args(p, oneshot: bool) -> None:
    p.add_argument("--reference", metavar="FILE", help="SCD, CID or snapshot (default: the inventory's reference)")
    p.add_argument("--kind", choices=("scd", "cid", "snapshot"), help="kind of the reference file (guessed from the name)")
    p.add_argument("--ied", metavar="NAME", help="IED name inside the SCD")
    p.add_argument("--device-scl", nargs="?", const="", metavar="NAME",
                   help="use an SCL file stored on the device as a (labelled) reference (VER-8); without a reference, "
                   "an interactive check offers these files by itself, a non-interactive one needs this option")
    p.add_argument("--test-writes", action="store_true", help="write current values back to check that writable attributes accept writes")
    p.add_argument("--write-filter", metavar="GLOB", help="limit --test-writes to references matching GLOB")
    p.add_argument("--try-reports", action="store_true", help="briefly enable the client's RCBs and wait for a report")
    p.add_argument("--ignore", metavar="FILE", help="ignore file (VER-14)")


@command(
    "check",
    "Verify the device: self-consistency always, plus SCD/CID/snapshot if given; Configuration and Communication kept separate",
    area="Verification",
    configure=_check_args,
    service="read",
    examples=("check relay-F12", "check relay-F12 --reference scd/rack.scd --ied F12", "check relay-F12 --device-scl"),
)
def check(ctx: CliContext, args) -> CommandResult:
    from ied_client.verify.checks import CheckOptions, run_checks
    from ied_client.verify.reference import reference_for

    s = ctx.require_session()
    if args.write_filter and not args.test_writes:
        raise UsageError("--write-filter needs --test-writes")
    if args.reference:
        ref = load_reference(args.reference, kind=args.kind, ied=args.ied, host=s.target.host, live_model=s.model())
    else:
        try:
            # VER-8: without a reference, an interactive run looks for SCL files on the device and offers them
            ref = reference_for(s, None, kind=args.kind, ied=args.ied, device_supplied=True if args.device_scl is not None else None,
                                device_file=args.device_scl or None)
        except (ValueError, OSError) as e:
            raise UsageError(f"cannot use the reference: {e}") from e
    s.reference = ref
    with progress_status(ctx, "Checking") as progress:
        opts = CheckOptions(
            test_writes=args.test_writes,
            write_filter=args.write_filter,
            try_reports=args.try_reports,
            ignore=_ignore(args.ignore),
            progress=progress,
        )
        rep = run_checks(s, ref, opts)
    s.last_results["check"] = rep
    data = rep.to_json()
    summary = rep.summary()

    def text(out: Output) -> None:
        out.line(f"Reference: {ref.label if ref is not None else 'none (self-consistency checks only; review the raw values yourself)'}",
                 style="bold")
        rows = []
        for r in data["results"]:
            msg = r["message"] or (f"not run: {r['reason']}" if r.get("reason") else "")
            rows.append((status_text(r["status"]), r["category"], r["title"], r.get("subject") or "", msg))
        out.table(["status", "category", "check", "subject", "result"], rows, title=f"Checks of {s.device_name}")
        problems = [r for r in data["results"] if r["status"] in ("fail", "warn") and r.get("error")]
        if problems:
            out.line("Failures and warnings:", style="bold")
            for r in problems:
                render_result_line(ctx, out, r, None)
        # VER-7: the two categories are reported separately and never merged
        for cat in (Category.CONFIGURATION.value, Category.COMMUNICATION.value):
            st = summary[cat]
            out.print(Text.assemble((f"{cat}: ", "bold"), status_text(st)))
        passes = summary["device_passes"] == "yes"
        out.print(Text.assemble(("Device passes: ", "bold"), ("yes" if passes else "no", STYLE_OK if passes else STYLE_WARN),
                                (" (only if both categories pass)", STYLE_NOTE)))
        if s.protocol.blind_spots:
            out.note(s.protocol.blind_spots)

    r = CommandResult(data=data, text=text)
    fails = [x for x in rep.results if x.status is Status.FAIL]
    if fails:
        r.ok = False
        r.show_hint = False  # per-result hints are shown above
        r.error = fails[0].error
        r.message = (f"{len(fails)} check(s) failed: Configuration {summary[Category.CONFIGURATION.value]}, "
                     f"Communication {summary[Category.COMMUNICATION.value]}")
    return r


# ---------------------------------------------------------------------------------- snapshot
def _snapshot_args(p, oneshot: bool) -> None:
    p.add_argument("--out", "-o", metavar="FILE", help="where to save it (default: <device>-<time>.snapshot.json)")
    p.add_argument("--experiment", help="experiment name recorded in the metadata (default: from the inventory)")
    p.add_argument("--overwrite", action="store_true", help="replace an existing file")


@command(
    "snapshot",
    "Save a read-only three-layer snapshot (structure, configuration, operational state) as JSON (VER-5)",
    area="Verification",
    configure=_snapshot_args,
    service="read",
)
def snapshot(ctx: CliContext, args) -> CommandResult:
    from ied_client.verify.snapshot import capture

    s = ctx.require_session()
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in s.device_name)
    path = Path(args.out or f"{safe}-{time.strftime('%Y%m%d-%H%M%S')}.snapshot.json")
    if path.exists() and not args.overwrite:
        raise FileExistsError(f"{path} exists (use --overwrite)")
    with progress_status(ctx, "Reading") as progress:
        snap = capture(s, experiment=args.experiment, progress=progress)
    saved = snap.save(path)
    md = snap.metadata
    data = {"path": str(saved.resolve()), "metadata": md}

    def text(out: Output) -> None:
        out.kv([
            ("saved", data["path"]),
            ("attributes", md.get("attribute_count")),
            ("unreadable", md.get("unreadable_count")),
            ("edition", f"{md.get('edition', {}).get('value')} ({md.get('edition', {}).get('confidence')}, "
                        f"from {md.get('edition', {}).get('source')})"),
            ("took", f"{md.get('duration_s')} s"),
        ], title=f"Snapshot of {s.device_name}")
        for n in md.get("notes", []):
            out.note(n)

    return CommandResult(data=data, text=text)


# ---------------------------------------------------------------------------------- diff
def _diff_args(p, oneshot: bool) -> None:
    if oneshot:
        p.add_argument("left", help="device (compare live against SNAPSHOT) or a snapshot file")
        p.add_argument("right", help="snapshot file")
    else:
        p.add_argument("snapshots", nargs="+", metavar="SNAPSHOT",
                       help="one snapshot (compared with the live device) or two (compared with each other)")
    p.add_argument("--ignore", metavar="FILE", help="ignore file (VER-14)")
    p.add_argument("--limit", type=int, default=500, help="differences to list in text output (default 500)")


def _load_snapshot(path: str) -> Any:
    from ied_client.verify.snapshot import Snapshot

    try:
        return Snapshot.load(path)
    except (OSError, ValueError) as e:
        raise UsageError(f"cannot read the snapshot {path}: {e}") from e


@command(
    "diff",
    "Compare the live device with a snapshot, or two snapshots (VER-6)",
    area="Verification",
    needs=Needs.NOTHING,
    configure=_diff_args,
    service="read",
    description="diff <device> <snapshot> compares the live device with the snapshot; diff <snapshot> <snapshot> "
    "compares two snapshots and needs no device. Structure changes fail, configuration changes warn, "
    "operational changes are information.",
)
def diff(ctx: CliContext, args) -> CommandResult:
    from ied_client.verify.diff import diff_snapshots
    from ied_client.verify.snapshot import capture

    if ctx.in_shell:
        if len(args.snapshots) > 2:
            raise UsageError("give one snapshot (compared with the live device) or two")
        left_path = args.snapshots[0]
        right_path = args.snapshots[1] if len(args.snapshots) == 2 else None
        live = right_path is None
    else:
        left_path, right_path = args.left, args.right
        live = not Path(args.left).is_file()
    ignore = _ignore(args.ignore)
    if live:
        if ctx.in_shell:
            s = ctx.require_session()
            ref_path = left_path
        else:
            s = ctx.make_session(args.left)
            s.log.write("command", line=" ".join(ctx.argv), command="diff", args={"left": args.left, "right": args.right})
            ctx.connect()
            ref_path = right_path
        assert ref_path is not None
        reference = _load_snapshot(ref_path)
        ctx.ensure_model()
        with progress_status(ctx, "Reading") as progress:
            current = capture(s, progress=progress)
        res = diff_snapshots(reference, current, ignore=ignore, left_label=ref_path, right_label=f"{s.device_name} (live)")
    else:
        assert right_path is not None
        res = diff_snapshots(_load_snapshot(left_path), _load_snapshot(right_path), ignore=ignore,
                             left_label=left_path, right_label=right_path)
    data = res.to_json()

    def text(out: Output) -> None:
        out.line(f"{res.left}  ->  {res.right}", style="bold")
        rows = []
        for e in data["entries"][: args.limit]:
            rows.append((e["layer"], status_text(e["severity"]), e["change"], e["key"],
                         "" if e["old"] is None else str(e["old"]), "" if e["new"] is None else str(e["new"])))
        out.table(["layer", "severity", "change", "key", "before", "after"], rows, title="Differences")
        if len(data["entries"]) > args.limit:
            out.note(f"({len(data['entries']) - args.limit} more; see --json or raise --limit)")
        for layer, c in data["counts"].items():
            out.line(f"{layer}: {c['added']} added, {c['removed']} removed, {c['changed']} changed")
        if res.ignored:
            out.note(f"{res.ignored} difference(s) ignored by the ignore file")
        for n in res.notes:
            out.note(n)
        out.print(Text.assemble(("Result: ", "bold"), status_text(data["status"])))

    r = CommandResult(data=data, text=text)
    if res.status is Status.FAIL:
        r.ok = False
        r.show_hint = False
        r.message = "the structure differs (a changed model, dataset or control block configuration)"
    return r
