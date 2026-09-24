"""diagnose, discover (DIA-1 … DIA-6)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from rich.text import Text

from mms_client import codes
from mms_client.core.results import Status

from .. import errors
from ..context import CliContext
from ..registry import Needs, UsageError, command
from ..render import STYLE_ERROR, STYLE_NOTE, STYLE_OK, STYLE_WARN, Output, code_label
from ..result import CommandResult
from ._common import load_reference

STATUS_STYLE = {"pass": STYLE_OK, "fail": STYLE_ERROR, "warn": STYLE_WARN, "info": "", "not-run": STYLE_NOTE}
CERTAINTY_PREFIX = {"likely": "Likely cause: ", "check": "Check: ", "fact": ""}
ASSOCIATION_LAYERS = {"bind", "network", "transport-session", "mms-initiate", "association"}


def status_text(status: str) -> Text:
    return Text(status.upper() if status in ("fail", "warn") else status, style=STATUS_STYLE.get(status, ""))


def render_result_line(ctx: CliContext, out: Output, r: dict[str, Any], service: str | None) -> None:
    """One failed/warning check result: status, title, subject, message, raw code, hint."""
    parts: list[Any] = ["  ", status_text(r["status"]), f"  {r['title']}"]
    if r.get("subject"):
        parts.append(f" [{r['subject']}]")
    out.print(Text.assemble(*parts))
    if r.get("message"):
        out.line(f"      {r['message']}")
    if r.get("reason"):
        out.line(f"      not run: {r['reason']}", style=STYLE_NOTE)
    err = r.get("error")
    if err:
        out.line(f"      code: {code_label(err)}", style=STYLE_NOTE)
        if not ctx.terse and r["status"] in ("fail", "warn"):
            hint = errors.hint_for(codes.parse_key(f"{err['domain']}:{err['name']}"), {"service": service, "mode": ctx.mode.value})
            if hint is not None:
                out.line(f"      Hint: {hint.render()}")


def _diag_args(p, oneshot: bool) -> None:
    p.add_argument("--controls", nargs="?", const="", metavar="OBJ",
                   help="also probe control authority (Select + Cancel, nothing operated), on OBJ or all SBO objects")
    p.add_argument("--probe-timeout", type=float, default=3.0, metavar="S", help="timeout of each probe (default 3 s)")
    p.add_argument("--no-ping", dest="ping", action="store_false", help="skip the ICMP echo")
    p.add_argument("--reference", metavar="FILE", help="SCD/CID/snapshot to compare the model and RCBs with")
    p.add_argument("--ied", metavar="NAME", help="IED name inside the SCD")


@command(
    "diagnose",
    "Layered connectivity diagnosis: network, transport, MMS, identity, model, reports; verdict and next steps",
    area="Diagnosis",
    needs=Needs.TARGET,
    configure=_diag_args,
    service="associate",
    description="Runs the layers in order and stops at the first one that fails (DIA-1), then gives a verdict with "
    "its certainty and the next steps (DIA-2). Use the global --as CLIENT to act as a neighbour (NBR-1).",
    examples=("diagnose relay-F12", "diagnose relay-F12 --as station-manager-1 --controls"),
)
def diagnose_cmd(ctx: CliContext, args) -> CommandResult:
    from mms_client.diagnosis.diagnose import diagnose

    s = ctx.session
    if s is None:
        raise UsageError("no device: `connect <device>` first, or run `mms-client diagnose <device>`")
    notes: list[str] = []
    if s.client is not None:
        # The probes must see the device as a new client would: release our association first.
        notes = [f"Cleanup (released the shell's association before diagnosing): {n}" for n in s.disconnect()]
        notes.append("The shell's association was released for the probes; diagnose reconnects at the end.")
    warnings: list[str] = []
    dev = s.target.device
    ref_path = args.reference or (str(dev.reference.path) if dev is not None and dev.reference is not None else None)
    if ref_path:
        try:
            kind = dev.reference.kind if (dev is not None and dev.reference is not None and not args.reference) else None
            ied = args.ied or (dev.reference.ied if dev is not None and dev.reference is not None else None)
            s.reference = load_reference(ref_path, kind=kind, ied=ied, host=s.target.host)
        except UsageError as e:
            warnings.append(f"reference not used: {e}")
    rep = diagnose(
        s,
        controls=args.controls is not None,
        control_ref=args.controls or None,
        timeout_s=args.probe_timeout,
        icmp=args.ping,
    )
    data = rep.to_json()
    all_passed = rep.stopped_at is None and all(lay.status in (Status.PASS, Status.INFO, Status.NOT_RUN) for lay in rep.layers)

    def text(out: Output) -> None:
        out.table(
            ["layer", "status", "summary"],
            [(lay["layer"], status_text(lay["status"]), lay["summary"]) for lay in data["layers"]],
            title=f"Diagnosis of {rep.device} ({rep.host}:{rep.port})" + (f" as {rep.as_client}" if rep.as_client else ""),
        )
        for lay in data["layers"]:
            service = "associate" if lay["layer"] in ASSOCIATION_LAYERS else None
            for r in lay["results"]:
                if r["status"] in ("fail", "warn"):
                    render_result_line(ctx, out, r, service)
        v = data.get("verdict")
        if v:
            prefix = CERTAINTY_PREFIX.get(v["certainty"], "")
            out.print(Text.assemble(("Verdict: ", "bold"), (prefix + v["text"], STYLE_OK if all_passed else STYLE_WARN)))
            if v.get("key"):
                out.line(f"  code: {v['key']}   (explain {v['key']})", style=STYLE_NOTE)
        if data["next_steps"]:
            out.line("Next steps:", style="bold")
            for i, step in enumerate(data["next_steps"], 1):
                out.line(f"  {i}. {step}")
        if data["findings"]:
            out.line("Findings:", style="bold")
            for f in data["findings"]:
                prefix = CERTAINTY_PREFIX.get(str(f["certainty"]), "")
                out.line(f"  - {prefix}{f['text']}")
                for ev in f.get("evidence") or []:
                    out.line(f"      {ev}", style=STYLE_NOTE)
        if all_passed:
            out.ok("All MMS layers passed.")
        for n in data["notes"]:
            out.note(n)

    result = CommandResult(data=data, text=text, notes=notes, warnings=warnings)
    if rep.stopped_at is not None:
        result.ok = False
        result.show_hint = False  # the verdict and the per-result hints are shown above
        result.message = f"the diagnosis stopped at the {rep.stopped_at} layer"
        key = rep.verdict.key if rep.verdict else None
        if key:
            try:
                result.error = codes.parse_key(key)
            except (KeyError, ValueError):
                result.error = None
        if rep.stopped_at in ASSOCIATION_LAYERS:
            result.exit_code = 3
    return result


# ---------------------------------------------------------------------------------- discover
def _discover_args(p, oneshot: bool) -> None:
    p.add_argument("subnet", help="IPv4 network to scan, e.g. 10.0.0.0/24 (at most a /22 unless --allow-large)")
    p.add_argument("--out", metavar="FILE", help="write the draft inventory to FILE (YAML)")
    p.add_argument("--probe-timeout", type=float, default=1.0, metavar="S", help="TCP/association timeout per host (default 1 s)")
    p.add_argument("--delay", type=float, default=0.2, metavar="S", help="pause between hosts (default 0.2 s)")
    p.add_argument("--no-associate", dest="associate", action="store_false", help="only test TCP 102, do not associate")
    p.add_argument("--allow-large", action="store_true", help="allow networks larger than /22")
    if not oneshot:
        p.add_argument("--port", type=int, help="MMS port (default 102)")
        p.add_argument("--bind", metavar="IP", help="local IP to scan from")


@command(
    "discover",
    "Scan a subnet for MMS servers (sequential, rate-limited), read identity, draft an inventory (DIA-5)",
    area="Diagnosis",
    needs=Needs.NOTHING,
    configure=_discover_args,
    service="associate",
    examples=("discover 10.0.0.0/24 --out experiments/new.yaml",),
)
def discover_cmd(ctx: CliContext, args) -> CommandResult:
    from mms_client.diagnosis.discover import discover, draft_inventory

    port = getattr(args, "port", None) or ctx.options.port or 102
    local_ip = getattr(args, "bind", None) or ctx.options.bind
    out = ctx.out
    status = None

    def progress(p) -> None:
        d = p.device
        if status is not None:
            status.update(f"Scanning {p.ip} ({p.index}/{p.total})")
        if d.tcp_open and not ctx.json:
            ident = d.identity
            label = f"{ident.vendor} {ident.model} {ident.revision}" if ident else (str(d.error) if d.error else "")
            out.line(f"  {d.ip}:{d.port}  TCP open  {label}")

    if out.is_terminal and not ctx.json:
        status = out.console.status(f"Scanning {args.subnet} …")
        status.start()
    try:
        try:
            devices = discover(
                args.subnet,
                port=port,
                timeout_s=args.probe_timeout,
                delay_s=args.delay,
                local_ip=local_ip,
                associate=args.associate,
                progress=progress,
                allow_large=args.allow_large,
            )
        except ValueError as e:
            raise UsageError(str(e)) from e
    finally:
        if status is not None:
            status.stop()
    draft = draft_inventory(devices)
    written = None
    if args.out:
        p = Path(args.out)
        if p.exists():
            raise FileExistsError(f"{p} exists; choose another --out (nothing written)")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(yaml.safe_dump(draft, sort_keys=False, allow_unicode=True), encoding="utf-8")
        written = str(p)
    data = {"subnet": args.subnet, "port": port, "devices": [d.to_json() for d in devices], "draft_inventory": draft, "written": written}

    def text(o: Output) -> None:
        rows = []
        for d in devices:
            ident = d.identity
            rows.append((
                d.ip,
                "open" if d.tcp_open else "closed",
                f"{ident.vendor} / {ident.model} / {ident.revision}" if ident else "",
                ", ".join(d.logical_devices),
                str(d.error) if d.error else "",
                str(d.release) if d.release else "",
            ))
        o.table(["address", "TCP", "vendor / model / revision", "logical devices", "error", "release"], rows,
                title=f"MMS servers in {args.subnet} (port {port})")
        if written:
            o.ok(f"Draft inventory written to {written}. Review names and roles before use.")
        elif devices:
            o.line("Draft inventory (use --out FILE to save it):", style="bold")
            o.line(yaml.safe_dump(draft, sort_keys=False, allow_unicode=True).rstrip())
        o.note("Each association was released before the next host was contacted.")

    return CommandResult(data=data, text=text)
