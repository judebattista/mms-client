"""The contract between the client and a protocol module (IED-CLIENT-SPEC.md §4.1, PROTO-1 … PROTO-13).

A protocol module is an object implementing :class:`ProtocolModule`, registered under the entry-point
group ``ied_client.protocols`` (see :mod:`.registry`). Everything the client does on a device goes
through the :class:`Association` it opens; everything the client shows or stores in the protocol's own
naming goes through its :class:`Naming`. The core never imports a protocol module.

Object references are IEC 61850 references (:class:`ied_client.core.refs.ObjectRef`, with an FC for
data below the LN). Values and records are the plain types of :mod:`.types`. Failures are raised as
the exceptions of :mod:`.errors`, carrying the stack's own code as an :class:`ErrorInfo` in a domain
the module registered (:func:`ied_client.codes.register_domain`).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import IO, TYPE_CHECKING, Any, Protocol, runtime_checkable

from ied_client.codes import ErrorInfo

from .types import (
    CommandTermination,
    ControlStepResult,
    DataSetMember,
    DatasetRef,
    FileEntry,
    RcbValues,
    Report,
    ServerIdentity,
    Value,
    ValueKind,
    VarSpec,
)

if TYPE_CHECKING:
    from ied_client.core.refs import ObjectRef
    from ied_client.core.session import Session
    from ied_client.diagnosis.diagnose import DiagnosisReport, LayerOutcome
    from ied_client.diagnosis.probe import ProbeResult


class Naming(Protocol):
    """The protocol's own names for model objects (e.g. MMS ``LD/LN$FC$DO$DA``).

    The client never builds or parses these itself. They appear in output next to the IEC 61850
    reference (under the JSON key :attr:`key`), are accepted as input, and are what datasets and
    their members are stored as in snapshots.
    """

    key: str  # JSON key for a native name, e.g. "mms"
    label: str  # column heading, e.g. "MMS name"

    def looks_native(self, text: str) -> bool:
        """Is ``text`` (an operator's reference) written in the protocol's own form?"""

    def data(self, ref: ObjectRef) -> str:
        """Native name of an LD, LN or data reference (data below the LN needs an FC)."""

    def parse_data(self, text: str, default_ld: str | None = None) -> ObjectRef:
        """A native data reference as an ObjectRef (with FC); ``default_ld`` completes a name
        given without its LD. Raises :class:`ied_client.core.refs.RefError`."""

    def dataset(self, ds: DatasetRef) -> str:
        """Native name of a dataset, including its LD."""

    def parse_dataset(self, text: str, default_ld: str | None = None) -> DatasetRef:
        """A native dataset name (with or without its LD) as a DatasetRef. Raises RefError."""

    def control_block(self, ld: str, ln: str, fc: str, name: str) -> str:
        """Native name of a control block."""

    def control_block_to_iec(self, text: str) -> str:
        """A control block reference in native form as ``LD/LN.FC.name``; other text unchanged."""


@runtime_checkable
class ControlObject(Protocol):
    """One controllable data object on an open association (PROTO-6)."""

    reference: str

    @property
    def ctl_model(self) -> int: ...

    @property
    def ctl_val_kind(self) -> ValueKind | None: ...

    def configure(
        self, *, or_ident: str, or_cat: int, test: bool = False, interlock_check: bool = True, synchro_check: bool = True
    ) -> None: ...

    def select(self) -> ControlStepResult: ...

    def select_with_value(self, spec: VarSpec, value: Value) -> ControlStepResult: ...

    def operate(self, spec: VarSpec, value: Value, oper_time_ms: int = 0) -> ControlStepResult: ...

    def cancel(self) -> ControlStepResult: ...

    def wait_termination(self, timeout_s: float, grace_s: float = 0.15) -> CommandTermination | None: ...


@runtime_checkable
class Association(Protocol):
    """One open association with one device (PROTO-1 … PROTO-8)."""

    host: str | None
    port: int | None
    local_ip: str | None

    @property
    def is_connected(self) -> bool: ...

    def close(self, *, graceful: bool = True) -> None:
        """Release (gracefully if possible) and free everything. Safe to call twice."""

    def release(self) -> ErrorInfo:
        """Graceful release; the result code (``code == 0``: confirmed)."""

    def abort(self) -> ErrorInfo: ...

    def add_closed_listener(self, cb: Callable[[], None]) -> None:
        """``cb`` runs (on any thread) when the association closes."""

    # -- identity and parameters (PROTO-3)
    def identify(self) -> ServerIdentity: ...

    def association_info(self) -> dict[str, Any]:
        """Negotiated association parameters, JSON-serialisable (shown by `info`)."""

    # -- model (PROTO-4)
    def logical_devices(self) -> list[str]: ...

    def logical_nodes(self, ld: str) -> list[str]: ...

    def logical_node_spec(self, ld: str, ln: str) -> VarSpec:
        """The LN's type: a structure whose children are the FCs, each a structure of data objects."""

    def datasets(self, ld: str) -> list[DatasetRef]: ...

    def dataset_directory(self, ds: DatasetRef) -> tuple[list[DataSetMember], bool]:
        """Members of a dataset and whether it is deletable."""

    # -- data (PROTO-5); per-variable failures come back as AccessError values
    def read(self, ref: ObjectRef, spec: VarSpec | None = None) -> Value: ...

    def read_many(self, refs: Sequence[ObjectRef], specs: Sequence[VarSpec | None] | None = None) -> list[Value]:
        """Several references in as few requests as the protocol allows, results in request order."""

    def write(self, ref: ObjectRef, spec: VarSpec, value: Value) -> None: ...

    # -- report control blocks (PROTO-7); ``reference`` is ``LD/LN.FC.name``
    def get_rcb(self, reference: str) -> RcbValues: ...

    def set_rcb(self, reference: str, changes: dict[str, object], *, single_request: bool = True) -> None: ...

    def install_report_handler(
        self,
        reference: str,
        rpt_id: str | None,
        callback: Callable[[Report], None],
        member_specs: Sequence[VarSpec | None] | None = None,
    ) -> None:
        """Deliver reports of ``reference`` to ``callback``, which may run on another thread."""

    def uninstall_report_handler(self, reference: str) -> None: ...

    # -- files (PROTO-8)
    def file_directory(self, directory: str = "") -> list[FileEntry]: ...

    def get_file(self, remote: str, sink: IO[bytes], *, progress: Callable[[int], None] | None = None) -> int: ...

    # -- controls (PROTO-6)
    def control(self, ref: ObjectRef) -> ControlObject: ...


class ConnectionDiagnosis(Protocol):
    """The protocol's part of `diagnose` (PROTO-9, PROTO-10, PROTO-12): the layers from the network up
    to an open association. One instance per `diagnose` run (it may keep state between the calls)."""

    def run(self, session: Session, report: DiagnosisReport, *, timeout_s: float, icmp: bool) -> bool:
        """Append the protocol's layers to ``report``. On success leave ``session`` connected and return
        True; on failure call ``report.stop(...)`` with the verdict and return False."""

    def after_identity(self, session: Session, report: DiagnosisReport, layer: LayerOutcome) -> None:
        """Add findings that need the device's identity (e.g. known association limits)."""


class FailedAssociation(Protocol):
    """Why an association attempt failed, as a protocol module classified it (for `discover`)."""

    def to_json(self) -> dict[str, Any]: ...


@runtime_checkable
class ProtocolModule(Protocol):
    """What a protocol module provides (IED-CLIENT-SPEC.md §4.1)."""

    name: str  # the inventory's `protocol:` value and the entry-point name, e.g. "mms"
    display_name: str  # e.g. "MMS"
    default_port: int
    names: Naming
    identify_source: str  # label of the protocol's identify service as an identity source (IDN-1)
    # What the protocol cannot observe, stated in `diagnose` output (e.g. GOOSE/SV for MMS).
    blind_spots: str
    # Why a select without a value carries no originator, if that is so (ORG-5); "" otherwise.
    sbo_normal_select_note: str
    # The `diagnose` layers the connection diagnosis may add, in order; a failure in one is a failure to
    # associate (exit code 3).
    association_layers: tuple[str, ...]
    next_steps: dict[str, list[str]]  # `diagnose` next steps for the protocol's own layers

    def open(
        self,
        host: str,
        port: int,
        *,
        local_ip: str | None = None,
        connect_timeout_ms: int = 5000,
        request_timeout_ms: int = 10000,
    ) -> Association:
        """Associate (PROTO-1). Raises :class:`ConnectError` with the stack's code."""

    def versions(self) -> dict[str, str]:
        """Library versions for the JSON envelope's ``tool`` block (PROTO-11)."""

    def describe_association(self, info: dict[str, Any]) -> list[tuple[str, Any]]:
        """Label/value rows for :meth:`Association.association_info` (shown by `info`)."""

    def connection_diagnosis(self) -> ConnectionDiagnosis: ...

    def classify_failed_association(
        self, host: str, port: int, tcp: ProbeResult, timeout_s: float, local_ip: str | None
    ) -> FailedAssociation | None:
        """Probe why an association with a device whose port is open failed (`discover`)."""

    def catalogue_sources(self) -> list[tuple[str, str]]:
        """(label, YAML text) of the module's explanation catalogue files, in load order (PROTO-13)."""

    def quirks_sources(self) -> list[tuple[str, str]]:
        """(label, YAML text) of the module's quirks files (PROTO-12)."""
