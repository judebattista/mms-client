"""connect, disconnect, info (§6.2 Connection; IDN-1, IDN-3)."""

from __future__ import annotations

import time
from typing import Any

from rich.text import Text

from ied_client.core.identity import Confidence, identify
from ied_client.protocol.api import ProtocolModule

from ..context import CliContext
from ..registry import Needs, UsageError, command
from ..render import STYLE_NOTE, STYLE_OK, STYLE_WARN, Output
from ..result import CommandResult


def _connect_args(p, oneshot: bool) -> None:
    if not oneshot:
        p.add_argument("device", nargs="?", help="device to connect to (default: the current one)")
        p.add_argument("--as", dest="as_client", metavar="CLIENT", help="act as this inventory client (NBR-1)")
        p.add_argument("--bind", metavar="IP", help="local IP to bind the association to (NBR-2)")
        p.add_argument("--port", type=int, metavar="N", help="port (default: the inventory's, or the protocol's)")


def association_info(ctx: CliContext, duration_s: float | None = None) -> dict[str, Any]:
    s = ctx.session
    assert s is not None
    t = s.target
    params = None
    try:
        params = s.association_info()
    except Exception:  # parameters are informative only
        params = None
    return {
        "device": s.device_name,
        "host": t.host,
        "port": t.port,
        "local_ip": t.local_ip,
        "as_client": t.as_client.name if t.as_client else None,
        "connected": s.connected,
        "duration_s": round(duration_s, 4) if duration_s is not None else None,
        "connection_params": params,
        "mode": s.policy.mode.value,
        "safety": s.policy.safety.value,
        "log": str(s.log.path) if s.log.path else None,
    }


def _render_association(out: Output, d: dict[str, Any], *, title: str, protocol: ProtocolModule) -> None:
    rows: list[tuple[str, Any]] = [("device", f"{d['device']} ({d['host']}:{d['port']})")]
    if d.get("as_client"):
        rows.append(("acting as", d["as_client"]))
    if d.get("local_ip"):
        rows.append(("local IP", d["local_ip"]))
    if d.get("duration_s") is not None:
        rows.append(("associate took", f"{d['duration_s'] * 1000:.1f} ms"))
    p = d.get("connection_params")
    if p:
        rows.extend(protocol.describe_association(p))
    rows.append(("mode / safety", f"{d['mode']} / {d['safety']}"))
    if d.get("log"):
        rows.append(("session log", d["log"]))
    out.kv(rows, title=title)


@command(
    "connect",
    "Open the association (in the shell: reconnect, or switch device)",
    area="Connection",
    needs=Needs.TARGET,
    configure=_connect_args,
    service="associate",
    examples=("connect relay-F12", "connect relay-F12 --as station-manager-1"),
)
def connect(ctx: CliContext, args) -> CommandResult:
    notes: list[str] = []
    if ctx.in_shell:
        spec = args.device
        switching = spec is not None and (ctx.session is None or spec not in (ctx.session_spec, ctx.session.device_name))
        rebind = any(getattr(args, k, None) for k in ("as_client", "bind", "port"))
        if ctx.session is None and spec is None:
            raise UsageError("give the device: connect <device>")
        if switching or rebind:
            if ctx.session is not None:
                notes = [f"Cleanup ({ctx.session.device_name}): {n}" for n in ctx.close_session()]
            ctx.make_session(spec or ctx.session_spec or "", as_client=args.as_client, bind=args.bind, port=args.port)
        s = ctx.session
        assert s is not None
        if s.connected:
            data = association_info(ctx)
            return CommandResult(
                data=data,
                notes=notes,
                text=lambda out: out.line(f"Already connected to {s.device_name}. (`disconnect` first to open a new association.)"),
            )
    s = ctx.session
    assert s is not None
    t0 = time.perf_counter()
    ctx.connect()
    dt = time.perf_counter() - t0
    if ctx.in_shell:
        ctx.ensure_model()  # CLI-3: completion uses the model browsed on connect
    data = association_info(ctx, dt)
    model = s.cached_model
    if model is not None:
        data["logical_devices"] = list(model.lds)

    def text(out: Output) -> None:
        out.parts(("Connected ", STYLE_OK), f"to {data['device']} ({data['host']}:{data['port']}) in {dt * 1000:.1f} ms.")
        if data.get("logical_devices"):
            out.line("Logical devices: " + ", ".join(data["logical_devices"]))
        if not ctx.in_shell:
            _render_association(out, data, title="Association", protocol=s.protocol)
            out.note("One-shot mode: the association is released when the command ends.")

    return CommandResult(data=data, text=text, notes=notes)


@command(
    "disconnect",
    "Release the association (undoes the tool's RCB changes first, RPT-7)",
    area="Connection",
    needs=Needs.TARGET,
    service="associate",
)
def disconnect(ctx: CliContext, args) -> CommandResult:
    s = ctx.session
    if not ctx.in_shell:
        return CommandResult(
            data={"connected": False},
            text=lambda out: out.line("Nothing to do: one-shot commands release their association when they finish."),
        )
    if s is None or s.client is None:
        return CommandResult(data={"connected": False}, text=lambda out: out.line("Not connected."))
    notes = s.disconnect()
    return CommandResult(
        data={"connected": False, "cleanup": notes},
        text=lambda out: out.line(f"Released the association with {s.device_name}. `connect` opens it again."),
        notes=[f"Cleanup: {n}" for n in notes],
    )


# ---------------------------------------------------------------------------------- info
def _attr_text(attr: dict[str, Any], *, is_edition: bool = False) -> Text:
    conf = attr.get("confidence")
    value = attr.get("value")
    if conf == Confidence.UNKNOWN.value or value in (None, "unknown"):
        return Text("unknown", style=STYLE_WARN if is_edition else STYLE_NOTE)
    if conf == Confidence.INFERRED.value:
        # IDN-3: an inferred value (above all the edition) must never look confirmed
        return Text.assemble((str(value), STYLE_WARN), (" (inferred, not confirmed)", STYLE_WARN))
    if conf == Confidence.OPERATOR.value:
        return Text.assemble(str(value), (" (operator-supplied)", STYLE_NOTE))
    return Text(str(value))


def _info_args(p, oneshot: bool) -> None:
    p.add_argument("--save-identity", action="store_true",
                   help="write the identity found into the inventory entry of this device, after confirmation (IDN-6)")


def _save_identity(ctx: CliContext, rep) -> str:
    """IDN-6: store vendor/model/firmware (and an operator-supplied edition) in the inventory.

    An inferred edition is not written: stored in the inventory it would read as operator-supplied (IDN-3).
    """
    from ied_client.inventory_ops import write_back_identity

    s = ctx.require_session()
    inv = s.inventory
    if inv is None or inv.path is None or s.target.device is None:
        raise UsageError("--save-identity needs an inventory file that contains this device (--inventory FILE)")
    edition = None
    if rep.edition.confidence is Confidence.OPERATOR:
        edition = getattr(rep.edition.value, "value", rep.edition.value)
    s.policy.confirm_write(
        s.ui,
        f"Save the identity of {s.device_name} into {inv.path}?\n"
        f"  vendor {rep.vendor.value!r}, model {rep.model.value!r}, firmware {rep.firmware.value!r}"
        + (f", edition {edition}" if edition else " (the edition is not saved: it is not operator-supplied)")
        + "\n  Values already in the inventory are kept.",
    )
    write_back_identity(inv, s.target.device.name, rep, edition=edition)
    inv.save()
    s.log.write("note", action="save-identity", inventory=str(inv.path), device=s.target.device.name)
    return str(inv.path)


@command(
    "info",
    "Identity from every source, edition with source and confidence, association parameters",
    area="Connection",
    configure=_info_args,
    service="identify",
)
def info(ctx: CliContext, args) -> CommandResult:
    s = ctx.require_session()
    model = s.model()
    dev = s.target.device
    rep = identify(s)  # keeps an edition the operator gave earlier in the session (IDN-6)
    ident = rep.to_json()
    data = {
        "identity": ident,
        "association": association_info(ctx),
        "model": {
            "logical_devices": list(model.lds),
            "logical_nodes": sum(len(ld.lns) for ld in model.lds.values()),
            "browse_errors": model.errors,
        },
        "session": {
            "orCat": s.or_cat,
            "orIdent": s.or_ident,
            "role": dev.role if dev else None,
            "experiment": s.inventory.experiment if s.inventory else None,
        },
    }

    if args.save_identity:
        data["saved_to"] = _save_identity(ctx, rep)

    def text(out: Output) -> None:
        rows = []
        for name in ("vendor", "model", "firmware", "edition"):
            a = ident[name]
            rows.append((name, _attr_text(a, is_edition=name == "edition"), a.get("confidence") or "", a.get("source") or ""))
        out.table(["attribute", "value", "confidence", "source"], rows, title=f"Identity of {s.device_name} (IDN-1, IDN-3)")
        src_rows = []
        for src in ident["sources"]:
            fields = ", ".join(f"{k}={v!r}" for k, v in src["fields"].items())
            err = ""
            if src.get("error"):
                e = src["error"]
                err = f"{e['domain']}:{e['name']}" + (f" ({e['code']})" if e.get("code") is not None else "")
            src_rows.append((src["source"], src.get("reference") or "", fields or "-", err))
        out.table(["source", "reference", "fields", "error"], src_rows, title="What each source says")
        for d in ident["disagreements"]:
            out.warn("sources disagree: " + d)
        if ident["edition_evidence"]:
            out.line("Edition evidence:", style="bold")
            for ev in ident["edition_evidence"]:
                out.line(f"  - {ev}")
        _render_association(out, data["association"], title="Association", protocol=s.protocol)
        out.line(
            f"Model: {len(data['model']['logical_devices'])} logical device(s), {data['model']['logical_nodes']} logical node(s)"
            + (f", {len(model.errors)} browse error(s)" if model.errors else "")
        )
        if data.get("saved_to"):
            out.ok(f"Identity saved to {data['saved_to']}.")

    return CommandResult(data=data, text=text)
