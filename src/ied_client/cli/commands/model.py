"""ls, cd, pwd, tree, describe, dataset (CLI-2, MDL-1 … MDL-4)."""

from __future__ import annotations

from typing import Any

from rich.tree import Tree

from ied_client import codes
from ied_client.core import controls
from ied_client.core.model import DataNode, DeviceModel, NotInModelError, Resolved
from ied_client.core.refs import ObjectRef, RefError, ln_class_of, parse_shell_path
from ied_client.protocol.api import Naming
from ied_client.protocol.types import DatasetRef

from ..context import CliContext
from ..registry import command
from ..render import STYLE_NOTE, STYLE_REF, Output
from ..result import CommandResult, failure
from ._common import summary_of

KIND_NAMES = {"ld": "logical device", "ln": "logical node", "do": "data object", "data": "data", "attr": "attribute"}


def _resolve_path(model: DeviceModel, path: tuple[str, ...]) -> Resolved | None:
    if not path:
        return None
    return model.resolve(ObjectRef(path[0], path[1] if len(path) > 1 else None, tuple(path[2:])))


def _fcs_types(node: DataNode) -> dict[str, str]:
    return {fc: spec.type_name() for fc, spec in node.specs.items()}


def _entry(model: DeviceModel, path: tuple[str, ...], name: str, kind: str) -> dict[str, Any]:
    child = (*path, name)
    e: dict[str, Any] = {"name": name, "kind": kind}
    if kind == "ld":
        ld = model.lds[name]
        e["logical_nodes"] = len(ld.lns)
        e["datasets"] = len(ld.datasets)
        if ld.error:
            e["error"] = ld.error.to_json()
    elif kind == "ln":
        ln = model.lds[path[0]].lns[name]
        e["ln_class"] = ln.ln_class
        e["data_objects"] = len(ln.data)
        e["control_blocks"] = sorted(ln.control_blocks)
        if ln.error:
            e["error"] = ln.error.to_json()
    else:
        r = _resolve_path(model, child)
        if r is not None and r.node is not None:
            e["fcs"] = _fcs_types(r.node)
    return e


def shell_path(path: tuple[str, ...]) -> str:
    return "/" + "/".join(path)


def iec_of(path: tuple[str, ...]) -> str:
    if not path:
        return ""
    return ObjectRef(path[0], path[1] if len(path) > 1 else None, tuple(path[2:])).iec()


def _path_arg(p, oneshot: bool) -> None:
    p.add_argument("path", nargs="?", help="where: /LD/LN/DO, LD/LN.DO, or relative (.., DO.DA)")


# ---------------------------------------------------------------------------------- ls
def _ls_args(p, oneshot: bool) -> None:
    _path_arg(p, oneshot)
    p.add_argument("--refresh", action="store_true", help="read the model from the device again (MDL-4)")


@command(
    "ls",
    "List what is at a location of the model (like a directory)",
    area="Model",
    configure=_ls_args,
    service="browse",
    examples=("ls", "ls /CTRL/CSWI1", "ls Pos", "ls --refresh"),
)
def ls(ctx: CliContext, args) -> CommandResult:
    s = ctx.require_session()
    model = ctx.ensure_model(refresh=args.refresh)
    path = parse_shell_path(args.path, s.cwd, model.ld_names) if args.path else s.cwd
    r = _resolve_path(model, path)
    kind = "root" if r is None else ("do" if r.kind == "data" and len(path) == 3 else r.kind)
    entries = [_entry(model, path, n, k) for n, k in model.children(path)]
    data: dict[str, Any] = {"path": shell_path(path), "reference": iec_of(path) or None, "kind": kind, "children": entries}
    if r is not None and r.kind == "ln" and r.ln is not None:
        data["ln_class"] = r.ln.ln_class
        data["control_blocks"] = [
            {"name": cb.name, "kind": cb.kind, "reference": cb.reference} for cb in r.ln.control_blocks.values()
        ]
        data["datasets"] = [d.name for d in r.ld.datasets if d.ln == r.ln.name]
    if r is not None and r.kind == "data" and r.node is not None and r.node.is_leaf():
        data["attribute"] = {"fcs": _fcs_types(r.node)}
    if args.refresh:
        data["browsed_at"] = model.browsed_at
        data["browse_errors"] = model.errors

    def text(out: Output) -> None:
        out.line(f"{data['path']}" + (f"   ({data['reference']})" if data["reference"] else ""), style=STYLE_REF)
        if args.refresh:
            out.note(f"Model read again: {len(model.lds)} logical device(s), {len(model.errors)} browse error(s).")
        if "attribute" in data:
            for fc, t in data["attribute"]["fcs"].items():
                out.line(f"  attribute [{fc}] {t}")
            return
        if kind == "root":
            out.table(["logical device", "LNs", "datasets"], [(e["name"], e["logical_nodes"], e["datasets"]) for e in entries])
        elif kind == "ld":
            out.table(
                ["logical node", "class", "DOs", "control blocks"],
                [(e["name"], e["ln_class"], e["data_objects"], ", ".join(e["control_blocks"])) for e in entries],
            )
        else:
            out.table(
                ["name", "kind", "FC", "type"],
                [(e["name"], KIND_NAMES.get(e["kind"], e["kind"]), ",".join(e.get("fcs", {})),
                  "  ".join(dict.fromkeys(e.get("fcs", {}).values())) if e["kind"] == "attr" else "")
                 for e in entries],
            )
        if data.get("control_blocks"):
            out.line("Control blocks: " + ", ".join(f"{c['name']} ({c['kind']})" for c in data["control_blocks"]))
        if data.get("datasets"):
            out.line("Datasets: " + ", ".join(data["datasets"]))

    return CommandResult(data=data, text=text)


# ---------------------------------------------------------------------------------- cd / pwd
@command(
    "cd",
    "Change the current location in the model (`cd ..`, `cd /`, `cd -`)",
    area="Model",
    configure=_path_arg,
    service="browse",
)
def cd(ctx: CliContext, args) -> CommandResult:
    s = ctx.require_session()
    model = s.model()
    if args.path == "-":
        path = ctx.prev_cwd
    else:
        path = parse_shell_path(args.path or "/", s.cwd, model.ld_names)
    r = _resolve_path(model, path)
    if r is not None and r.kind == "data" and r.node is not None and r.node.is_leaf():
        return failure(codes.tool("invalid-reference"), f"{iec_of(path)} is an attribute, not something to `cd` into (use `read`)",
                       exit_code=2, show_hint=False)
    if ctx.in_shell:
        ctx.prev_cwd, s.cwd = s.cwd, path
    data = {"path": shell_path(path), "reference": iec_of(path) or None, "kind": "root" if r is None else r.kind}

    def text(out: Output) -> None:
        if not ctx.in_shell:
            out.line(f"{data['path']} exists ({KIND_NAMES.get(data['kind'], data['kind'])}).")

    return CommandResult(data=data, text=text)


@command("pwd", "Show the current location in the model", area="Model", shell_only=True, needs_model=False)
def pwd(ctx: CliContext, args) -> CommandResult:
    s = ctx.require_session()
    data = {"path": shell_path(s.cwd), "reference": iec_of(s.cwd) or None}
    return CommandResult(
        data=data,
        text=lambda out: out.line(data["path"] + (f"   ({data['reference']})" if data["reference"] else "")),
    )


# ---------------------------------------------------------------------------------- tree
def _tree_args(p, oneshot: bool) -> None:
    _path_arg(p, oneshot)
    p.add_argument("--depth", type=int, default=None, help="levels to show (default: 3, or everything below a data object)")
    p.add_argument("--fc", help="only data with this functional constraint")


MAX_TREE_NODES = 5000


@command("tree", "Show the model below a location as a tree", area="Model", configure=_tree_args, service="browse")
def tree(ctx: CliContext, args) -> CommandResult:
    s = ctx.require_session()
    model = s.model()
    path = parse_shell_path(args.path, s.cwd, model.ld_names) if args.path else s.cwd
    _resolve_path(model, path)  # raises if it does not exist
    depth = args.depth if args.depth is not None else (3 if len(path) < 3 else 99)
    fc = args.fc.upper() if args.fc else None
    count = [0]

    def build(p: tuple[str, ...], level: int) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if level >= depth:
            return out
        for name, kind in model.children(p):
            if count[0] >= MAX_TREE_NODES:
                break
            e = _entry(model, p, name, kind)
            if fc and "fcs" in e and fc not in e["fcs"]:
                continue
            count[0] += 1
            e["children"] = build((*p, name), level + 1)
            out.append(e)
        return out

    nodes = build(path, 0)
    data = {"path": shell_path(path), "depth": depth, "fc": fc, "nodes": nodes, "truncated": count[0] >= MAX_TREE_NODES}

    def label(e: dict[str, Any]) -> str:
        extra = ""
        if e["kind"] == "ln":
            extra = f"  ({e['ln_class']})"
        elif e.get("fcs"):
            if e["kind"] == "attr":
                extra = "  " + "  ".join(f"[{f}] {t}" for f, t in e["fcs"].items())
            else:
                extra = "  [" + ",".join(e["fcs"]) + "]"
        return e["name"] + extra

    def text(out: Output) -> None:
        t = Tree(data["path"], guide_style=STYLE_NOTE)

        def add(parent: Tree, items: list[dict[str, Any]]) -> None:
            for e in items:
                add(parent.add(label(e)), e["children"])

        add(t, nodes)
        out.print(t)
        if data["truncated"]:
            out.note(f"(truncated after {MAX_TREE_NODES} nodes: choose a deeper location or a smaller --depth)")

    return CommandResult(data=data, text=text)


# ---------------------------------------------------------------------------------- describe
def writability(fc: str, is_basic: bool) -> dict[str, Any]:
    """Is data under this FC written with `write`, and in which mode (MOD-2/3, RW-6/7/8)?"""
    if fc == "CO":
        return {"writable": False, "how": "operate", "mode": None,
                "note": "control structure: use `operate` / `select` / `cancel`, never `write` (RW-8)"}
    if fc == "SE":
        return {"writable": True, "how": "setgroup", "mode": "standard",
                "note": "setting-group edit value: use `setgroup edit <group> <ref> <value>` (RW-7)"}
    if fc == "SG":
        return {"writable": False, "how": "setgroup", "mode": None,
                "note": "value of the active setting group: read-only here; edit the group with `setgroup edit` (FC=SE)"}
    if fc in codes.NORMALLY_WRITABLE_FCS:
        note = f"normally writable (FC={fc}) with `write` in standard mode"
        if not is_basic:
            note += "; write its components one by one"
        return {"writable": True, "how": "write", "mode": "standard", "note": note}
    return {"writable": False, "how": "write", "mode": "expert",
            "note": f"FC={fc} is not normally writable (the device maintains it); expert mode allows a write attempt "
            "for negative testing, and the device's answer is shown as received (RW-6)"}


def _describe_args(p, oneshot: bool) -> None:
    p.add_argument("ref", help="object reference (LD/LN.DO.DA, relative, or with [FC])")


@command(
    "describe",
    "Type, FC, writability and plain-language meaning of an object (MDL-2)",
    area="Model",
    configure=_describe_args,
    service="browse",
    examples=("describe CTRL/CSWI1.Pos", "describe Pos.ctlModel", "describe XCBR1.Pos.stVal [ST]"),
)
def describe(ctx: CliContext, args) -> CommandResult:
    s = ctx.require_session()
    model = s.model()
    ref = s.parse_ref(args.ref)
    r = model.resolve(ref)
    data: dict[str, Any] = {"reference": ref.iec(), "shell_path": ref.shell_path()}
    explain: dict[str, Any] = {}
    if r.kind == "ld":
        data["kind"] = "logical device"
        data["logical_nodes"] = list(r.ld.lns)
        data["datasets"] = [_in_ld(s.protocol.names, d) for d in r.ld.datasets]
        explain["logical device"] = summary_of("LD")
    elif r.kind == "ln":
        assert r.ln is not None
        data["kind"] = "logical node"
        data["ln_class"] = r.ln.ln_class
        data["data_objects"] = list(r.ln.data)
        data["control_blocks"] = [{"name": cb.name, "kind": cb.kind} for cb in r.ln.control_blocks.values()]
        explain["LN class " + r.ln.ln_class] = summary_of(r.ln.ln_class)
    else:
        node = r.node
        assert node is not None and ref.ln is not None
        is_do = len(ref.path) == 1
        data["kind"] = "data object" if is_do else ("data attribute" if node.is_leaf() else "structured data attribute / sub data object")
        data["ln_class"] = ln_class_of(ref.ln)
        fcs = {}
        for fc, spec in node.specs.items():
            if ref.fc and fc != ref.fc:
                continue
            names = s.protocol.names
            fcs[fc] = {"type": spec.type_name(), names.key: names.data(ref.with_fc(fc)), **writability(fc, spec.is_basic)}
            explain[f"FC {fc}"] = summary_of(f"fc.{fc.lower()}") or summary_of(fc)
        data["fcs"] = fcs
        if not node.is_leaf():
            data["components"] = list(node.children)
        cdc = _cdc_of(s, ref, node, is_do)
        data["cdc"] = cdc
        if cdc and cdc.get("cdc") not in (None, "unknown"):
            explain["CDC " + cdc["cdc"]] = summary_of(cdc["cdc"])
        explain["LN class " + data["ln_class"]] = summary_of(data["ln_class"])
        if not is_do:
            explain["attribute " + ref.path[-1]] = summary_of(ref.path[-1])
    data["explanations"] = {k: v for k, v in explain.items() if v}

    def text(out: Output) -> None:
        rows: list[tuple[str, Any]] = [("reference", data["reference"]), ("shell path", data["shell_path"]), ("kind", data["kind"])]
        if data.get("ln_class"):
            rows.append(("LN class", data["ln_class"]))
        if data.get("cdc"):
            c = data["cdc"]
            rows.append(("CDC", f"{c['cdc']} ({c['source']})"))
        for fc, f in data.get("fcs", {}).items():
            rows.append((f"[{fc}]", f"{f['type']}   {s.protocol.names.label} {f[s.protocol.names.key]}"))
            rows.append(("  writable", f["note"]))
        if data.get("components"):
            rows.append(("components", ", ".join(data["components"])))
        if data.get("data_objects") is not None:
            rows.append(("data objects", ", ".join(data["data_objects"]) or "-"))
        if data.get("control_blocks"):
            rows.append(("control blocks", ", ".join(f"{c['name']} ({c['kind']})" for c in data["control_blocks"])))
        if data.get("logical_nodes") is not None:
            rows.append(("logical nodes", ", ".join(data["logical_nodes"])))
        out.kv(rows)
        if data["explanations"]:
            out.line("")
            for k, e in data["explanations"].items():
                out.parts((f"{k}: ", "bold"), e["summary"])
            out.note("(`explain <term>` gives the full text)")
        elif not ctx.terse:
            out.note("(no plain-language explanation available for these terms in this build)")

    return CommandResult(data=data, text=text)


def _cdc_of(s, ref: ObjectRef, node: DataNode, is_do: bool) -> dict[str, str] | None:
    if not is_do:
        return None
    ref_fn = getattr(s.reference, "cdc_of", None)
    if callable(ref_fn):
        try:
            cdc = ref_fn(ref)
        except Exception:
            cdc = None
        if cdc:
            return {"cdc": cdc, "source": "SCL reference"}
    oper = node.children.get("Oper")
    if oper is not None and "CO" in oper.specs:
        ctl_val = oper.specs["CO"].child("ctlVal")
        st = node.children.get("stVal")
        if ctl_val is not None:
            cdc, source = controls.infer_cdc(s, ref, ctl_val, st.specs.get("ST") if st else None)
            return {"cdc": cdc, "source": source}
    return {"cdc": "unknown", "source": f"not visible over {s.protocol.display_name} without an SCL reference"}


# ---------------------------------------------------------------------------------- dataset
def _dataset_args(p, oneshot: bool) -> None:
    p.add_argument("name", nargs="?", help="dataset: LD/LLN0.Events, LLN0.Events, its protocol name, or just Events (default: list all)")


def _in_ld(names: Naming, ds: DatasetRef) -> str:
    """The dataset's native name without its LD prefix."""
    n = names.dataset(ds)
    prefix = f"{ds.ld}/"
    return n[len(prefix) :] if n.startswith(prefix) else n


def find_dataset(model: DeviceModel, cwd: tuple[str, ...], text: str, names: Naming) -> DatasetRef:
    """A dataset given as ``LD/LN.DS``, ``LN.DS`` or ``DS``, or by the protocol's own name (with or
    without its LD). ``LN.DS`` and an in-LD native name mean the current LD when there is one."""
    t = text.strip()
    native = names.looks_native(t)
    if "/" in t:
        return names.parse_dataset(t) if native else _iec_dataset(t)
    ln, sep, name = t.partition(".")
    if (sep or native) and cwd:
        return names.parse_dataset(t, cwd[0]) if native else DatasetRef(cwd[0], ln, name)

    def wanted(d: DatasetRef) -> bool:
        if native:
            return _in_ld(names, d) == t
        if sep:
            return (d.ln, d.name) == (ln, name)
        return d.name == t

    matches = [d for ldi in model.lds.values() for d in ldi.datasets if wanted(d)]
    if cwd:
        local = [m for m in matches if m.ld == cwd[0]]
        matches = local or matches
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise NotInModelError(f"no dataset {text!r} (use `dataset` to list them)")
    raise RefError(f"{text!r} matches several datasets: " + ", ".join(d.iec() for d in matches))


def _iec_dataset(text: str) -> DatasetRef:
    """``LD/LN.DS`` (or ``LD/DS`` for a dataset outside any LN)."""
    ld, _, rest = text.partition("/")
    ln, sep, name = rest.partition(".")
    return DatasetRef(ld, ln, name) if sep else DatasetRef(ld, "", rest)


@command(
    "dataset",
    "List datasets, or the members of one and whether each resolves (MDL-3)",
    area="Model",
    configure=_dataset_args,
    service="dataset",
)
def dataset(ctx: CliContext, args) -> CommandResult:
    s = ctx.require_session()
    model = s.model()
    names = s.protocol.names
    key = names.key
    if not args.name:
        items = [{"reference": d.iec(), key: names.dataset(d)} for ldi in model.lds.values() for d in ldi.datasets]
        return CommandResult(
            data={"datasets": items},
            text=lambda out: out.table(["dataset", names.label], [(i["reference"], i[key]) for i in items], title="Datasets"),
        )
    ds = find_dataset(model, s.cwd, args.name, names)
    from ied_client.core.datasets import dataset_members

    info = dataset_members(s, ds)
    deletable = info.deletable
    rows = []
    for i, m in enumerate(info.members):
        entry: dict[str, Any] = {"index": i, key: m.member.native, "reference": m.reference, "fc": m.fc, "resolves": m.resolves}
        if m.type_name:
            entry["type"] = m.type_name
        if m.problem:
            entry["reason"] = m.problem
        rows.append(entry)
    ref_text = ds.iec()
    unresolved = [r for r in rows if not r["resolves"]]
    data = {"dataset": ref_text, key: names.dataset(ds), "deletable": deletable, "members": rows, "unresolved": len(unresolved)}

    def text(out: Output) -> None:
        out.table(
            ["#", "member", "FC", "type", "resolves"],
            [(r["index"], r["reference"] or r[key], r["fc"] or "", r.get("type", ""), "yes" if r["resolves"] else f"NO: {r.get('reason', '')}")
             for r in rows],
            title=f"Dataset {ref_text} ({len(rows)} members)",
        )
        if unresolved:
            out.warn(f"{len(unresolved)} member(s) do not resolve in the device's model")

    res = CommandResult(data=data, text=text)
    if unresolved:
        res.ok = False
        res.error = codes.check("dataset-member-unresolved")
        res.message = f"{len(unresolved)} member(s) of {ref_text} do not resolve"
    return res
