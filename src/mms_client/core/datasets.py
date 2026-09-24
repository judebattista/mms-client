"""Datasets (MDL-3): list the members of a dataset and whether each member resolves."""

from __future__ import annotations

from dataclasses import dataclass, field

from mms_client.adapter import DataSetMember, ServiceError

from .model import dataset_ref_to_mms
from .refs import RefError, parse_mms
from .session import Session


@dataclass(slots=True)
class MemberInfo:
    member: DataSetMember
    reference: str | None  # IEC reference, when the MMS name parses
    fc: str | None
    resolves: bool
    type_name: str | None = None
    problem: str | None = None

    def to_json(self) -> dict:
        return {
            "mms": self.member.mms_ref(),
            "reference": self.reference,
            "fc": self.fc,
            "resolves": self.resolves,
            "type": self.type_name,
            "problem": self.problem,
        }


@dataclass(slots=True)
class DatasetInfo:
    reference: str  # LD/LN$DS
    deletable: bool
    members: list[MemberInfo] = field(default_factory=list)

    @property
    def unresolved(self) -> list[MemberInfo]:
        return [m for m in self.members if not m.resolves]

    def to_json(self) -> dict:
        return {
            "reference": self.reference,
            "deletable": self.deletable,
            "member_count": len(self.members),
            "unresolved": len(self.unresolved),
            "members": [m.to_json() for m in self.members],
        }


def list_datasets(session: Session) -> list[str]:
    model = session.model()
    return [f"{ld}/{ds}" for ld, ldi in model.lds.items() for ds in ldi.datasets]


def dataset_members(session: Session, ref: str) -> DatasetInfo:
    """Members of ``ref`` (``LD/LN.DS`` or ``LD/LN$DS``) resolved against the device model."""
    client = session.require_client()
    model = session.model()
    domain, name = dataset_ref_to_mms(ref)
    try:
        members, deletable = client.get_dataset_directory(domain, name)
    except ServiceError as e:
        session.remember_error(e.error, {"service": "dataset"}, str(e))
        raise
    info = DatasetInfo(f"{domain}/{name}", deletable)
    for m in members:
        try:
            r = parse_mms(f"{m.domain}/{m.item}")
        except RefError as e:
            info.members.append(MemberInfo(m, None, None, False, problem=str(e)))
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
