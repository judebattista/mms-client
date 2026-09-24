"""files ls, files get (FIL-1 … FIL-4). Read-only: no upload or delete in v1 (FIL-3)."""

from __future__ import annotations

from mms_client.core import files as core_files

from ..context import CliContext
from ..registry import command
from ..render import Output
from ..result import CommandResult


def _ls_args(p, oneshot: bool) -> None:
    p.add_argument("path", nargs="?", default="", help="directory on the device (default: the root)")


def _size(n: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return f"{n} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024  # type: ignore[assignment]
    return str(n)


@command(
    "files ls",
    "List the device's MMS file directory: name, size, last modified (FIL-1)",
    area="Files",
    configure=_ls_args,
    needs_model=False,
    service="file-dir",
)
def files_ls(ctx: CliContext, args) -> CommandResult:
    s = ctx.require_session()
    entries = core_files.list_files(s, args.path or "")
    data = {"directory": args.path or "/", "files": [e.to_json() for e in entries]}

    def text(out: Output) -> None:
        out.table(
            ["name", "size", "last modified (UTC)"],
            [(e.name, _size(e.size), e.last_modified_iso()) for e in entries],
            title=f"Files in {data['directory']} on {s.device_name}",
        )
        out.note("Read-only: this tool never uploads or deletes files on a device (FIL-3).")

    return CommandResult(data=data, text=text)


def _get_args(p, oneshot: bool) -> None:
    p.add_argument("remote", help="file name on the device")
    p.add_argument("--out", "-o", metavar="LOCAL", help="local file or directory (default: same name, here)")
    p.add_argument("--overwrite", action="store_true", help="replace an existing local file")


@command(
    "files get",
    "Download a file from the device as-is (FIL-2, FIL-4)",
    area="Files",
    configure=_get_args,
    needs_model=False,
    service="file-get",
)
def files_get(ctx: CliContext, args) -> CommandResult:
    s = ctx.require_session()
    progress = None
    status = None
    if ctx.out.is_terminal and not ctx.json:
        status = ctx.out.console.status(f"Downloading {args.remote} …")
        status.start()

        def progress(n: int) -> None:
            status.update(f"Downloading {args.remote}: {_size(n)}")

    try:
        d = core_files.get_file(s, args.remote, args.out, overwrite=args.overwrite, progress=progress)
    finally:
        if status is not None:
            status.stop()
    data = d.to_json()
    return CommandResult(
        data=data,
        text=lambda out: out.kv(
            [
                ("downloaded", args.remote),
                ("saved as", data["local"]),
                ("size", f"{data['size']} bytes"),
                ("sha256", data["sha256"]),
                ("took", f"{data['duration_s']} s"),
            ]
        ),
    )
