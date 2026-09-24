"""The adapter's client: one MMS association with one IED.

Design rules (see docs/architecture.md and docs/spikes.md):

* Every blocking libiec61850 call goes through ctypes, which releases the GIL, so report and
  CommandTermination callbacks can always run (the deadlock found in spike RSK-1 cannot occur).
* Callbacks run on libiec61850's connection thread. They only copy data into Python objects and
  hand them to the registered Python callable; they never issue requests (a request from the
  connection thread would wait for itself).
* The adapter owns all native memory. Nothing native is returned to callers.
"""

from __future__ import annotations

import atexit
import ctypes as C
import threading
import time
import weakref
from collections.abc import Callable, Sequence
from typing import IO

from mms_client import codes
from mms_client.codes import ErrorInfo

from . import _native
from ._native import c_str, iter_linked_list, lib, py_str, string_list
from .codec import decode, encode, spec_from_native
from .errors import ConnectError, NotConnectedError, ServiceError
from .types import (
    RCB_ELEMENTS,
    AccessError,
    CommandTermination,
    ConnectionParams,
    ControlStepResult,
    DataSetMember,
    FileEntry,
    MmsKind,
    RcbValues,
    Report,
    ReportEntry,
    ServerIdentity,
    Value,
    VarSpec,
)

STATE_NAMES = {0: "closed", 1: "connecting", 2: "connected", 3: "closing"}

_live_clients: weakref.WeakSet[IedClient] = weakref.WeakSet()


@atexit.register
def _close_all() -> None:  # pragma: no cover - interpreter shutdown
    # Callbacks into a finalising interpreter crash the process; stop all connection threads first.
    for c in list(_live_clients):
        try:
            c.close()
        except Exception:
            pass


def _check_mms(service: str, target: str | None, err: C.c_int) -> None:
    if err.value != 0:
        raise ServiceError(service, target, codes.mms_error(err.value))


def _check_ied(service: str, target: str | None, err: C.c_int) -> None:
    if err.value != 0:
        raise ServiceError(service, target, codes.ied_error(err.value))


class IedClient:
    """One association with one server. Not reusable after :meth:`close`."""

    def __init__(self, *, connect_timeout_ms: int = 5000, request_timeout_ms: int = 10000) -> None:
        self.connect_timeout_ms = connect_timeout_ms
        self.request_timeout_ms = request_timeout_ms
        self.host: str | None = None
        self.port: int | None = None
        self.local_ip: str | None = None
        self._con: int | None = None
        self._mms: int | None = None
        self._lock = threading.RLock()
        self._report_cbs: dict[str, tuple[object, Callable[[Report], None]]] = {}
        self._controls: dict[str, ControlObject] = {}
        self._closed_event = threading.Event()
        self._closed_listeners: list[Callable[[], None]] = []
        self._closed_cfunc = _native.ConnectionClosedCallback(self._on_closed)
        self.connected_at: float | None = None
        self.lost_at: float | None = None
        self._released = False  # a successful release already ended the association
        _live_clients.add(self)

    # ------------------------------------------------------------------ connection
    def connect(self, host: str, port: int = 102, *, local_ip: str | None = None, local_port: int = 0) -> None:
        """Associate with the server. Raises :class:`ConnectError` with the libiec61850 error code."""
        L = lib()
        with self._lock:
            if self._con is not None:
                raise ServiceError("associate", host, codes.ied_error(2))
            con = L.IedConnection_create()
            if not con:
                raise ConnectError("associate", host, codes.ied_error(99), "could not allocate a connection")
            L.IedConnection_setConnectTimeout(con, self.connect_timeout_ms)
            L.IedConnection_setRequestTimeout(con, self.request_timeout_ms)
            if local_ip:
                L.IedConnection_setLocalAddress(con, c_str(local_ip), local_port)
            L.IedConnection_installConnectionClosedHandler(con, self._closed_cfunc, None)
            err = C.c_int(0)
            self._closed_event.clear()
            L.IedConnection_connect(con, C.byref(err), c_str(host), port)
            if err.value != 0:
                L.IedConnection_destroy(con)
                raise ConnectError("associate", f"{host}:{port}", codes.ied_error(err.value))
            self._con = con
            self._mms = L.IedConnection_getMmsConnection(con)
            self.host, self.port, self.local_ip = host, port, local_ip
            self.connected_at = time.time()
            self.lost_at = None
            self._released = False

    def _on_closed(self, _param: int | None, _con: int | None) -> None:
        # Connection thread. Only record the fact and notify listeners.
        self.lost_at = time.time()
        self._closed_event.set()
        for cb in list(self._closed_listeners):
            try:
                cb()
            except Exception:
                pass

    def add_closed_listener(self, cb: Callable[[], None]) -> None:
        """Register a callable run (on the connection thread) when the association closes."""
        self._closed_listeners.append(cb)

    @property
    def state(self) -> str:
        con = self._con
        if con is None:
            return "closed"
        return STATE_NAMES.get(lib().IedConnection_getState(con), "unknown")

    @property
    def is_connected(self) -> bool:
        return self._con is not None and not self._released and self.state == "connected"

    def _require(self) -> tuple[int, int]:
        con, mms = self._con, self._mms
        if con is None or mms is None:
            raise NotConnectedError("not connected")
        return con, mms

    def release(self) -> ErrorInfo:
        """Graceful release (MMS conclude). Returns the raw result code."""
        L = lib()
        with self._lock:
            if self._con is None:
                return codes.ied_error(1)
            err = C.c_int(0)
            L.IedConnection_release(self._con, C.byref(err))
            if err.value == 0:
                # libiec61850 may still report "connected" although conclude and FINISH/DISCONNECT
                # have completed; remember it so close() does not release a second time.
                self._released = True
            return codes.ied_error(err.value)

    def abort(self) -> ErrorInfo:
        L = lib()
        with self._lock:
            if self._con is None:
                return codes.ied_error(1)
            err = C.c_int(0)
            L.IedConnection_abort(self._con, C.byref(err))
            return codes.ied_error(err.value)

    def close(self, *, graceful: bool = True) -> None:
        """Release the association (gracefully if possible) and free all native resources."""
        L = lib()
        with self._lock:
            con = self._con
            if con is None:
                return
            for ref in list(self._report_cbs):
                L.IedConnection_uninstallReportHandler(con, c_str(ref))
            for ctl in list(self._controls.values()):
                ctl._destroy()
            self._controls.clear()
            if graceful and not self._released and L.IedConnection_getState(con) == 2:
                err = C.c_int(0)
                L.IedConnection_release(con, C.byref(err))
                if err.value != 0:
                    L.IedConnection_abort(con, C.byref(err))
            L.IedConnection_close(con)
            L.IedConnection_destroy(con)  # joins the connection thread
            self._con = None
            self._mms = None
            self._report_cbs.clear()

    def __enter__(self) -> IedClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def wait_closed(self, timeout: float | None = None) -> bool:
        return self._closed_event.wait(timeout)

    # ------------------------------------------------------------------ identity / parameters
    def identify(self) -> ServerIdentity:
        _, mms = self._require()
        L = lib()
        err = C.c_int(0)
        p = L.MmsConnection_identify(mms, C.byref(err))
        _check_mms("identify", None, err)
        if not p:
            raise ServiceError("identify", None, codes.mms_error(9), "empty response")
        try:
            s = p.contents
            return ServerIdentity(py_str(s.vendorName), py_str(s.modelName), py_str(s.revision))
        finally:
            L.MmsServerIdentity_destroy(p)

    def connection_params(self) -> ConnectionParams:
        _, mms = self._require()
        p = lib().MmsConnection_getMmsConnectionParameters(mms)
        return ConnectionParams(
            max_pdu_size=p.maxPduSize,
            max_serv_outstanding_calling=p.maxServOutstandingCalling,
            max_serv_outstanding_called=p.maxServOutstandingCalled,
            data_structure_nesting_level=p.dataStructureNestingLevel,
            services_supported=bytes(p.servicesSupported),
        )

    # ------------------------------------------------------------------ directory
    def get_domain_names(self) -> list[str]:
        _, mms = self._require()
        err = C.c_int(0)
        head = lib().MmsConnection_getDomainNames(mms, C.byref(err))
        names = string_list(head)
        _check_mms("get-name-list", "domains", err)
        return names

    def get_domain_variable_names(self, domain: str) -> list[str]:
        _, mms = self._require()
        err = C.c_int(0)
        names = string_list(lib().MmsConnection_getDomainVariableNames(mms, C.byref(err), c_str(domain)))
        _check_mms("get-name-list", domain, err)
        return names

    def get_dataset_names(self, domain: str) -> list[str]:
        _, mms = self._require()
        err = C.c_int(0)
        names = string_list(lib().MmsConnection_getDomainVariableListNames(mms, C.byref(err), c_str(domain)))
        _check_mms("get-name-list", f"{domain} (datasets)", err)
        return names

    def get_variable_spec(self, domain: str, item: str) -> VarSpec:
        _, mms = self._require()
        L = lib()
        err = C.c_int(0)
        p = L.MmsConnection_getVariableAccessAttributes(mms, C.byref(err), c_str(domain), c_str(item))
        try:
            _check_mms("get-variable-access-attributes", f"{domain}/{item}", err)
            if not p:
                raise ServiceError("get-variable-access-attributes", f"{domain}/{item}", codes.mms_error(9), "empty")
            return spec_from_native(p)
        finally:
            if p:
                L.MmsVariableSpecification_destroy(p)

    def get_dataset_directory(self, domain: str, name: str) -> tuple[list[DataSetMember], bool]:
        """Members of a named variable list and whether the list is deletable."""
        _, mms = self._require()
        L = lib()
        err = C.c_int(0)
        deletable = C.c_bool(False)
        head = L.MmsConnection_readNamedVariableListDirectory(
            mms, C.byref(err), c_str(domain), c_str(name), C.byref(deletable)
        )
        members: list[DataSetMember] = []
        try:
            for p in iter_linked_list(head):
                s = C.cast(p, C.POINTER(_native.MmsVariableAccessSpecification)).contents
                members.append(
                    DataSetMember(
                        domain=py_str(s.domainId) or domain,
                        item=py_str(s.itemId) or "",
                        array_index=s.arrayIndex if s.arrayIndex >= 0 else None,
                        component=py_str(s.componentName),
                    )
                )
        finally:
            if head:
                L.LinkedList_destroyDeep(head, L.MmsVariableAccessSpecification_destroy_ptr)
        _check_mms("get-dataset-directory", f"{domain}/{name}", err)
        return members, bool(deletable.value)

    # ------------------------------------------------------------------ read / write
    def read(self, domain: str, item: str, spec: VarSpec | None = None) -> Value:
        """Read one MMS variable. A per-variable DataAccessError comes back as :class:`AccessError`."""
        _, mms = self._require()
        L = lib()
        err = C.c_int(0)
        p = L.MmsConnection_readVariable(mms, C.byref(err), c_str(domain), c_str(item))
        try:
            _check_mms("read", f"{domain}/{item}", err)
            if not p:
                raise ServiceError("read", f"{domain}/{item}", codes.mms_error(9), "empty response")
            return decode(p, spec)
        finally:
            if p:
                L.MmsValue_delete(p)

    def read_multiple(
        self, domain: str, items: Sequence[str], specs: Sequence[VarSpec | None] | None = None
    ) -> list[Value]:
        """Read several variables of one domain in a single request (results in request order)."""
        _, mms = self._require()
        if not items:
            return []
        L = lib()
        keep = [C.c_char_p(c_str(i)) for i in items]
        lst = L.LinkedList_create()
        for s in keep:
            L.LinkedList_add(lst, C.cast(s, C.c_void_p))
        err = C.c_int(0)
        p = None
        try:
            p = L.MmsConnection_readMultipleVariables(mms, C.byref(err), c_str(domain), lst)
            _check_mms("read", f"{domain} ({len(items)} variables)", err)
            if not p:
                raise ServiceError("read", domain, codes.mms_error(9), "empty response")
            n = L.MmsValue_getArraySize(p)
            out: list[Value] = []
            for i in range(len(items)):
                if i >= n:
                    out.append(AccessError.from_code(12))
                    continue
                sp = specs[i] if specs is not None else None
                out.append(decode(L.MmsValue_getElement(p, i), sp))
            return out
        finally:
            L.LinkedList_destroyStatic(lst)
            if p:
                L.MmsValue_delete(p)

    def read_dataset_values(self, domain: str, name: str, specs: Sequence[VarSpec | None] | None = None) -> list[Value]:
        _, mms = self._require()
        L = lib()
        err = C.c_int(0)
        p = L.MmsConnection_readNamedVariableListValues(mms, C.byref(err), c_str(domain), c_str(name), False)
        try:
            _check_mms("read-dataset", f"{domain}/{name}", err)
            if not p:
                return []
            n = L.MmsValue_getArraySize(p)
            return [decode(L.MmsValue_getElement(p, i), specs[i] if specs and i < len(specs) else None) for i in range(n)]
        finally:
            if p:
                L.MmsValue_delete(p)

    def write(self, domain: str, item: str, spec: VarSpec, value: Value) -> None:
        """Write one variable, encoding ``value`` according to the device's ``spec`` (RW-3).

        Raises ServiceError with a ``data-access`` code when the device refuses the write, or an
        ``mms`` code when the service itself failed.
        """
        _, mms = self._require()
        L = lib()
        p = encode(spec, value)
        try:
            err = C.c_int(0)
            dae = L.MmsConnection_writeVariable(mms, C.byref(err), c_str(domain), c_str(item), p)
        finally:
            L.MmsValue_delete(p)
        target = f"{domain}/{item}"
        if dae >= 0:
            raise ServiceError("write", target, codes.data_access_error(dae))
        _check_mms("write", target, err)

    # ------------------------------------------------------------------ report control blocks
    def get_rcb(self, reference: str) -> RcbValues:
        """Read all attributes of an RCB. ``reference`` like ``LD/LLN0.BR.brcb01``."""
        con, _ = self._require()
        L = lib()
        err = C.c_int(0)
        rcb = L.IedConnection_getRCBValues(con, C.byref(err), c_str(reference), None)
        try:
            _check_ied("get-rcb", reference, err)
            if not rcb:
                raise ServiceError("get-rcb", reference, codes.ied_error(99), "empty response")
            buffered = bool(L.ClientReportControlBlock_isBuffered(rcb))
            v = RcbValues(reference=reference, buffered=buffered)
            v.rpt_id = py_str(L.ClientReportControlBlock_getRptId(rcb))
            v.rpt_ena = bool(L.ClientReportControlBlock_getRptEna(rcb))
            v.dataset = py_str(L.ClientReportControlBlock_getDataSetReference(rcb))
            v.conf_rev = int(L.ClientReportControlBlock_getConfRev(rcb))
            v.opt_flds = int(L.ClientReportControlBlock_getOptFlds(rcb))
            v.buf_tm = int(L.ClientReportControlBlock_getBufTm(rcb))
            v.sq_num = int(L.ClientReportControlBlock_getSqNum(rcb))
            v.trg_ops = int(L.ClientReportControlBlock_getTrgOps(rcb))
            v.intg_pd = int(L.ClientReportControlBlock_getIntgPd(rcb))
            v.gi = bool(L.ClientReportControlBlock_getGI(rcb))
            if buffered:
                v.purge_buf = bool(L.ClientReportControlBlock_getPurgeBuf(rcb))
                eid = L.ClientReportControlBlock_getEntryId(rcb)
                if eid:
                    val = decode(eid)
                    v.entry_id = val if isinstance(val, bytes) else None
                v.time_of_entry_ms = int(L.ClientReportControlBlock_getEntryTime(rcb))
                if L.ClientReportControlBlock_hasResvTms(rcb):
                    v.resv_tms = int(L.ClientReportControlBlock_getResvTms(rcb))
            else:
                v.resv = bool(L.ClientReportControlBlock_getResv(rcb))
            owner = L.ClientReportControlBlock_getOwner(rcb)
            if owner:
                val = decode(owner)
                v.owner = val if isinstance(val, bytes) else None
            return v
        finally:
            if rcb:
                L.ClientReportControlBlock_destroy(rcb)

    def set_rcb(self, reference: str, changes: dict[str, object], *, single_request: bool = True) -> None:
        """Write RCB attributes. Keys: rpt_id, rpt_ena, resv, dataset, opt_flds, buf_tm, trg_ops,
        intg_pd, gi, purge_buf, entry_id (bytes), resv_tms."""
        con, _ = self._require()
        L = lib()
        rcb = L.ClientReportControlBlock_create(c_str(reference))
        entry_val = None
        try:
            mask = 0
            for key, val in changes.items():
                if key not in RCB_ELEMENTS or key in ("conf_rev", "sq_num", "time_of_entry", "owner"):
                    raise ValueError(f"RCB attribute {key!r} cannot be written")
                mask |= RCB_ELEMENTS[key]
                if key == "rpt_id":
                    L.ClientReportControlBlock_setRptId(rcb, c_str(str(val)))
                elif key == "rpt_ena":
                    L.ClientReportControlBlock_setRptEna(rcb, bool(val))
                elif key == "resv":
                    L.ClientReportControlBlock_setResv(rcb, bool(val))
                elif key == "dataset":
                    L.ClientReportControlBlock_setDataSetReference(rcb, c_str(str(val)))
                elif key == "opt_flds":
                    L.ClientReportControlBlock_setOptFlds(rcb, int(val))  # type: ignore[arg-type]
                elif key == "buf_tm":
                    L.ClientReportControlBlock_setBufTm(rcb, int(val))  # type: ignore[arg-type]
                elif key == "trg_ops":
                    L.ClientReportControlBlock_setTrgOps(rcb, int(val))  # type: ignore[arg-type]
                elif key == "intg_pd":
                    L.ClientReportControlBlock_setIntgPd(rcb, int(val))  # type: ignore[arg-type]
                elif key == "gi":
                    L.ClientReportControlBlock_setGI(rcb, bool(val))
                elif key == "purge_buf":
                    L.ClientReportControlBlock_setPurgeBuf(rcb, bool(val))
                elif key == "resv_tms":
                    L.ClientReportControlBlock_setResvTms(rcb, int(val))  # type: ignore[arg-type]
                elif key == "entry_id":
                    raw = bytes(val)  # type: ignore[arg-type]
                    entry_val = encode(VarSpec(MmsKind.OCTET_STRING, size=8), raw)
                    L.ClientReportControlBlock_setEntryId(rcb, entry_val)
            err = C.c_int(0)
            L.IedConnection_setRCBValues(con, C.byref(err), rcb, mask, single_request)
            _check_ied("set-rcb", f"{reference} {sorted(changes)}", err)
        finally:
            L.ClientReportControlBlock_destroy(rcb)
            if entry_val:
                L.MmsValue_delete(entry_val)

    def trigger_gi(self, reference: str) -> None:
        con, _ = self._require()
        err = C.c_int(0)
        lib().IedConnection_triggerGIReport(con, C.byref(err), c_str(reference))
        _check_ied("gi", reference, err)

    def install_report_handler(
        self,
        reference: str,
        rpt_id: str | None,
        callback: Callable[[Report], None],
        member_specs: Sequence[VarSpec | None] | None = None,
    ) -> None:
        """Deliver reports of ``reference`` to ``callback`` (called on the connection thread).

        libiec61850 matches reports by RptID (an empty RptID means the RCB reference).
        """
        con, _ = self._require()
        specs = list(member_specs) if member_specs is not None else None

        def on_report(_param: int | None, rpt: int) -> None:
            try:
                callback(_decode_report(rpt, specs))
            except Exception:  # never let an exception unwind into C
                pass

        cfunc = _native.ReportCallback(on_report)
        with self._lock:
            lib().IedConnection_installReportHandler(con, c_str(reference), c_str(rpt_id or ""), cfunc, None)
            self._report_cbs[reference] = (cfunc, callback)

    def uninstall_report_handler(self, reference: str) -> None:
        with self._lock:
            if self._con is not None and reference in self._report_cbs:
                lib().IedConnection_uninstallReportHandler(self._con, c_str(reference))
            # Keep the ctypes callback object alive briefly: a report may be in flight.
            entry = self._report_cbs.pop(reference, None)
            if entry is not None:
                _graveyard.append(entry[0])

    # ------------------------------------------------------------------ files
    def get_file_directory(self, directory: str = "") -> list[FileEntry]:
        con, _ = self._require()
        L = lib()
        out: list[FileEntry] = []
        cont: str | None = None
        for _ in range(10000):
            err = C.c_int(0)
            more = C.c_bool(False)
            head = L.IedConnection_getFileDirectoryEx(
                con, C.byref(err), c_str(directory) if directory else None, c_str(cont), C.byref(more)
            )
            try:
                for p in iter_linked_list(head):
                    name = py_str(L.FileDirectoryEntry_getFileName(p)) or ""
                    out.append(
                        FileEntry(name, int(L.FileDirectoryEntry_getFileSize(p)), int(L.FileDirectoryEntry_getLastModified(p)))
                    )
            finally:
                if head:
                    L.LinkedList_destroyDeep(head, L.FileDirectoryEntry_destroy_ptr)
            _check_ied("file-dir", directory or "/", err)
            if not more.value or not out:
                break
            cont = out[-1].name
        return out

    def get_file(self, remote: str, sink: IO[bytes], *, progress: Callable[[int], None] | None = None) -> int:
        """Download ``remote`` into the binary file object ``sink``; returns the byte count."""
        con, _ = self._require()
        received = [0]
        failure: list[BaseException] = []

        def handler(_param: int | None, buf: C.POINTER(C.c_uint8), n: int) -> bool:  # type: ignore[valid-type]
            try:
                sink.write(C.string_at(buf, n))
                received[0] += n
                if progress:
                    progress(received[0])
                return True
            except BaseException as exc:  # stop the transfer
                failure.append(exc)
                return False

        cfunc = _native.GetFileCallback(handler)
        err = C.c_int(0)
        lib().IedConnection_getFile(con, C.byref(err), c_str(remote), cfunc, None)
        if failure:
            raise failure[0]
        _check_ied("file-get", remote, err)
        return received[0]

    # ------------------------------------------------------------------ controls
    def control(self, reference: str) -> ControlObject:
        """Control object for ``reference`` (e.g. ``LD/CSWI1.Pos``); created once per association.

        libiec61850 reads ctlModel and the Oper structure while creating it.
        """
        con, _ = self._require()
        with self._lock:
            ctl = self._controls.get(reference)
            if ctl is not None:
                return ctl
            p = lib().ControlObjectClient_create(c_str(reference), con)
            if not p:
                raise ServiceError(
                    "control-create",
                    reference,
                    codes.ied_error(12),
                    "not a controllable object, or ctlModel/Oper could not be read",
                )
            ctl = ControlObject(self, reference, p)
            self._controls[reference] = ctl
            return ctl


# ctypes callback objects that were uninstalled; kept alive for the life of the process because a
# callback may still be queued on the connection thread (they are tiny).
_graveyard: list[object] = []


def _decode_report(rpt: int, specs: list[VarSpec | None] | None) -> Report:
    L = lib()
    r = Report(
        rcb_reference=py_str(L.ClientReport_getRcbReference(rpt)) or "",
        rpt_id=py_str(L.ClientReport_getRptId(rpt)) or "",
        received_at=time.time(),
    )
    if L.ClientReport_hasDataSetName(rpt):
        r.dataset = py_str(L.ClientReport_getDataSetName(rpt))
    if L.ClientReport_hasSeqNum(rpt):
        r.seq_num = int(L.ClientReport_getSeqNum(rpt))
    if L.ClientReport_hasSubSeqNum(rpt):
        r.sub_seq_num = int(L.ClientReport_getSubSeqNum(rpt))
        r.more_segments_follow = bool(L.ClientReport_getMoreSeqmentsFollow(rpt))
    if L.ClientReport_hasTimestamp(rpt):
        r.timestamp_ms = int(L.ClientReport_getTimestamp(rpt))
    if L.ClientReport_hasConfRev(rpt):
        r.conf_rev = int(L.ClientReport_getConfRev(rpt))
    if L.ClientReport_hasBufOvfl(rpt):
        r.buf_ovfl = bool(L.ClientReport_getBufOvfl(rpt))
    eid = L.ClientReport_getEntryId(rpt)
    if eid:
        val = decode(eid)
        r.entry_id = val if isinstance(val, bytes) else None
    values = L.ClientReport_getDataSetValues(rpt)
    has_reason = bool(L.ClientReport_hasReasonForInclusion(rpt))
    has_ref = bool(L.ClientReport_hasDataReference(rpt))
    if values:
        n = L.MmsValue_getArraySize(values)
        for i in range(n):
            reason = int(L.ClientReport_getReasonForInclusion(rpt, i)) if has_reason else -1
            elem = L.MmsValue_getElement(values, i)
            if reason == 0 or (reason == -1 and not elem):
                continue
            reasons = (
                tuple(name for bit, name in codes.REASONS_FOR_INCLUSION.items() if bit and reason & bit)
                if reason > 0
                else ()
            )
            spec = specs[i] if specs is not None and i < len(specs) else None
            ref = py_str(L.ClientReport_getDataReference(rpt, i)) if has_ref else None
            r.entries.append(ReportEntry(i, reasons, decode(elem, spec) if elem else None, ref))
    return r


class ControlObject:
    """One controllable data object (SPC, DPC, INC, …) on an open association."""

    def __init__(self, client: IedClient, reference: str, ptr: int) -> None:
        self.client = client
        self.reference = reference
        self._p: int | None = ptr
        self._terminations: list[float] = []  # arrival times of CommandTermination
        self._term_cond = threading.Condition()
        self._appl_before_operate: tuple[int, int, int] = (0, 0, 0)
        self._term_cfunc = _native.CommandTerminationCallback(self._on_termination)
        lib().ControlObjectClient_setCommandTerminationHandler(ptr, self._term_cfunc, None)
        self._ctl_num = 0
        lib().ControlObjectClient_setCtlNum(ptr, 0)

    # -- properties read by libiec61850 at creation
    @property
    def ctl_model(self) -> int:
        return int(lib().ControlObjectClient_getControlModel(self._ptr()))

    @property
    def ctl_val_kind(self) -> MmsKind | None:
        from .types import MMS_KIND_BY_NUMBER

        return MMS_KIND_BY_NUMBER.get(int(lib().ControlObjectClient_getCtlValType(self._ptr())))

    def _ptr(self) -> int:
        if self._p is None:
            raise NotConnectedError("control object has been released")
        return self._p

    def configure(
        self,
        *,
        or_ident: str,
        or_cat: int,
        test: bool = False,
        interlock_check: bool = True,
        synchro_check: bool = True,
    ) -> None:
        L = lib()
        p = self._ptr()
        L.ControlObjectClient_setOrigin(p, c_str(or_ident), or_cat)
        L.ControlObjectClient_setTestMode(p, test)
        L.ControlObjectClient_setInterlockCheck(p, interlock_check)
        L.ControlObjectClient_setSynchroCheck(p, synchro_check)

    def last_appl_error(self) -> tuple[int, int, int]:
        e = lib().ControlObjectClient_getLastApplError(self._ptr())
        return (int(e.ctlNum), int(e.error), int(e.addCause))

    def _step(self, service: str, call: Callable[[], bool], ctl_num: int) -> ControlStepResult:
        L = lib()
        before = self.last_appl_error()
        t0 = time.perf_counter()
        ok = bool(call())
        dt = time.perf_counter() - t0
        after = self.last_appl_error()
        ied = codes.ied_error(int(L.ControlObjectClient_getLastError(self._ptr())))
        fresh = after != before
        return ControlStepResult(
            service=service,
            ok=ok,
            duration_s=dt,
            ied_error=ied if not ok else codes.ied_error(0),
            add_cause=codes.add_cause(after[2]) if fresh else None,
            ctl_error=codes.ctl_error(after[1]) if fresh else None,
            ctl_num=ctl_num,
        )

    def select(self) -> ControlStepResult:
        """SBO with normal security: read the SBO attribute."""
        p = self._ptr()
        self._ctl_num = (self._ctl_num + 1) % 256
        return self._step("select", lambda: lib().ControlObjectClient_select(p), self._ctl_num)

    def select_with_value(self, spec: VarSpec, value: Value) -> ControlStepResult:
        p = self._ptr()
        val = encode(spec, value)
        try:
            self._ctl_num = (self._ctl_num + 1) % 256
            return self._step("select-with-value", lambda: lib().ControlObjectClient_selectWithValue(p, val), self._ctl_num)
        finally:
            lib().MmsValue_delete(val)

    def operate(self, spec: VarSpec, value: Value, oper_time_ms: int = 0) -> ControlStepResult:
        p = self._ptr()
        val = encode(spec, value)
        try:
            if self.ctl_model not in (2, 4):
                self._ctl_num = (self._ctl_num + 1) % 256
            with self._term_cond:
                self._terminations.clear()
            self._appl_before_operate = self.last_appl_error()
            return self._step("operate", lambda: lib().ControlObjectClient_operate(p, val, oper_time_ms), self._ctl_num)
        finally:
            lib().MmsValue_delete(val)

    def cancel(self) -> ControlStepResult:
        p = self._ptr()
        return self._step("cancel", lambda: lib().ControlObjectClient_cancel(p), self._ctl_num)

    # -- CommandTermination (enhanced security)
    def _on_termination(self, _param: int | None, _ctl: int | None) -> None:
        # Connection thread, called with libiec61850's control list lock held: only record the
        # arrival. Whether it is negative is decided in wait_termination(): libiec61850 may run
        # this callback before it has stored the LastApplError that accompanies a negative one.
        with self._term_cond:
            self._terminations.append(time.time())
            self._term_cond.notify_all()

    def wait_termination(self, timeout_s: float, grace_s: float = 0.15) -> CommandTermination | None:
        """Wait for CommandTermination; negative if a LastApplError arrived since the operate."""
        deadline = time.monotonic() + timeout_s
        with self._term_cond:
            while not self._terminations:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._term_cond.wait(min(remaining, 0.1))
            received_at = self._terminations.pop(0)
        after = self.last_appl_error()
        if after == self._appl_before_operate:
            time.sleep(grace_s)  # let a trailing LastApplError report be processed
            after = self.last_appl_error()
        negative = after != self._appl_before_operate
        return CommandTermination(
            positive=not negative,
            received_at=received_at,
            add_cause=codes.add_cause(after[2]) if negative else None,
            ctl_error=codes.ctl_error(after[1]) if negative else None,
            ctl_num=after[0] if negative else None,
        )

    def _destroy(self) -> None:
        if self._p is not None:
            lib().ControlObjectClient_destroy(self._p)
            self._p = None
            _graveyard.append(self._term_cfunc)
