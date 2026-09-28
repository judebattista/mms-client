"""The device model as the protocol module reports it (MDL-1, MDL-4).

Browsing asks the association for the logical devices, their logical nodes and datasets, and for
each logical node its contents (:class:`~ied_client.protocol.types.LogicalNodeModel`): the IEC view
LD → LN → DO → … → DA, where every node remembers under which FC(s) it exists, and the LN's control
blocks.

A DO and a structured DA look the same in that view; only an SCL reference can tell them apart.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

from ied_client.codes import ErrorInfo
from ied_client.protocol.api import Association
from ied_client.protocol.errors import ServiceError
from ied_client.protocol.types import ControlBlockInfo, DataNode, DatasetRef, LogicalNodeModel, VarSpec

from .refs import ObjectRef, RefError, ln_class_of

__all__ = [
    "AmbiguousFcError",
    "ControlBlockInfo",
    "DataNode",
    "DeviceModel",
    "LogicalDeviceInfo",
    "LogicalNodeInfo",
    "NotInModelError",
    "Resolved",
    "browse",
]


@dataclass(slots=True)
class LogicalNodeInfo:
    ld: str
    name: str
    data: dict[str, DataNode] = field(default_factory=dict)
    control_blocks: dict[str, ControlBlockInfo] = field(default_factory=dict)
    error: ErrorInfo | None = None

    @classmethod
    def from_model(cls, ld: str, name: str, model: LogicalNodeModel) -> LogicalNodeInfo:
        return cls(ld, name, model.data, model.control_blocks)

    @property
    def ln_class(self) -> str:
        return ln_class_of(self.name)

    @property
    def fcs(self) -> list[str]:
        return LogicalNodeModel(self.data, self.control_blocks).fcs


@dataclass(slots=True)
class LogicalDeviceInfo:
    name: str
    lns: dict[str, LogicalNodeInfo] = field(default_factory=dict)
    datasets: list[DatasetRef] = field(default_factory=list)
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
        if self.ln is not None:
            return self.ln.fcs
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
        """Return ``ref`` with its FC fixed and the type there. Ambiguity is an error unless
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

    # ------------------------------------------------------------------ serialisation (cache)
    def to_json(self) -> dict:
        """The model as JSON: per LN, each data object's type per FC and each control block."""
        return {
            "logical_devices": {
                ld_name: {
                    "datasets": [{"ln": d.ln, "name": d.name} for d in ldi.datasets],
                    "logical_nodes": {ln_name: _ln_to_json(lni) for ln_name, lni in ldi.lns.items()},
                }
                for ld_name, ldi in self.lds.items()
            }
        }

    @classmethod
    def from_json(cls, d: dict) -> DeviceModel:
        m = cls()
        for ld_name, ldd in d.get("logical_devices", {}).items():
            ldi = LogicalDeviceInfo(ld_name, datasets=[DatasetRef(ld_name, d["ln"], d["name"]) for d in ldd.get("datasets", [])])
            for ln_name, lnd in ldd.get("logical_nodes", {}).items():
                ldi.lns[ln_name] = _ln_from_json(ld_name, ln_name, lnd)
            m.lds[ld_name] = ldi
        return m


def _ln_to_json(lni: LogicalNodeInfo) -> dict:
    return {
        "data": {name: {fc: s.to_json() for fc, s in node.specs.items()} for name, node in lni.data.items()},
        "control_blocks": {name: {"fc": cb.fc, "spec": cb.spec.to_json()} for name, cb in lni.control_blocks.items()},
        "error": lni.error.to_json() if lni.error else None,
    }


def _ln_from_json(ld: str, ln: str, d: dict) -> LogicalNodeInfo:
    lnm = LogicalNodeModel()
    for fcs in d.get("data", {}).values():
        for fc, spec in fcs.items():
            lnm.add_data(fc, VarSpec.from_json(spec))
    for name, cb in d.get("control_blocks", {}).items():
        lnm.add_control_block(ControlBlockInfo(ld, ln, name, cb["fc"], VarSpec.from_json(cb["spec"])))
    info = LogicalNodeInfo.from_model(ld, ln, lnm)
    if d.get("error"):
        info.error = ErrorInfo(**d["error"])
    return info


def _leaves(ref: ObjectRef, node: DataNode, fcs) -> Iterator[tuple[ObjectRef, VarSpec]]:
    if node.children:
        for name, child in node.children.items():
            yield from _leaves(ref.child(name), child, fcs)
        return
    for fc, spec in node.specs.items():
        if fcs is None or fc in fcs:
            yield ref.with_fc(fc), spec


def browse(
    client: Association,
    *,
    progress: Callable[[str, int, int], None] | None = None,
    lds: list[str] | None = None,
) -> DeviceModel:
    """Read the device's model: logical devices, then each LD's logical nodes (one request per LN) and
    datasets.

    Failures on individual LNs are recorded in ``model.errors`` and on the LN; browsing goes on.
    """
    t0 = time.time()
    model = DeviceModel(browsed_at=t0)
    ld_names = lds if lds is not None else client.logical_devices()
    for ld_name in ld_names:
        ldi = LogicalDeviceInfo(ld_name)
        model.lds[ld_name] = ldi
        try:
            ln_names = client.logical_nodes(ld_name)
        except ServiceError as e:
            ldi.error = e.error
            model.errors.append({"ld": ld_name, "service": e.service, "error": e.error.to_json()})
            continue
        for i, ln_name in enumerate(ln_names):
            if progress:
                progress(f"{ld_name}/{ln_name}", i + 1, len(ln_names))
            try:
                ldi.lns[ln_name] = LogicalNodeInfo.from_model(ld_name, ln_name, client.logical_node(ld_name, ln_name))
            except ServiceError as e:
                lni = LogicalNodeInfo(ld_name, ln_name, error=e.error)
                ldi.lns[ln_name] = lni
                model.errors.append({"ld": ld_name, "ln": ln_name, "service": e.service, "error": e.error.to_json()})
        try:
            ldi.datasets = client.datasets(ld_name)
        except ServiceError as e:
            model.errors.append({"ld": ld_name, "service": f"{e.service} (datasets)", "error": e.error.to_json()})
    model.duration_s = time.time() - t0
    return model
