"""Test-only ctypes access to the libiec61850 bundled with pyiec61850-ng.

Used to prove that ``to_libiec61850_config()`` output loads in libiec61850 v1.6.1 and that a
server built from it behaves like the SCL says. Production code never does this (PLT-4).
"""

from __future__ import annotations

import ctypes
import socket
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pyiec61850.pyiec61850 as _ext

lib = ctypes.CDLL(_ext._pyiec61850.__file__)

_vp = ctypes.c_void_p
_err = ctypes.POINTER(ctypes.c_int)


def _sig(name: str, restype: object, *argtypes: object) -> None:
    fn = getattr(lib, name)
    fn.restype = restype
    fn.argtypes = list(argtypes)


_sig("ConfigFileParser_createModelFromConfigFileEx", _vp, ctypes.c_char_p)
_sig("IedModel_getModelNodeByObjectReference", _vp, _vp, ctypes.c_char_p)
_sig("IedModel_destroy", None, _vp)
_sig("IedServer_create", _vp, _vp)
_sig("IedServer_start", None, _vp, ctypes.c_int)
_sig("IedServer_isRunning", ctypes.c_bool, _vp)
_sig("IedServer_stop", None, _vp)
_sig("IedServer_destroy", None, _vp)
_sig("IedConnection_create", _vp)
_sig("IedConnection_connect", None, _vp, _err, ctypes.c_char_p, ctypes.c_int)
_sig("IedConnection_close", None, _vp)
_sig("IedConnection_destroy", None, _vp)
_sig("IedConnection_readObject", _vp, _vp, _err, ctypes.c_char_p, ctypes.c_int)
_sig("IedConnection_getDataSetDirectory", _vp, _vp, _err, ctypes.c_char_p, ctypes.POINTER(ctypes.c_bool))
_sig("IedConnection_getLogicalDeviceList", _vp, _vp, _err)
_sig("IedConnection_getRCBValues", _vp, _vp, _err, ctypes.c_char_p, _vp)
_sig("ClientReportControlBlock_getRptId", ctypes.c_char_p, _vp)
_sig("ClientReportControlBlock_getDataSetReference", ctypes.c_char_p, _vp)
_sig("ClientReportControlBlock_getConfRev", ctypes.c_uint32, _vp)
_sig("ClientReportControlBlock_getTrgOps", ctypes.c_int, _vp)
_sig("ClientReportControlBlock_getOptFlds", ctypes.c_int, _vp)
_sig("ClientReportControlBlock_getIntgPd", ctypes.c_uint32, _vp)
_sig("ClientReportControlBlock_getBufTm", ctypes.c_uint32, _vp)
_sig("ClientReportControlBlock_isBuffered", ctypes.c_bool, _vp)
_sig("ClientReportControlBlock_destroy", None, _vp)
_sig("MmsValue_getType", ctypes.c_int, _vp)
_sig("MmsValue_toInt32", ctypes.c_int32, _vp)
_sig("MmsValue_toUint32", ctypes.c_uint32, _vp)
_sig("MmsValue_toFloat", ctypes.c_float, _vp)
_sig("MmsValue_getBoolean", ctypes.c_bool, _vp)
_sig("MmsValue_toString", ctypes.c_char_p, _vp)
_sig("MmsValue_delete", None, _vp)
_sig("LinkedList_getNext", _vp, _vp)
_sig("LinkedList_getData", _vp, _vp)
_sig("LinkedList_destroy", None, _vp)

# libiec61850 MmsType numbers used below
MMS_BOOLEAN, MMS_INTEGER, MMS_UNSIGNED, MMS_FLOAT, MMS_VISIBLE_STRING, MMS_STRING = 2, 4, 5, 6, 8, 13


def load_model(path: Path) -> int | None:
    """``IedModel*`` from a config file, or None if the parser rejected it."""
    return lib.ConfigFileParser_createModelFromConfigFileEx(str(path).encode())


def has_node(model: int, ref: str) -> bool:
    return bool(lib.IedModel_getModelNodeByObjectReference(model, ref.encode()))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    return port if port > 10000 else free_port()


def _strings(linked_list: int | None) -> list[str]:
    out: list[str] = []
    if not linked_list:
        return out
    node = lib.LinkedList_getNext(linked_list)
    while node:
        data = lib.LinkedList_getData(node)
        out.append(ctypes.cast(data, ctypes.c_char_p).value.decode())
        node = lib.LinkedList_getNext(node)
    lib.LinkedList_destroy(linked_list)
    return out


class Client:
    """Minimal blocking client over ctypes."""

    def __init__(self, port: int) -> None:
        self.con = lib.IedConnection_create()
        err = ctypes.c_int(0)
        lib.IedConnection_connect(self.con, ctypes.byref(err), b"127.0.0.1", port)
        if err.value != 0:
            lib.IedConnection_destroy(self.con)
            raise ConnectionError(f"IedConnection_connect failed: {err.value}")

    def close(self) -> None:
        lib.IedConnection_close(self.con)
        lib.IedConnection_destroy(self.con)

    def read(self, ref: str, fc: int) -> bool | int | float | str:
        err = ctypes.c_int(0)
        v = lib.IedConnection_readObject(self.con, ctypes.byref(err), ref.encode(), fc)
        if err.value != 0 or not v:
            raise LookupError(f"read {ref} failed: {err.value}")
        try:
            t = lib.MmsValue_getType(v)
            if t == MMS_BOOLEAN:
                return bool(lib.MmsValue_getBoolean(v))
            if t == MMS_INTEGER:
                return int(lib.MmsValue_toInt32(v))
            if t == MMS_UNSIGNED:
                return int(lib.MmsValue_toUint32(v))
            if t == MMS_FLOAT:
                return float(lib.MmsValue_toFloat(v))
            if t in (MMS_VISIBLE_STRING, MMS_STRING):
                return lib.MmsValue_toString(v).decode()
            raise TypeError(f"unsupported MmsType {t} for {ref}")
        finally:
            lib.MmsValue_delete(v)

    def logical_devices(self) -> list[str]:
        err = ctypes.c_int(0)
        return _strings(lib.IedConnection_getLogicalDeviceList(self.con, ctypes.byref(err)))

    def dataset_directory(self, ref: str) -> list[str]:
        err = ctypes.c_int(0)
        deletable = ctypes.c_bool(False)
        lst = lib.IedConnection_getDataSetDirectory(self.con, ctypes.byref(err), ref.encode(), ctypes.byref(deletable))
        if err.value != 0:
            raise LookupError(f"dataset directory {ref} failed: {err.value}")
        return _strings(lst)

    def rcb(self, ref: str) -> dict[str, object]:
        err = ctypes.c_int(0)
        r = lib.IedConnection_getRCBValues(self.con, ctypes.byref(err), ref.encode(), None)
        if err.value != 0 or not r:
            raise LookupError(f"RCB {ref} failed: {err.value}")
        try:
            rpt_id = lib.ClientReportControlBlock_getRptId(r)
            ds = lib.ClientReportControlBlock_getDataSetReference(r)
            return {
                "rptID": rpt_id.decode() if rpt_id else None,
                "datSet": ds.decode() if ds else None,
                "confRev": lib.ClientReportControlBlock_getConfRev(r),
                "trgOps": lib.ClientReportControlBlock_getTrgOps(r),
                "optFlds": lib.ClientReportControlBlock_getOptFlds(r),
                "intgPd": lib.ClientReportControlBlock_getIntgPd(r),
                "bufTm": lib.ClientReportControlBlock_getBufTm(r),
                "buffered": lib.ClientReportControlBlock_isBuffered(r),
            }
        finally:
            lib.ClientReportControlBlock_destroy(r)


@contextmanager
def running_server(config_path: Path) -> Iterator[int]:
    """Start a libiec61850 server from a config file; yields the TCP port."""
    model = load_model(config_path)
    if not model:
        raise AssertionError(f"libiec61850 rejected {config_path}")
    server = lib.IedServer_create(model)
    port = free_port()
    lib.IedServer_start(server, port)
    try:
        if not lib.IedServer_isRunning(server):
            raise AssertionError(f"server did not start on port {port}")
        yield port
    finally:
        lib.IedServer_stop(server)
        lib.IedServer_destroy(server)
        lib.IedModel_destroy(model)
