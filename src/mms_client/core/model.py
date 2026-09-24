"""The device model as seen over MMS (MDL-1, MDL-4).

Browsing uses GetNameList (logical devices, logical nodes, datasets) and one
GetVariableAccessAttributes per logical node, which returns the whole LN structure grouped by
functional constraint. The model then merges the FC branches into an IEC view:
LD → LN → DO → … → DA, where every node remembers under which FC(s) it exists.

Over MMS a DO and a structured DA look the same; only an SCL reference can tell them apart.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

from mms_client.adapter import IedClient, MmsKind, ServiceError, VarSpec
from mms_client.codes import ErrorInfo

from .refs import ObjectRef, RefError, ln_class_of

# FC branches whose children are control blocks, not data objects.
CONTROL_BLOCK_FCS = frozenset({"RP", "BR", "GO", "MS", "US", "LG"})
# Components of the SP branch of LLN0 that are control blocks.
SP_CONTROL_BLOCKS = frozenset({"SGCB"})


@dataclass(slots=True)
class DataNode:
    """A DO/SDO/DA/BDA, merged over FCs. ``specs`` maps FC → the MMS type at this node."""

    name: str
    specs: dict[str, VarSpec] = field(default_factory=dict)
    children: dict[str, DataNode] = field(default_factory=dict)

    @property
    def fcs(self) -> list[str]:
        return list(self.specs)

    def is_leaf(self) -> bool:
        return not self.children

    def merge(self, fc: str, spec: VarSpec) -> None:
        self.specs[fc] = spec
        if spec.kind is MmsKind.STRUCTURE:
            for c in spec.children:
                if c.name is None:
                    continue
                node = self.children.get(c.name)
                if node is None:
                    node = self.children[c.name] = DataNode(c.name)
                node.merge(fc, c)

    def to_json(self) -> dict:
        return {"name": self.name, "fcs": {fc: s.type_name() for fc, s in self.specs.items()}}


@dataclass(slots=True)
class ControlBlockInfo:
    ld: str
    ln: str
    name: str
    fc: str  # RP, BR, GO, MS, US, LG, SP (SGCB)
    spec: VarSpec

    @property
    def reference(self) -> str:
        """IEC-style reference accepted by libiec61850, e.g. ``LD/LLN0.BR.brcb01``."""
        return f"{self.ld}/{self.ln}.{self.fc}.{self.name}"

    @property
    def mms_reference(self) -> str:
        return f"{self.ld}/{self.ln}${self.fc}${self.name}"

    @property
    def kind(self) -> str:
        return {"RP": "URCB", "BR": "BRCB", "GO": "GoCB", "MS": "MSVCB", "US": "USVCB", "LG": "LCB", "SP": "SGCB"}[
            self.fc
        ]


@dataclass(slots=True)
class LogicalNodeInfo:
    ld: str
    name: str
    spec: VarSpec | None = None
    data: dict[str, DataNode] = field(default_factory=dict)
    control_blocks: dict[str, ControlBlockInfo] = field(default_factory=dict)
    error: ErrorInfo | None = None

    @property
    def ln_class(self) -> str:
        return ln_class_of(self.name)


@dataclass(slots=True)
class LogicalDeviceInfo:
    name: str
    lns: dict[str, LogicalNodeInfo] = field(default_factory=dict)
    datasets: list[str] = field(default_factory=list)  # MMS names, e.g. "LLN0$Events"
    error: ErrorInfo | None = None


class AmbiguousFcError(RefError):
    def __init__(self, ref: ObjectRef, fcs: list[str]) -> None:
        self.ref = ref
        self.fcs = fcs
        super().__init__(f"{ref.iec()} exists under several functional constraints {fcs}; add [FC] (e.g. [{fcs[0]}])")


class NotInModelError(RefError):
    pass


@dataclass(slots=True)
class Resolved:
    """A reference located in the model."""

    ref: ObjectRef
    kind: str  # "ld" | "ln" | "data"
    ld: LogicalDeviceInfo
    ln: LogicalNodeInfo | None = None
    node: DataNode | None = None

    @property
    def fcs(self) -> list[str]:
        if self.node is not None:
            return self.node.fcs
        if self.ln is not None and self.ln.spec is not None:
            return [c.name for c in self.ln.spec.children if c.name]
        return []


@dataclass(slots=True)
class DeviceModel:
    lds: dict[str, LogicalDeviceInfo] = field(default_factory=dict)
    browsed_at: float = 0.0
    duration_s: float = 0.0
    errors: list[dict[str, Any]] = field(default_factory=list)

    # ------------------------------------------------------------------ lookup
    @property
    def ld_names(self) -> frozenset[str]:
        return frozenset(self.lds)

    def resolve(self, ref: ObjectRef) -> Resolved:
        ld = self.lds.get(ref.ld)
        if ld is None:
            raise NotInModelError(f"logical device {ref.ld!r} not found (have: {', '.join(self.lds) or 'none'})")
        if ref.ln is None:
            return Resolved(ref, "ld", ld)
        ln = ld.lns.get(ref.ln)
        if ln is None:
            raise NotInModelError(f"logical node {ref.ld}/{ref.ln} not found")
        if not ref.path:
            return Resolved(ref, "ln", ld, ln)
        node: DataNode | None = None
        children = ln.data
        for i, comp in enumerate(ref.path):
            node = children.get(comp)
            if node is None:
                where = ".".join(ref.path[:i]) or ref.ln
                raise NotInModelError(f"{ref.iec()}: {comp!r} not found under {where}")
            children = node.children
        if ref.fc and node is not None and ref.fc not in node.specs:
            raise NotInModelError(f"{ref.iec()} has no [{ref.fc}] (available: {', '.join(node.fcs)})")
        return Resolved(ref, "data", ld, ln, node)

    def resolve_fc(self, ref: ObjectRef, prefer: tuple[str, ...] = ()) -> tuple[ObjectRef, VarSpec]:
        """Return ``ref`` with its FC fixed and the MMS type there. Ambiguity is an error unless
        ``prefer`` names an FC present at the node."""
        r = self.resolve(ref)
        if r.kind != "data" or r.node is None:
            raise RefError(f"{ref.iec()} is a {'logical device' if r.kind == 'ld' else 'logical node'}, not data")
        if ref.fc:
            return ref, r.node.specs[ref.fc]
        fcs = r.node.fcs
        if len(fcs) == 1:
            return ref.with_fc(fcs[0]), r.node.specs[fcs[0]]
        for p in prefer:
            if p in fcs:
                return ref.with_fc(p), r.node.specs[p]
        raise AmbiguousFcError(ref, fcs)

    def spec(self, ref: ObjectRef) -> VarSpec:
        return self.resolve_fc(ref)[1]

    def has(self, ref: ObjectRef) -> bool:
        try:
            self.resolve(ref)
            return True
        except RefError:
            return False

    def children(self, path: tuple[str, ...]) -> list[tuple[str, str]]:
        """(name, kind) of the children at a shell path, for ``ls`` and completion."""
        if not path:
            return [(n, "ld") for n in self.lds]
        r = self.resolve(ObjectRef(path[0], path[1] if len(path) > 1 else None, tuple(path[2:])))
        if r.kind == "ld":
            return [(n, "ln") for n in r.ld.lns]
        if r.kind == "ln":
            assert r.ln is not None
            return [(n, "do") for n in r.ln.data]
        assert r.node is not None
        return [(n, "data" if c.children else "attr") for n, c in r.node.children.items()]

    def iter_leaves(
        self, ld: str | None = None, ln: str | None = None, fcs: frozenset[str] | set[str] | None = None
    ) -> Iterator[tuple[ObjectRef, VarSpec]]:
        """All basic attributes (with their FC), in model order."""
        for ld_name, ldi in self.lds.items():
            if ld is not None and ld_name != ld:
                continue
            for ln_name, lni in ldi.lns.items():
                if ln is not None and ln_name != ln:
                    continue
                for do_name, node in lni.data.items():
                    yield from _leaves(ObjectRef(ld_name, ln_name, (do_name,)), node, fcs)

    def iter_data_objects(self, ld: str | None = None) -> Iterator[tuple[ObjectRef, DataNode]]:
        for ld_name, ldi in self.lds.items():
            if ld is not None and ld_name != ld:
                continue
            for ln_name, lni in ldi.lns.items():
                for do_name, node in lni.data.items():
                    yield ObjectRef(ld_name, ln_name, (do_name,)), node

    def control_blocks(self, kinds: frozenset[str] | set[str] | None = None) -> list[ControlBlockInfo]:
        out = []
        for ldi in self.lds.values():
            for lni in ldi.lns.values():
                for cb in lni.control_blocks.values():
                    if kinds is None or cb.kind in kinds:
                        out.append(cb)
        return out

    def rcbs(self) -> list[ControlBlockInfo]:
        return self.control_blocks({"URCB", "BRCB"})

    def controllable_objects(self) -> list[ObjectRef]:
        """DOs that have an ``Oper`` structure under CO."""
        return [ref for ref, node in self.iter_data_objects() if "Oper" in node.children and "CO" in node.children["Oper"].specs]

    def count_attributes(self) -> int:
        return sum(1 for _ in self.iter_leaves())

    # ------------------------------------------------------------------ serialisation (snapshots, cache)
    def to_json(self) -> dict:
        return {
            "logical_devices": {
                ld_name: {
                    "datasets": ldi.datasets,
                    "logical_nodes": {
                        ln_name: {"spec": lni.spec.to_json() if lni.spec else None} for ln_name, lni in ldi.lns.items()
                    },
                }
                for ld_name, ldi in self.lds.items()
            }
        }

    @classmethod
    def from_json(cls, d: dict) -> DeviceModel:
        m = cls()
        for ld_name, ldd in d.get("logical_devices", {}).items():
            ldi = LogicalDeviceInfo(ld_name, datasets=list(ldd.get("datasets", [])))
            for ln_name, lnd in ldd.get("logical_nodes", {}).items():
                spec = VarSpec.from_json(lnd["spec"]) if lnd.get("spec") else None
                ldi.lns[ln_name] = build_ln(ld_name, ln_name, spec)
            m.lds[ld_name] = ldi
        return m


def _leaves(ref: ObjectRef, node: DataNode, fcs) -> Iterator[tuple[ObjectRef, VarSpec]]:
    if node.children:
        for name, child in node.children.items():
            yield from _leaves(ref.child(name), child, fcs)
        return
    for fc, spec in node.specs.items():
        if fcs is None or fc in fcs:
            yield ref.with_fc(fc), spec


def build_ln(ld: str, ln: str, spec: VarSpec | None) -> LogicalNodeInfo:
    info = LogicalNodeInfo(ld, ln, spec)
    if spec is None:
        return info
    for fc_branch in spec.children:
        fc = fc_branch.name or ""
        if fc in CONTROL_BLOCK_FCS:
            for cb in fc_branch.children:
                if cb.name:
                    info.control_blocks[cb.name] = ControlBlockInfo(ld, ln, cb.name, fc, cb)
            continue
        for do_spec in fc_branch.children:
            if do_spec.name is None:
                continue
            if fc == "SP" and do_spec.name in SP_CONTROL_BLOCKS:
                info.control_blocks[do_spec.name] = ControlBlockInfo(ld, ln, do_spec.name, "SP", do_spec)
                continue
            node = info.data.get(do_spec.name)
            if node is None:
                node = info.data[do_spec.name] = DataNode(do_spec.name)
            node.merge(fc, do_spec)
    return info


def browse(
    client: IedClient,
    *,
    progress: Callable[[str, int, int], None] | None = None,
    lds: list[str] | None = None,
) -> DeviceModel:
    """Read the device's model (GetNameList + GetVariableAccessAttributes per LN).

    Failures on individual LNs are recorded in ``model.errors`` and on the LN; browsing goes on.
    """
    t0 = time.time()
    model = DeviceModel(browsed_at=t0)
    ld_names = lds if lds is not None else client.get_domain_names()
    for ld_name in ld_names:
        ldi = LogicalDeviceInfo(ld_name)
        model.lds[ld_name] = ldi
        try:
            names = client.get_domain_variable_names(ld_name)
        except ServiceError as e:
            ldi.error = e.error
            model.errors.append({"ld": ld_name, "service": e.service, "error": e.error.to_json()})
            continue
        ln_names = [n for n in names if "$" not in n]
        for i, ln_name in enumerate(ln_names):
            if progress:
                progress(f"{ld_name}/{ln_name}", i + 1, len(ln_names))
            try:
                spec = client.get_variable_spec(ld_name, ln_name)
                ldi.lns[ln_name] = build_ln(ld_name, ln_name, spec)
            except ServiceError as e:
                lni = LogicalNodeInfo(ld_name, ln_name, error=e.error)
                ldi.lns[ln_name] = lni
                model.errors.append({"ld": ld_name, "ln": ln_name, "service": e.service, "error": e.error.to_json()})
        try:
            ldi.datasets = client.get_dataset_names(ld_name)
        except ServiceError as e:
            model.errors.append({"ld": ld_name, "service": "get-name-list (datasets)", "error": e.error.to_json()})
    model.duration_s = time.time() - t0
    return model


def dataset_ref_to_mms(ref: str) -> tuple[str, str]:
    """``LD/LLN0$Events`` or ``LD/LLN0.Events`` → (domain, ``LLN0$Events``)."""
    ld, sep, rest = ref.partition("/")
    if not sep:
        raise RefError(f"{ref!r}: expected LD/LN.dataset")
    return ld, rest.replace(".", "$")
