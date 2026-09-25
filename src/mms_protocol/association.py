"""The client's :class:`~ied_client.protocol.api.Association`, over one MMS association (``IedClient``).

This is where IEC 61850 references become MMS names: data ``LD/LN.DO.DA [FC]`` is read as the
variable ``LN$FC$DO$DA`` of domain ``LD``, datasets are named variable lists, logical nodes are the
domain's variables without ``$``.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import IO, Any

from ied_client.codes import ErrorInfo
from ied_client.core.refs import ObjectRef, RefError
from ied_client.protocol.types import (
    DataSetMember,
    DatasetRef,
    FileEntry,
    RcbValues,
    Report,
    ServerIdentity,
    Value,
    VarSpec,
)

from .adapter import ControlObject, IedClient, VariableListEntry
from .names import dataset_from_item, dataset_item, mms_item, parse_mms


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

    def logical_node_spec(self, ld: str, ln: str) -> VarSpec:
        return self.client.get_variable_spec(ld, ln)

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

    # -- files
    def file_directory(self, directory: str = "") -> list[FileEntry]:
        return self.client.get_file_directory(directory)

    def get_file(self, remote: str, sink: IO[bytes], *, progress: Callable[[int], None] | None = None) -> int:
        return self.client.get_file(remote, sink, progress=progress)

    # -- controls
    def control(self, ref: ObjectRef) -> ControlObject:
        return self.client.control(ref.iec())
