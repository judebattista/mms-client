"""The client's :class:`~ied_client.protocol.api.Association`, over one MMS association (``IedClient``).

This is where IEC 61850 references become MMS names: data ``LD/LN.DO.DA [FC]`` is read as the
variable ``LN$FC$DO$DA`` of domain ``LD``, datasets are named variable lists, logical nodes are the
domain's variables without ``$``, and an LN's type is grouped by FC (IEC 61850-8-1), which
:func:`ln_model` turns back into data objects and control blocks.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import IO, Any

from ied_client import codes
from ied_client.codes import ErrorInfo
from ied_client.core.refs import ObjectRef, RefError
from ied_client.protocol.errors import ServiceError
from ied_client.protocol.types import (
    AccessError,
    ControlBlockInfo,
    DataSetMember,
    DatasetRef,
    FileEntry,
    LogicalNodeModel,
    RcbValues,
    Report,
    ServerIdentity,
    Value,
    VarSpec,
)

from .adapter import ControlObject, IedClient, VariableListEntry
from .names import dataset_from_item, dataset_item, mms_item, parse_mms

# FC branches of an LN variable whose components are control blocks, not data objects.
CONTROL_BLOCK_FCS = frozenset({"RP", "BR", "GO", "MS", "US", "LG"})
# Components of the SP branch (of LLN0) that are control blocks.
SP_CONTROL_BLOCKS = frozenset({"SGCB"})


def ln_model(ld: str, ln: str, spec: VarSpec) -> LogicalNodeModel:
    """An LN variable's type (a structure of FC branches, each a structure of data objects or control
    blocks) as the IEC view of the LN."""
    out = LogicalNodeModel()
    for fc_branch in spec.children:
        fc = fc_branch.name or ""
        for comp in fc_branch.children:
            if comp.name is None:
                continue
            if fc in CONTROL_BLOCK_FCS or (fc == "SP" and comp.name in SP_CONTROL_BLOCKS):
                out.add_control_block(ControlBlockInfo(ld, ln, comp.name, fc, comp))
            else:
                out.add_data(fc, comp)
    return out


def dataset_member(entry: VariableListEntry) -> DataSetMember:
    try:
        return DataSetMember(entry.mms_ref(), parse_mms(f"{entry.domain}/{entry.item}"))
    except RefError as e:
        return DataSetMember(entry.mms_ref(), None, str(e))


class MmsAssociation:
    def __init__(self, client: IedClient) -> None:
        self.client = client

    # -- connection
    @property
    def host(self) -> str | None:
        return self.client.host

    @property
    def port(self) -> int | None:
        return self.client.port

    @property
    def local_ip(self) -> str | None:
        return self.client.local_ip

    @property
    def is_connected(self) -> bool:
        return self.client.is_connected

    def close(self, *, graceful: bool = True) -> None:
        self.client.close(graceful=graceful)

    def release(self) -> ErrorInfo:
        return self.client.release()

    def abort(self) -> ErrorInfo:
        return self.client.abort()

    def add_closed_listener(self, cb: Callable[[], None]) -> None:
        self.client.add_closed_listener(cb)

    # -- identity and parameters
    def identify(self) -> ServerIdentity:
        return self.client.identify()

    def association_info(self) -> dict[str, Any]:
        return self.client.connection_params().to_json()

    # -- model
    def logical_devices(self) -> list[str]:
        return self.client.get_domain_names()

    def logical_nodes(self, ld: str) -> list[str]:
        return [n for n in self.client.get_domain_variable_names(ld) if "$" not in n]

    def logical_node(self, ld: str, ln: str) -> LogicalNodeModel:
        return ln_model(ld, ln, self.client.get_variable_spec(ld, ln))

    def datasets(self, ld: str) -> list[DatasetRef]:
        return [dataset_from_item(ld, n) for n in self.client.get_dataset_names(ld)]

    def dataset_directory(self, ds: DatasetRef) -> tuple[list[DataSetMember], bool]:
        entries, deletable = self.client.get_dataset_directory(ds.ld, dataset_item(ds))
        return [dataset_member(e) for e in entries], deletable

    # -- data
    def read(self, ref: ObjectRef, spec: VarSpec | None = None) -> Value:
        return self.client.read(ref.ld, mms_item(ref), spec)

    def read_many(self, refs: Sequence[ObjectRef], specs: Sequence[VarSpec | None] | None = None) -> list[Value]:
        """One MMS Read per logical device (a Read names variables of one domain)."""
        out: list[Value] = [None] * len(refs)
        by_ld: dict[str, list[int]] = {}
        for i, r in enumerate(refs):
            by_ld.setdefault(r.ld, []).append(i)
        for ld, idx in by_ld.items():
            vals = self.client.read_multiple(
                ld, [mms_item(refs[i]) for i in idx], [specs[i] for i in idx] if specs is not None else None
            )
            for i, v in zip(idx, vals, strict=True):
                out[i] = v
        return out

    def write(self, ref: ObjectRef, spec: VarSpec, value: Value) -> None:
        self.client.write(ref.ld, mms_item(ref), spec, value)

    # -- report control blocks
    def get_rcb(self, reference: str) -> RcbValues:
        return self.client.get_rcb(reference)

    def set_rcb(self, reference: str, changes: dict[str, object], *, single_request: bool = True) -> None:
        self.client.set_rcb(reference, changes, single_request=single_request)

    def install_report_handler(
        self,
        reference: str,
        rpt_id: str | None,
        callback: Callable[[Report], None],
        member_specs: Sequence[VarSpec | None] | None = None,
    ) -> None:
        self.client.install_report_handler(reference, rpt_id, callback, member_specs)

    def uninstall_report_handler(self, reference: str) -> None:
        self.client.uninstall_report_handler(reference)

    # -- other control blocks: over MMS they are structured variables (``LLN0$SP$SGCB``, ``LLN0$LG$lcb``)
    def get_control_block(self, cb: ControlBlockInfo) -> dict[str, Value]:
        val = self.client.read(cb.ld, mms_item(ObjectRef(cb.ld, cb.ln, (cb.name,), cb.fc)), cb.spec)
        if isinstance(val, AccessError):
            raise ServiceError("get-control-block", cb.reference, val.error)
        return val if isinstance(val, dict) else {}

    def _write_cb(self, service: str, cb: ControlBlockInfo, attribute: str, value: Value) -> None:
        spec = cb.spec.child(attribute)
        if spec is None:
            raise ServiceError(
                service, cb.reference, codes.tool("sgcb-attribute-missing"), f"the {cb.kind} has no {attribute}"
            )
        self.client.write(cb.ld, mms_item(ObjectRef(cb.ld, cb.ln, (cb.name, attribute), cb.fc)), spec, value)

    # -- setting groups: writes of the SGCB's ActSG, EditSG and CnfEdit (IEC 61850-8-1)
    def select_active_sg(self, cb: ControlBlockInfo, group: int) -> None:
        self._write_cb("select-active-sg", cb, "ActSG", group)

    def select_edit_sg(self, cb: ControlBlockInfo, group: int) -> None:
        self._write_cb("select-edit-sg", cb, "EditSG", group)

    def confirm_edit_sg_values(self, cb: ControlBlockInfo) -> None:
        self._write_cb("confirm-edit-sg-values", cb, "CnfEdit", True)

    # -- files
    def file_directory(self, directory: str = "") -> list[FileEntry]:
        return self.client.get_file_directory(directory)

    def get_file(self, remote: str, sink: IO[bytes], *, progress: Callable[[int], None] | None = None) -> int:
        return self.client.get_file(remote, sink, progress=progress)

    # -- controls
    def control(self, ref: ObjectRef) -> ControlObject:
        return self.client.control(ref.iec())
