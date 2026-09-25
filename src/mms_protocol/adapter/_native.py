"""Low-level access to the libiec61850 library shipped inside the pyiec61850-ng wheel.

Why ctypes and not the SWIG wrappers of pyiec61850-ng (Phase 0 spike RSK-1, see docs/spikes.md):

* The SWIG module is not built with ``-threads``: apart from four ``pyWrap_*`` functions, every
  blocking call (control operate/select/cancel, directory and file services, dataset reads, ...)
  keeps the Python GIL while it waits for the device. When a report or a CommandTermination
  arrives during such a call, libiec61850's receive thread must take the GIL to run the Python
  callback, while the calling thread waits for a response that the receive thread will never
  process: a permanent deadlock. The spike reproduced it within seconds under report load.
* ctypes releases the GIL for the duration of every foreign call, so callbacks can always run.
* Several SWIG typemaps reject NULL where the C API allows it (e.g. ``updateRcb`` of
  ``IedConnection_getRCBValues``).

The protocol stack is still pyiec61850-ng's: the symbols are resolved through the handle of its
SWIG extension, i.e. exactly the libiec61850 build (1.6.1) that the pinned wheel bundles.

This module is private to the adapter package (PLT-4). Nothing outside ``mms_protocol.adapter``
may import it; the test simulator is the one exception and uses only :func:`library`.
"""

from __future__ import annotations

import ctypes as C
import threading
from typing import Any

# ---------------------------------------------------------------------------
# C types
# ---------------------------------------------------------------------------
vp = C.c_void_p
cp = C.c_char_p
i32 = C.c_int32
u32 = C.c_uint32
u16 = C.c_uint16
u64 = C.c_uint64
i64 = C.c_int64
cbool = C.c_bool
cint = C.c_int
pint = C.POINTER(C.c_int)
pbool = C.POINTER(C.c_bool)


class LastApplError(C.Structure):
    _fields_ = [("ctlNum", C.c_int), ("error", C.c_int), ("addCause", C.c_int)]


class MmsConnectionParameters(C.Structure):
    _fields_ = [
        ("maxServOutstandingCalling", C.c_int),
        ("maxServOutstandingCalled", C.c_int),
        ("dataStructureNestingLevel", C.c_int),
        ("maxPduSize", C.c_int),
        ("servicesSupported", C.c_uint8 * 11),
    ]


class MmsServerIdentity(C.Structure):
    _fields_ = [("vendorName", C.c_char_p), ("modelName", C.c_char_p), ("revision", C.c_char_p)]


class MmsVariableAccessSpecification(C.Structure):
    _fields_ = [
        ("domainId", C.c_char_p),
        ("itemId", C.c_char_p),
        ("arrayIndex", C.c_int32),
        ("componentName", C.c_char_p),
    ]


class LinkedListNode(C.Structure):
    pass


LinkedListNode._fields_ = [("data", C.c_void_p), ("next", C.POINTER(LinkedListNode))]

# Callback signatures (all invoked on libiec61850's connection thread).
ReportCallback = C.CFUNCTYPE(None, vp, vp)  # (parameter, ClientReport)
CommandTerminationCallback = C.CFUNCTYPE(None, vp, vp)  # (parameter, ControlObjectClient)
ConnectionClosedCallback = C.CFUNCTYPE(None, vp, vp)  # (parameter, IedConnection)
GetFileCallback = C.CFUNCTYPE(C.c_bool, vp, C.POINTER(C.c_uint8), u32)  # (param, buffer, bytesRead)
LinkedListDeleteFunction = C.CFUNCTYPE(None, vp)

# name -> (restype, argtypes)
_PROTOTYPES: dict[str, tuple[Any, list[Any]]] = {
    # -- connection --------------------------------------------------------
    "IedConnection_create": (vp, []),
    "IedConnection_destroy": (None, [vp]),
    "IedConnection_setLocalAddress": (None, [vp, cp, cint]),
    "IedConnection_setConnectTimeout": (None, [vp, u32]),
    "IedConnection_setRequestTimeout": (None, [vp, u32]),
    "IedConnection_connect": (None, [vp, pint, cp, cint]),
    "IedConnection_release": (None, [vp, pint]),
    "IedConnection_abort": (None, [vp, pint]),
    "IedConnection_close": (None, [vp]),
    "IedConnection_getState": (cint, [vp]),
    "IedConnection_installConnectionClosedHandler": (None, [vp, ConnectionClosedCallback, vp]),
    "IedConnection_getMmsConnection": (vp, [vp]),
    "MmsConnection_getMmsConnectionParameters": (MmsConnectionParameters, [vp]),
    "MmsConnection_identify": (C.POINTER(MmsServerIdentity), [vp, pint]),
    "MmsServerIdentity_destroy": (None, [C.POINTER(MmsServerIdentity)]),
    "MmsConnection_getServerStatus": (None, [vp, pint, pint, pint, cbool]),
    # -- directory (MMS level) -------------------------------------------------
    "MmsConnection_getDomainNames": (vp, [vp, pint]),
    "MmsConnection_getDomainVariableNames": (vp, [vp, pint, cp]),
    "MmsConnection_getDomainVariableListNames": (vp, [vp, pint, cp]),
    "MmsConnection_getVariableAccessAttributes": (vp, [vp, pint, cp, cp]),
    "MmsConnection_readNamedVariableListDirectory": (vp, [vp, pint, cp, cp, pbool]),
    "MmsConnection_readVariable": (vp, [vp, pint, cp, cp]),
    "MmsConnection_readMultipleVariables": (vp, [vp, pint, cp, vp]),
    "MmsConnection_writeVariable": (cint, [vp, pint, cp, cp, vp]),
    "MmsConnection_readNamedVariableListValues": (vp, [vp, pint, cp, cp, cbool]),
    "MmsVariableAccessSpecification_destroy": (None, [vp]),
    # -- variable specifications -----------------------------------------------
    "MmsVariableSpecification_destroy": (None, [vp]),
    "MmsVariableSpecification_getType": (cint, [vp]),
    "MmsVariableSpecification_getName": (cp, [vp]),
    "MmsVariableSpecification_getSize": (cint, [vp]),
    "MmsVariableSpecification_getChildSpecificationByIndex": (vp, [vp, cint]),
    "MmsVariableSpecification_getArrayElementSpecification": (vp, [vp]),
    "MmsVariableSpecification_getExponentWidth": (cint, [vp]),
    # -- linked lists ----------------------------------------------------------
    "LinkedList_create": (vp, []),
    "LinkedList_destroy": (None, [vp]),
    "LinkedList_destroyStatic": (None, [vp]),
    "LinkedList_destroyDeep": (None, [vp, LinkedListDeleteFunction]),
    "LinkedList_add": (None, [vp, vp]),
    "LinkedList_size": (cint, [vp]),
    # -- MmsValue ----------------------------------------------------------------
    "MmsValue_delete": (None, [vp]),
    "MmsValue_getType": (cint, [vp]),
    "MmsValue_getArraySize": (u32, [vp]),
    "MmsValue_getElement": (vp, [vp, cint]),
    "MmsValue_getBoolean": (cbool, [vp]),
    "MmsValue_toInt64": (i64, [vp]),
    "MmsValue_toUint32": (u32, [vp]),
    "MmsValue_toDouble": (C.c_double, [vp]),
    "MmsValue_toFloat": (C.c_float, [vp]),
    "MmsValue_getBitStringSize": (cint, [vp]),
    "MmsValue_getBitStringBit": (cbool, [vp, cint]),
    "MmsValue_getOctetStringSize": (u16, [vp]),
    "MmsValue_getOctetStringBuffer": (C.POINTER(C.c_uint8), [vp]),
    "MmsValue_toString": (vp, [vp]),
    "MmsValue_getStringSize": (cint, [vp]),
    "MmsValue_getUtcTimeInMsWithUs": (u64, [vp, C.POINTER(u32)]),
    "MmsValue_getUtcTimeQuality": (C.c_uint8, [vp]),
    "MmsValue_getBinaryTimeAsUtcMs": (u64, [vp]),
    "MmsValue_getDataAccessError": (cint, [vp]),
    "MmsValue_getUtcTimeBuffer": (C.POINTER(C.c_uint8), [vp]),
    "MmsValue_newBoolean": (vp, [cbool]),
    "MmsValue_newIntegerFromInt32": (vp, [i32]),
    "MmsValue_newIntegerFromInt64": (vp, [i64]),
    "MmsValue_newUnsignedFromUint32": (vp, [u32]),
    "MmsValue_newFloat": (vp, [C.c_float]),
    "MmsValue_newDouble": (vp, [C.c_double]),
    "MmsValue_newBitString": (vp, [cint]),
    "MmsValue_setBitStringBit": (None, [vp, cint, cbool]),
    "MmsValue_newOctetString": (vp, [cint, cint]),
    "MmsValue_setOctetString": (None, [vp, C.POINTER(C.c_uint8), cint]),
    "MmsValue_newVisibleString": (vp, [cp]),
    "MmsValue_newMmsString": (vp, [cp]),
    "MmsValue_newUtcTimeByMsTime": (vp, [u64]),
    "MmsValue_setUtcTimeQuality": (None, [vp, C.c_uint8]),
    "MmsValue_newBinaryTime": (vp, [cbool]),
    "MmsValue_setBinaryTime": (None, [vp, u64]),
    "MmsValue_createEmptyStructure": (vp, [cint]),
    "MmsValue_createEmptyArray": (vp, [cint]),
    "MmsValue_setElement": (None, [vp, cint, vp]),
    # -- data sets (IEC level, only for convenience of references) ---------------
    "IedConnection_getDataSetDirectory": (vp, [vp, pint, cp, pbool]),
    # -- report control blocks -----------------------------------------------------
    "IedConnection_getRCBValues": (vp, [vp, pint, cp, vp]),
    "IedConnection_setRCBValues": (None, [vp, pint, vp, u32, cbool]),
    "IedConnection_installReportHandler": (None, [vp, cp, cp, ReportCallback, vp]),
    "IedConnection_uninstallReportHandler": (None, [vp, cp]),
    "IedConnection_triggerGIReport": (None, [vp, pint, cp]),
    "ClientReportControlBlock_create": (vp, [cp]),
    "ClientReportControlBlock_destroy": (None, [vp]),
    "ClientReportControlBlock_isBuffered": (cbool, [vp]),
    "ClientReportControlBlock_getRptId": (cp, [vp]),
    "ClientReportControlBlock_setRptId": (None, [vp, cp]),
    "ClientReportControlBlock_getRptEna": (cbool, [vp]),
    "ClientReportControlBlock_setRptEna": (None, [vp, cbool]),
    "ClientReportControlBlock_getResv": (cbool, [vp]),
    "ClientReportControlBlock_setResv": (None, [vp, cbool]),
    "ClientReportControlBlock_getDataSetReference": (cp, [vp]),
    "ClientReportControlBlock_setDataSetReference": (None, [vp, cp]),
    "ClientReportControlBlock_getConfRev": (u32, [vp]),
    "ClientReportControlBlock_getOptFlds": (cint, [vp]),
    "ClientReportControlBlock_setOptFlds": (None, [vp, cint]),
    "ClientReportControlBlock_getBufTm": (u32, [vp]),
    "ClientReportControlBlock_setBufTm": (None, [vp, u32]),
    "ClientReportControlBlock_getSqNum": (u16, [vp]),
    "ClientReportControlBlock_getTrgOps": (cint, [vp]),
    "ClientReportControlBlock_setTrgOps": (None, [vp, cint]),
    "ClientReportControlBlock_getIntgPd": (u32, [vp]),
    "ClientReportControlBlock_setIntgPd": (None, [vp, u32]),
    "ClientReportControlBlock_getGI": (cbool, [vp]),
    "ClientReportControlBlock_setGI": (None, [vp, cbool]),
    "ClientReportControlBlock_getPurgeBuf": (cbool, [vp]),
    "ClientReportControlBlock_setPurgeBuf": (None, [vp, cbool]),
    "ClientReportControlBlock_hasResvTms": (cbool, [vp]),
    "ClientReportControlBlock_getResvTms": (C.c_int16, [vp]),
    "ClientReportControlBlock_setResvTms": (None, [vp, C.c_int16]),
    "ClientReportControlBlock_getEntryId": (vp, [vp]),
    "ClientReportControlBlock_setEntryId": (None, [vp, vp]),
    "ClientReportControlBlock_getEntryTime": (u64, [vp]),
    "ClientReportControlBlock_getOwner": (vp, [vp]),
    # -- received reports (valid only inside the report callback) ----------------
    "ClientReport_getDataSetName": (cp, [vp]),
    "ClientReport_getDataSetValues": (vp, [vp]),
    "ClientReport_getRcbReference": (cp, [vp]),
    "ClientReport_getRptId": (cp, [vp]),
    "ClientReport_getReasonForInclusion": (cint, [vp, cint]),
    "ClientReport_getEntryId": (vp, [vp]),
    "ClientReport_hasTimestamp": (cbool, [vp]),
    "ClientReport_getTimestamp": (u64, [vp]),
    "ClientReport_hasSeqNum": (cbool, [vp]),
    "ClientReport_getSeqNum": (u16, [vp]),
    "ClientReport_hasSubSeqNum": (cbool, [vp]),
    "ClientReport_getSubSeqNum": (u16, [vp]),
    "ClientReport_getMoreSeqmentsFollow": (cbool, [vp]),
    "ClientReport_hasDataSetName": (cbool, [vp]),
    "ClientReport_hasReasonForInclusion": (cbool, [vp]),
    "ClientReport_hasConfRev": (cbool, [vp]),
    "ClientReport_getConfRev": (u32, [vp]),
    "ClientReport_hasBufOvfl": (cbool, [vp]),
    "ClientReport_getBufOvfl": (cbool, [vp]),
    "ClientReport_hasDataReference": (cbool, [vp]),
    "ClientReport_getDataReference": (cp, [vp, cint]),
    # -- controls ------------------------------------------------------------------
    "ControlObjectClient_create": (vp, [cp, vp]),
    "ControlObjectClient_destroy": (None, [vp]),
    "ControlObjectClient_getControlModel": (cint, [vp]),
    "ControlObjectClient_setControlModel": (None, [vp, cint]),
    "ControlObjectClient_getCtlValType": (cint, [vp]),
    "ControlObjectClient_getLastError": (cint, [vp]),
    "ControlObjectClient_operate": (cbool, [vp, vp, u64]),
    "ControlObjectClient_select": (cbool, [vp]),
    "ControlObjectClient_selectWithValue": (cbool, [vp, vp]),
    "ControlObjectClient_cancel": (cbool, [vp]),
    "ControlObjectClient_getLastApplError": (LastApplError, [vp]),
    "ControlObjectClient_setTestMode": (None, [vp, cbool]),
    "ControlObjectClient_setOrigin": (None, [vp, cp, cint]),
    "ControlObjectClient_setInterlockCheck": (None, [vp, cbool]),
    "ControlObjectClient_setSynchroCheck": (None, [vp, cbool]),
    "ControlObjectClient_setCtlNum": (None, [vp, C.c_uint8]),
    "ControlObjectClient_useConstantT": (None, [vp, cbool]),
    "ControlObjectClient_setCommandTerminationHandler": (None, [vp, CommandTerminationCallback, vp]),
    # -- files -----------------------------------------------------------------------
    "IedConnection_getFileDirectoryEx": (vp, [vp, pint, cp, cp, pbool]),
    "IedConnection_getFile": (u32, [vp, pint, cp, GetFileCallback, vp]),
    "FileDirectoryEntry_getFileName": (cp, [vp]),
    "FileDirectoryEntry_getFileSize": (u32, [vp]),
    "FileDirectoryEntry_getLastModified": (u64, [vp]),
    "FileDirectoryEntry_destroy": (None, [vp]),
}


class _Lib:
    """Namespace object carrying the typed foreign functions as attributes."""

    def __init__(self, handle: C.CDLL) -> None:
        self.handle = handle
        missing = []
        for name, (restype, argtypes) in _PROTOTYPES.items():
            try:
                fn = getattr(handle, name)
            except AttributeError:
                missing.append(name)
                continue
            fn.restype = restype
            fn.argtypes = argtypes
            setattr(self, name, fn)
        if missing:
            raise NativeLibraryError(
                "the bundled libiec61850 lacks required symbols: " + ", ".join(sorted(missing))
            )
        # Deleters for LinkedList_destroyDeep (C function pointers, not Python callbacks).
        self.MmsValue_delete_ptr = LinkedListDeleteFunction(
            C.cast(handle.MmsValue_delete, C.c_void_p).value
        )
        self.FileDirectoryEntry_destroy_ptr = LinkedListDeleteFunction(
            C.cast(handle.FileDirectoryEntry_destroy, C.c_void_p).value
        )
        self.MmsVariableAccessSpecification_destroy_ptr = LinkedListDeleteFunction(
            C.cast(handle.MmsVariableAccessSpecification_destroy, C.c_void_p).value
        )


class NativeLibraryError(RuntimeError):
    """The pyiec61850-ng wheel or its bundled libiec61850 could not be loaded."""


_lock = threading.Lock()
_lib: _Lib | None = None
_handle: C.CDLL | None = None


def library() -> C.CDLL:
    """Raw ctypes handle of the bundled libiec61850 (symbols resolved via the SWIG extension)."""
    global _handle
    with _lock:
        if _handle is None:
            try:
                import pyiec61850.pyiec61850 as ext  # the only pyiec61850 import in the code base
            except Exception as exc:  # pragma: no cover - depends on the installation
                raise NativeLibraryError(
                    f"pyiec61850-ng could not be imported ({exc}). Install the pinned wheel into the "
                    "project's virtual environment (uv sync); see SPEC.md PLT-2."
                ) from exc
            _handle = C.CDLL(ext._pyiec61850.__file__)
        return _handle


def lib() -> _Lib:
    """Typed function table (loaded once per process)."""
    global _lib
    if _lib is None:
        handle = library()
        with _lock:
            if _lib is None:
                _lib = _Lib(handle)
    return _lib


def libiec61850_version() -> str:
    """Version of the bundled libiec61850 as reported by pyiec61850-ng's packaging."""
    try:
        from importlib.metadata import version

        wheel = version("pyiec61850-ng")
    except Exception:  # pragma: no cover
        return "unknown"
    # Package version is <libiec61850 version>.<package revision>
    parts = wheel.split(".")
    return ".".join(parts[:3]) if len(parts) >= 3 else wheel


def pyiec61850_version() -> str:
    try:
        from importlib.metadata import version

        return version("pyiec61850-ng")
    except Exception:  # pragma: no cover
        return "unknown"


# ---------------------------------------------------------------------------
# Small helpers used by the rest of the adapter
# ---------------------------------------------------------------------------
def iter_linked_list(head: int | None):
    """Yield the ``data`` pointers of a libiec61850 LinkedList (the head is a dummy node)."""
    if not head:
        return
    node = C.cast(head, C.POINTER(LinkedListNode)).contents
    nxt = node.next
    while nxt:
        cur = nxt.contents
        yield cur.data
        nxt = cur.next


def string_list(head: int | None) -> list[str]:
    """Decode a LinkedList of ``char*`` and destroy it (LinkedList_destroy frees the strings)."""
    if not head:
        return []
    try:
        return [C.string_at(p).decode("utf-8", "replace") for p in iter_linked_list(head) if p]
    finally:
        lib().LinkedList_destroy(head)


def c_str(s: str | None) -> bytes | None:
    return None if s is None else s.encode("utf-8")


def py_str(b: bytes | None) -> str | None:
    return None if b is None else b.decode("utf-8", "replace")
