"""Snapshots (VER-5, VER-9 … VER-13): a read-only capture of a device in three layers.

========================  ===================================================  ============
layer                     contents                                             diff default
========================  ===================================================  ============
structure                 LD/LN/DO/DA tree with types and FCs; datasets and    fail
                          members; RCB, LCB and SGCB configuration attributes
configuration             readable CF, SP, DC, EX values; SG values of the     warn
                          active setting group; identity (IDN-1)
operational               Mod, Beh, Health, Loc, LocSta, LocKey, LPHD.Sim,     info
                          PhyHealth; RCB runtime state (RptEna, Resv, Owner)
========================  ===================================================  ============

A snapshot never writes to the device (VER-9): only the active setting group is captured,
because the others are only readable after switching or selecting a group for editing.
Unreadable attributes are recorded with their error (VER-10). Keys are sorted so that snapshot
files diff cleanly in git (VER-13).
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mms_client import __version__
from mms_client.adapter import (
    AccessError,
    MmsKind,
    ServiceError,
    VarSpec,
    libiec61850_version,
    pyiec61850_version,
    value_to_json,
)
from mms_client.core.identity import identify
from mms_client.core.model import ControlBlockInfo, DeviceModel
from mms_client.core.refs import ObjectRef
from mms_client.core.session import Session, owner_ip

SNAPSHOT_KIND = "mms-client-snapshot"
SNAPSHOT_VERSION = "1.0"
CONFIG_FCS = ("CF", "SP", "DC", "EX", "SG")
OPERATIONAL_DOS = ("Mod", "Beh", "Health", "Loc", "LocSta", "LocKey", "PhyHealth")
RCB_STRUCTURE_ATTRS = ("RptID", "DatSet", "ConfRev", "OptFlds", "BufTm", "TrgOps", "IntgPd")
RCB_OPERATIONAL_ATTRS = ("RptEna", "Resv", "ResvTms", "Owner")
# Attributes of control blocks that change on their own and would make every diff noisy.
VOLATILE_CB_ATTRS = {"SqNum", "EntryID", "TimeOfEntry", "GI", "PurgeBuf", "OldEntrTm", "NewEntrTm", "OldEnt", "NewEnt", "LActTm", "EditSG", "CnfEdit"}


@dataclass
class Snapshot:
    metadata: dict[str, Any] = field(default_factory=dict)
    structure: dict[str, Any] = field(default_factory=dict)
    configuration: dict[str, Any] = field(default_factory=dict)
    operational: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": SNAPSHOT_KIND,
            "schema_version": SNAPSHOT_VERSION,
            "metadata": self.metadata,
            "structure": self.structure,
            "configuration": self.configuration,
            "operational": self.operational,
        }

    def dumps(self) -> str:
        return json.dumps(self.to_json(), indent=1, sort_keys=True, ensure_ascii=False) + "\n"

    def save(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(self.dumps(), encoding="utf-8")
        tmp.replace(p)
        return p

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Snapshot:
        if d.get("kind") != SNAPSHOT_KIND:
            raise ValueError("not an mms-client snapshot")
        major = str(d.get("schema_version", "0")).split(".")[0]
        if major != SNAPSHOT_VERSION.split(".")[0]:
            raise ValueError(f"unsupported snapshot schema_version {d.get('schema_version')}")
        return cls(d.get("metadata", {}), d.get("structure", {}), d.get("configuration", {}), d.get("operational", {}))

    @classmethod
    def load(cls, path: str | Path) -> Snapshot:
        return cls.from_json(json.loads(Path(path).read_text(encoding="utf-8")))

    @property
    def unreadable_count(self) -> int:
        return int(self.metadata.get("unreadable_count", 0))


def _key(ref: ObjectRef) -> str:
    return f"{ref.iec()}[{ref.fc}]"


def _flatten(prefix: ObjectRef, spec: VarSpec, value: Any, out: dict[str, Any]) -> None:
    """Record every basic attribute under ``prefix`` (an AccessError covers all leaves)."""
    if spec.kind is MmsKind.STRUCTURE and spec.children:
        for child in spec.children:
            if child.name is None:
                continue
            sub = value.get(child.name) if isinstance(value, dict) else value
            _flatten(prefix.child(child.name), child, sub, out)
        return
    out[_key(prefix)] = value_to_json(value)


class _Reader:
    """Batched, rate-limited reads with fallback to single reads (VER-12)."""

    def __init__(self, session: Session, batch: int, delay_s: float, progress: Callable[[str, int, int], None] | None):
        self.session = session
        self.client = session.require_client()
        self.batch = batch
        self.delay_s = delay_s
        self.progress = progress
        self.requests = 0

    def read_many(self, ld: str, jobs: list[tuple[ObjectRef, VarSpec]], label: str) -> list[Any]:
        out: list[Any] = []
        for i in range(0, len(jobs), self.batch):
            chunk = jobs[i : i + self.batch]
            if self.progress:
                self.progress(label, min(i + len(chunk), len(jobs)), len(jobs))
            items = [ref.mms_item() for ref, _ in chunk]
            specs = [spec for _, spec in chunk]
            try:
                out.extend(self.client.read_multiple(ld, items, specs))
                self.requests += 1
            except ServiceError:
                for (ref, spec) in chunk:
                    try:
                        out.append(self.client.read(ld, ref.mms_item(), spec))
                    except ServiceError as e:
                        out.append(AccessError(e.error))
                    self.requests += 1
            if self.delay_s:
                time.sleep(self.delay_s)
        return out


def capture(
    session: Session,
    *,
    experiment: str | None = None,
    batch: int = 12,
    delay_s: float = 0.02,
    progress: Callable[[str, int, int], None] | None = None,
) -> Snapshot:
    """Capture the three layers. Reads only (VER-9)."""
    t0 = time.time()
    model = session.model()
    reader = _Reader(session, batch, delay_s, progress)
    snap = Snapshot()
    snap.structure = {"model": {}, "logical_nodes": [], "datasets": {}, "control_blocks": {}}
    _structure_model(model, snap)
    active_groups: dict[str, Any] = {}
    for ld_name, ldi in model.lds.items():
        # -- configuration values: one read per (LN, FC, DO)
        jobs: list[tuple[ObjectRef, VarSpec]] = []
        for ln_name, lni in ldi.lns.items():
            for do_name, node in lni.data.items():
                for fc in CONFIG_FCS:
                    if fc in node.specs:
                        jobs.append((ObjectRef(ld_name, ln_name, (do_name,), fc), node.specs[fc]))
        values = reader.read_many(ld_name, jobs, f"{ld_name} configuration")
        for (ref, spec), val in zip(jobs, values, strict=True):
            _flatten(ref, spec, val, snap.configuration)
        # -- operational ST values
        op_jobs: list[tuple[ObjectRef, VarSpec]] = []
        for ln_name, lni in ldi.lns.items():
            names = list(OPERATIONAL_DOS) + (["Sim"] if lni.ln_class == "LPHD" else [])
            for do_name in names:
                node = lni.data.get(do_name)
                if node is None or "stVal" not in node.children or "ST" not in node.children["stVal"].specs:
                    continue
                op_jobs.append((ObjectRef(ld_name, ln_name, (do_name, "stVal"), "ST"), node.children["stVal"].specs["ST"]))
        for (ref, _spec), val in zip(op_jobs, reader.read_many(ld_name, op_jobs, f"{ld_name} operational"), strict=True):
            snap.operational[_key(ref)] = value_to_json(val)
        # -- datasets and members
        for ds in ldi.datasets:
            key = f"{ld_name}/{ds}"
            try:
                members, _ = reader.client.get_dataset_directory(ld_name, ds)
                snap.structure["datasets"][key] = [m.mms_ref() for m in members]
            except ServiceError as e:
                snap.structure["datasets"][key] = {"error": e.error.to_json()}
    # -- control blocks
    for cb in model.control_blocks({"URCB", "BRCB", "LCB", "SGCB"}):
        _capture_cb(reader, cb, snap, active_groups)
    ident = identify(session, log=False)
    for s in ident.sources:
        if s.source == "mms-identify":
            for k, v in s.fields.items():
                snap.configuration[f"identity:mms-identify.{k}"] = v
    unreadable = sum(
        1
        for layer in (snap.configuration, snap.operational)
        for v in layer.values()
        if isinstance(v, dict) and set(v) == {"error"}
    )
    snap.metadata = {
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t0)),
        "duration_s": round(time.time() - t0, 3),
        "tool": {"name": "mms-client", "version": __version__, "pyiec61850_ng": pyiec61850_version(), "libiec61850": libiec61850_version()},
        "experiment": experiment or (session.inventory.experiment if session.inventory else None),
        "device": {"name": session.device_name, "host": session.target.host, "port": session.target.port},
        "identity": ident.to_json(),
        "edition": ident.edition.to_json(),
        "active_setting_groups": active_groups,
        "attribute_count": len(snap.configuration) + len(snap.operational),
        "unreadable_count": unreadable,
        "read_requests": reader.requests,
        "notes": [
            "read-only capture: only the active setting group is included (VER-9)",
            "ST/MX values other than the operational set are not captured",
        ],
    }
    session.log.write(
        "note",
        action="snapshot",
        attributes=snap.metadata["attribute_count"],
        unreadable=unreadable,
        duration_s=snap.metadata["duration_s"],
    )
    return snap


def model_structure(model: DeviceModel) -> Snapshot:
    """The part of the structure layer that the browsed model alone gives, without reading anything: the
    LD/LN/DO/DA tree with types and FCs (datasets and control blocks need reads and are left empty)."""
    snap = Snapshot()
    snap.structure = {"model": {}, "logical_nodes": [], "datasets": {}, "control_blocks": {}}
    _structure_model(model, snap)
    return snap


def _structure_model(model: DeviceModel, snap: Snapshot) -> None:
    for ld_name, ldi in model.lds.items():
        for ln_name, lni in ldi.lns.items():
            snap.structure["logical_nodes"].append(f"{ld_name}/{ln_name}")
            if lni.error is not None:
                snap.structure["model"][f"{ld_name}/{ln_name}"] = {"error": lni.error.to_json()}
    for ref, spec in model.iter_leaves():
        snap.structure["model"][_key(ref)] = spec.type_name()
    snap.structure["logical_nodes"].sort()


def _capture_cb(reader: _Reader, cb: ControlBlockInfo, snap: Snapshot, active_groups: dict[str, Any]) -> None:
    key = cb.reference
    client = reader.client
    struct: dict[str, Any] = {"type": cb.kind}
    if cb.kind in ("URCB", "BRCB"):
        try:
            v = client.get_rcb(cb.reference)
            reader.requests += 1
        except ServiceError as e:
            struct["error"] = e.error.to_json()
            snap.structure["control_blocks"][key] = struct
            return
        struct.update(
            {"RptID": v.rpt_id, "DatSet": v.dataset, "ConfRev": v.conf_rev, "OptFlds": v.opt_flds, "BufTm": v.buf_tm, "TrgOps": v.trg_ops, "IntgPd": v.intg_pd}
        )
        snap.operational[f"{key}.RptEna"] = v.rpt_ena
        if v.buffered:
            snap.operational[f"{key}.ResvTms"] = v.resv_tms
        else:
            snap.operational[f"{key}.Resv"] = v.resv
        snap.operational[f"{key}.Owner"] = owner_ip(v.owner)
    else:
        fc = cb.fc
        try:
            val = client.read(cb.ld, f"{cb.ln}${fc}${cb.name}", cb.spec)
            reader.requests += 1
        except ServiceError as e:
            val = AccessError(e.error)
        if isinstance(val, AccessError):
            struct["error"] = value_to_json(val)
        elif isinstance(val, dict):
            for k, x in val.items():
                if k not in VOLATILE_CB_ATTRS:
                    struct[k] = value_to_json(x)
            if cb.kind == "SGCB":
                active_groups[cb.ld] = val.get("ActSG")
    snap.structure["control_blocks"][key] = struct
