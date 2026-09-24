"""Adapter layer: the only code that touches pyiec61850-ng / libiec61850 (PLT-4).

It owns native lifetimes and error mapping; everything it returns is plain Python.
"""

from ._native import NativeLibraryError, libiec61850_version, pyiec61850_version
from .client import STATE_NAMES, ControlObject, IedClient
from .codec import parse_text
from .errors import AdapterError, ConnectError, EncodeError, NotConnectedError, ServiceError
from .types import (
    AccessError,
    BinaryTime,
    BitString,
    CommandTermination,
    ConnectionParams,
    ControlStepResult,
    DataSetMember,
    FileEntry,
    MmsKind,
    RawValue,
    RcbValues,
    Report,
    ReportEntry,
    ServerIdentity,
    UtcTime,
    Value,
    VarSpec,
    format_value,
    value_from_json,
    value_to_json,
)

__all__ = [
    "STATE_NAMES",
    "AccessError",
    "AdapterError",
    "BinaryTime",
    "BitString",
    "CommandTermination",
    "ConnectError",
    "ConnectionParams",
    "ControlObject",
    "ControlStepResult",
    "DataSetMember",
    "EncodeError",
    "FileEntry",
    "IedClient",
    "MmsKind",
    "NativeLibraryError",
    "NotConnectedError",
    "RawValue",
    "RcbValues",
    "Report",
    "ReportEntry",
    "ServerIdentity",
    "ServiceError",
    "UtcTime",
    "Value",
    "VarSpec",
    "format_value",
    "libiec61850_version",
    "parse_text",
    "pyiec61850_version",
    "value_from_json",
    "value_to_json",
]
