"""Adapter layer: the only code that touches pyiec61850-ng / libiec61850 (PLT-4).

It owns native lifetimes and error mapping; everything it returns is plain Python (the client's
shared types from :mod:`ied_client.protocol.types`, plus the MMS-level types in :mod:`.types`).
"""

from ._native import NativeLibraryError, libiec61850_version, pyiec61850_version
from .client import STATE_NAMES, ControlObject, IedClient
from .types import MMS_KIND_BY_NUMBER, RCB_ELEMENTS, SERVICE_BITS, ConnectionParams, VariableListEntry

__all__ = [
    "MMS_KIND_BY_NUMBER",
    "RCB_ELEMENTS",
    "SERVICE_BITS",
    "STATE_NAMES",
    "ConnectionParams",
    "ControlObject",
    "IedClient",
    "NativeLibraryError",
    "VariableListEntry",
    "libiec61850_version",
    "pyiec61850_version",
]
