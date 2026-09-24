"""inventory from-scd, inventory validate (INV-4)."""

from __future__ import annotations

from pathlib import Path

from rich.text import Text

from mms_client.inventory import load_inventory

from ..context import CliContext
from ..registry import Needs, UsageError, command
from ..render import STYLE_ERROR, STYLE_OK, STYLE_WARN, Output
from ..result import CommandResult

STATUS_STYLE = {"pass": STYLE_OK, "fail": STYLE_ERROR, "warn": STYLE_WARN, "info": ""}


def _from_scd_args(p, oneshot: bool) -> None:
    p.add_argument("scd", help="the experiment's SCD file")
    p.add_argument("--out", "-o", metavar="FILE", help="write the draft inventory here (default: print it)")
    p.add_argument("--experiment", help="experiment name (default: the SCD's file name)")
    p.add_argument("--overwrite", action="store_true", help="replace an existing file")


@command(
    "inventory from-scd",
    "Draft an experiment inventory from an SCD: devices, addresses, client relationships and their RCBs (INV-4)",
    area="Inventory",
    needs=Needs.NOTHING,
    configure=_from_scd_args,
    examples=("inventory from-scd scd/rack.scd --out experiments/feeder-trip.yaml",),
)
def from_scd(ctx: CliContext, args) -> CommandResult:
    from mms_client.inventory_ops import from_scd as core_from_scd
    from mms_client.scl import SclError

    out_path = Path(args.out) if args.out else None
    if out_path is not None and out_path.exists() and not args.overwrite:
        raise FileExistsError(f"{out_path} exists (use --overwrite)")
    try:
        inv = core_from_scd(args.scd, experiment=args.experiment, out_path=out_path)
    except (SclError, ValueError) as e:
        raise UsageError(f"cannot read {args.scd}: {e}") from e
    text_yaml = inv.dump()
    saved = str(inv.save(out_path)) if out_path is not None else None
    data = {"inventory": inv.to_yaml(), "warnings": inv.warnings, "written": saved}

    def text(out: Output) -> None:
        if saved:
            out.ok(f"Draft inventory written to {saved}: {len(inv.devices)} device(s), {len(inv.clients)} client relationship(s).")
        else:
            out.line(text_yaml.rstrip())
        for w in inv.warnings:
            out.warn(w)
        out.note("Review the names, roles and source IPs before using it. orCat is never stored in the inventory (§10.3).")

    return CommandResult(data=data, text=text)


def _validate_args(p, oneshot: bool) -> None:
    p.add_argument("file", nargs="?", help="inventory file (default: --inventory / $MMS_CLIENT_INVENTORY)")
    p.add_argument("--no-associate", dest="associate", action="store_false", help="only test TCP, do not associate")
    p.add_argument("--probe-timeout", type=float, default=2.0, metavar="S", help="timeout per device (default 2 s)")


@command(
    "inventory validate",
    "Check an inventory file and test it against the network: TCP, association, identity, logical devices (INV-4)",
    area="Inventory",
    needs=Needs.NOTHING,
    configure=_validate_args,
)
def validate(ctx: CliContext, args) -> CommandResult:
    from mms_client.inventory_ops import validate as core_validate

    path = args.file or ctx.options.inventory
    if not path:
        raise UsageError("which inventory? inventory validate FILE (or --inventory FILE)")
    inv = load_inventory(path)  # schema errors raise InventoryError (exit 2)
    status = None
    if ctx.out.is_terminal and not ctx.json:
        status = ctx.out.console.status(f"Checking {len(inv.devices)} device(s) one at a time …")
        status.start()
    try:
        items = core_validate(inv, timeout_s=args.probe_timeout, associate=args.associate)
    finally:
        if status is not None:
            status.stop()
    data = {"inventory": str(Path(path).resolve()), "experiment": inv.experiment, "devices": len(inv.devices),
            "clients": len(inv.clients), "items": [i.to_json() for i in items]}
    fails = [i for i in items if i.status == "fail"]

    def text(out: Output) -> None:
        out.line(f"Inventory {path}: schema OK, {len(inv.devices)} device(s), {len(inv.clients)} client relationship(s).")
        out.table(["subject", "check", "status", "result"],
                  [(i.subject, i.check, Text(i.status, style=STATUS_STYLE.get(i.status, "")), i.message) for i in items],
                  title="Against the network")
        out.note("Devices are contacted one at a time; each association is released before the next.")

    r = CommandResult(data=data, text=text)
    if fails:
        r.ok = False
        r.show_hint = False
        r.message = f"{len(fails)} check(s) failed"
    return r


