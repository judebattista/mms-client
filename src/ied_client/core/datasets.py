"""Datasets (MDL-3): list the members of a dataset and whether each member resolves."""

from __future__ import annotations

from dataclasses import dataclass, field

from ied_client.protocol.errors import ServiceError
from ied_client.protocol.types import DataSetMember, DatasetRef

from .refs import RefError
from .session import Session


@dataclass(slots=True)
class MemberInfo:
    member: DataSetMember
    reference: str | None  # IEC reference, when the native name parses
    fc: str | None
    resolves: bool
    type_name: str | None = None
    problem: str | None = None

    def to_json(self, native_key: str = "native") -> dict:
        return {
            native_key: self.member.native,
            "reference": self.reference,
            "fc": self.fc,
            "resolves": self.resolves,
            "type": self.type_name,
            "problem": self.problem,
        }


@dataclass(slots=True)
class DatasetInfo:
    dataset: DatasetRef
    reference: str  # the protocol's name for the dataset
    deletable: bool
    members: list[MemberInfo] = field(default_factory=list)

    @property
    def unresolved(self) -> list[MemberInfo]:
        return [m for m in self.members if not m.resolves]

    def to_json(self, native_key: str = "native") -> dict:
        return {
            "reference": self.reference,
            "deletable": self.deletable,
            "member_count": len(self.members),
            "unresolved": len(self.unresolved),
            "members": [m.to_json(native_key) for m in self.members],
        }


def list_datasets(session: Session) -> list[DatasetRef]:
    model = session.model()
    return [ds for ldi in model.lds.values() for ds in ldi.datasets]


def dataset_members(session: Session, ds: DatasetRef) -> DatasetInfo:
    """Members of ``ds`` resolved against the device model."""
    client = session.require_client()
    model = session.model()
    try:
        members, deletable = client.dataset_directory(ds)
    except ServiceError as e:
        session.remember_error(e.error, {"service": "dataset"}, str(e))
        raise
    info = DatasetInfo(ds, session.protocol.names.dataset(ds), deletable)
    for m in members:
        r = m.ref
        if r is None:
            info.members.append(MemberInfo(m, None, None, False, problem=m.problem))
            continue
        try:
            if r.path:
                r2, spec = model.resolve_fc(r)
                info.members.append(MemberInfo(m, r2.iec(), r2.fc, True, spec.type_name()))
            else:
                model.resolve(r)
                info.members.append(MemberInfo(m, r.iec(), r.fc, True))
        except RefError as e:
            info.members.append(MemberInfo(m, r.iec(), r.fc, False, problem=str(e)))
    session.log.write("request", service="get-dataset-directory", target=info.reference, ok=True,
                      members=len(info.members), unresolved=len(info.unresolved))
    return info
