"""Server-side libiec61850 prototypes for the simulated IED (test infrastructure only).

Uses the same library handle as the adapter so that the simulator runs exactly the stack the
client is built on.
"""

from __future__ import annotations

import ctypes as C

from mms_protocol.adapter._native import library

vp = C.c_void_p
cp = C.c_char_p
cint = C.c_int
cbool = C.c_bool
u8 = C.c_uint8
u32 = C.c_uint32
u64 = C.c_uint64

ControlHandler = C.CFUNCTYPE(cint, vp, vp, vp, cbool)  # (action, param, ctlVal, test)
CheckHandler = C.CFUNCTYPE(cint, vp, vp, vp, cbool, cbool)  # (action, param, ctlVal, test, interlockCheck)
WaitForExecutionHandler = C.CFUNCTYPE(cint, vp, vp, vp, cbool, cbool)
ConnectionIndicationHandler = C.CFUNCTYPE(None, vp, vp, cbool, vp)  # (server, connection, connected, param)
ActiveSgChangedHandler = C.CFUNCTYPE(cbool, vp, vp, u8, vp)
EditSgChangedHandler = C.CFUNCTYPE(cbool, vp, vp, u8, vp)
EditSgConfirmationHandler = C.CFUNCTYPE(None, vp, vp, u8)
WriteAccessHandler = C.CFUNCTYPE(cint, vp, vp, vp, vp)  # (dataAttribute, value, connection, param)

_PROTOS = {
    "ConfigFileParser_createModelFromConfigFileEx": (vp, [cp]),
    "IedModel_destroy": (None, [vp]),
    "IedModel_getModelNodeByObjectReference": (vp, [vp, cp]),
    "IedModel_getLogicalDeviceCount": (cint, [vp]),
    "IedModel_getDeviceByIndex": (vp, [vp, cint]),
    "LogicalDevice_getSettingGroupControlBlock": (vp, [vp]),
    "ModelNode_getChildCount": (cint, [vp]),
    "ModelNode_getChildWithIdx": (vp, [vp, cint]),
    "ModelNode_getChild": (vp, [vp, cp]),
    "ModelNode_getChildWithFc": (vp, [vp, cp, cint]),
    "ModelNode_getName": (cp, [vp]),
    "ModelNode_getType": (cint, [vp]),
    "ModelNode_getParent": (vp, [vp]),
    "ModelNode_getObjectReference": (vp, [vp, cp]),
    "DataAttribute_getFC": (cint, [vp]),
    "DataAttribute_getType": (cint, [vp]),
    "IedServerConfig_create": (vp, []),
    "IedServerConfig_destroy": (None, [vp]),
    "IedServerConfig_setEdition": (None, [vp, u8]),
    "IedServerConfig_setMaxMmsConnections": (None, [vp, cint]),
    "IedServerConfig_setFileServiceBasePath": (None, [vp, cp]),
    "IedServerConfig_enableFileService": (None, [vp, cbool]),
    "IedServerConfig_enableOwnerForRCB": (None, [vp, cbool]),
    "IedServerConfig_enableResvTmsForBRCB": (None, [vp, cbool]),
    "IedServerConfig_enableEditSG": (None, [vp, cbool]),
    "IedServerConfig_enableResvTmsForSGCB": (None, [vp, cbool]),
    "IedServer_createWithConfig": (vp, [vp, vp, vp]),
    "IedServer_destroy": (None, [vp]),
    "IedServer_start": (None, [vp, cint]),
    "IedServer_stop": (None, [vp]),
    "IedServer_isRunning": (cbool, [vp]),
    "IedServer_setLocalIpAddress": (None, [vp, cp]),
    "IedServer_setServerIdentity": (None, [vp, cp, cp, cp]),
    "IedServer_getNumberOfOpenConnections": (cint, [vp]),
    "IedServer_setConnectionIndicationHandler": (None, [vp, ConnectionIndicationHandler, vp]),
    "IedServer_lockDataModel": (None, [vp]),
    "IedServer_unlockDataModel": (None, [vp]),
    "IedServer_getAttributeValue": (vp, [vp, vp]),
    "IedServer_updateAttributeValue": (None, [vp, vp, vp]),
    "IedServer_updateFloatAttributeValue": (None, [vp, vp, C.c_float]),
    "IedServer_updateInt32AttributeValue": (None, [vp, vp, C.c_int32]),
    "IedServer_updateUnsignedAttributeValue": (None, [vp, vp, u32]),
    "IedServer_updateBooleanAttributeValue": (None, [vp, vp, cbool]),
    "IedServer_updateDbposValue": (None, [vp, vp, cint]),
    "IedServer_updateUTCTimeAttributeValue": (None, [vp, vp, u64]),
    "IedServer_setControlHandler": (None, [vp, vp, ControlHandler, vp]),
    "IedServer_setPerformCheckHandler": (None, [vp, vp, CheckHandler, vp]),
    "IedServer_setWaitForExecutionHandler": (None, [vp, vp, WaitForExecutionHandler, vp]),
    "IedServer_setActiveSettingGroupChangedHandler": (None, [vp, vp, ActiveSgChangedHandler, vp]),
    "IedServer_setEditSettingGroupChangedHandler": (None, [vp, vp, EditSgChangedHandler, vp]),
    "IedServer_setEditSettingGroupConfirmationHandler": (None, [vp, vp, EditSgConfirmationHandler, vp]),
    "IedServer_getActiveSettingGroup": (u8, [vp, vp]),
    "IedServer_handleWriteAccess": (None, [vp, vp, WriteAccessHandler, vp]),
    "IedServer_setWriteAccessPolicy": (None, [vp, cint, cint]),
    "ClientConnection_getPeerAddress": (cp, [vp]),
    "ClientConnection_abort": (cbool, [vp]),
    "ControlAction_setAddCause": (None, [vp, cint]),
    "ControlAction_setError": (None, [vp, cint]),
    "ControlAction_getOrCat": (cint, [vp]),
    "ControlAction_getOrIdent": (C.POINTER(C.c_uint8), [vp, C.POINTER(cint)]),
    "ControlAction_getCtlNum": (cint, [vp]),
    "ControlAction_isSelect": (cbool, [vp]),
    "ControlAction_getControlObject": (vp, [vp]),
    "ControlAction_getInterlockCheck": (cbool, [vp]),
    "ControlAction_getSynchroCheck": (cbool, [vp]),
    "MmsValue_getType": (cint, [vp]),
    "MmsValue_getBoolean": (cbool, [vp]),
    "MmsValue_toInt32": (C.c_int32, [vp]),
    "MmsValue_toFloat": (C.c_float, [vp]),
    "MmsValue_clone": (vp, [vp]),
    "MmsValue_delete": (None, [vp]),
    "MmsValue_newOctetString": (vp, [cint, cint]),
    "MmsValue_setOctetString": (None, [vp, C.POINTER(C.c_uint8), cint]),
    "MmsValue_getBitStringSize": (cint, [vp]),
}


class ModelNodeStruct(C.Structure):
    """Common prefix of sLogicalDevice / sLogicalNode / sDataObject / sDataAttribute."""


ModelNodeStruct._fields_ = [
    ("modelType", cint),
    ("name", cp),
    ("parent", vp),
    ("sibling", vp),
    ("firstChild", vp),
]


def children(node: int) -> list[int]:
    """Children of any model node (ModelNode_getChildWithIdx only works for DO/DA)."""
    out = []
    child = C.cast(node, C.POINTER(ModelNodeStruct)).contents.firstChild
    while child:
        out.append(child)
        child = C.cast(child, C.POINTER(ModelNodeStruct)).contents.sibling
    return out


class _Srv:
    pass


_srv: _Srv | None = None


def srv() -> _Srv:
    global _srv
    if _srv is None:
        h = library()
        s = _Srv()
        for name, (res, args) in _PROTOS.items():
            f = getattr(h, name)
            f.restype = res
            f.argtypes = args
            setattr(s, name, f)
        _srv = s
    return _srv


# Enumerations (libiec61850 1.6.1)
CONTROL_ACCEPTED = -1
CONTROL_OBJECT_ACCESS_DENIED = 3
CONTROL_TEMPORARILY_UNAVAILABLE = 2
CONTROL_RESULT_FAILED = 0
CONTROL_RESULT_OK = 1
CONTROL_RESULT_WAITING = 2
ACCESS_POLICY_ALLOW = 0
ACCESS_POLICY_DENY = 1
DATA_ACCESS_ERROR_SUCCESS = -1
DATA_ACCESS_ERROR_SUCCESS_NO_UPDATE = -3
DATA_ACCESS_ERROR_OBJECT_ACCESS_DENIED = 3
NODE_LD, NODE_LN, NODE_DO, NODE_DA = 0, 1, 2, 3
FC = {"ST": 0, "MX": 1, "SP": 2, "SV": 3, "CF": 4, "DC": 5, "SG": 6, "SE": 7, "CO": 12}
TYPE_BOOLEAN, TYPE_INT32, TYPE_ENUM, TYPE_CODEDENUM, TYPE_FLOAT32 = 0, 3, 12, 25, 10
