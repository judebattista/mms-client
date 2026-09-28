from __future__ import annotations

import pytest

from ied_client.core.model import DeviceModel, LogicalDeviceInfo, LogicalNodeInfo
from ied_client.protocol.types import ControlBlockInfo, LogicalNodeModel, ValueKind, VarSpec


def S(name, *children):
    return VarSpec(ValueKind.STRUCTURE, name, len(children), tuple(children))


def B(name, kind=ValueKind.BOOLEAN, size=None):
    return VarSpec(kind, name, size)


def _ln(name, cbs=(), **by_fc):
    m = LogicalNodeModel()
    for fc, dos in by_fc.items():
        for do in dos:
            m.add_data(fc, do)
    for cb in cbs:
        m.add_control_block(cb)
    return LogicalNodeInfo.from_model("CTRL", name, m)


@pytest.fixture
def model() -> DeviceModel:
    """CTRL/{LLN0, CSWI1, GGIO1} built offline (no network)."""
    q = B("q", ValueKind.BIT_STRING, -13)
    t = B("t", ValueKind.UTC_TIME)
    cswi = _ln(
        "CSWI1",
        ST=[S("Pos", B("stVal", ValueKind.BIT_STRING, 2), q, t), S("Loc", B("stVal"), q, t)],
        CO=[S("Pos", S("Oper", B("ctlVal"), B("ctlNum", ValueKind.UNSIGNED, 8)))],
        CF=[S("Pos", B("ctlModel", ValueKind.INTEGER, 8), B("sboTimeout", ValueKind.UNSIGNED, 32))],
    )
    ggio = _ln(
        "GGIO1",
        MX=[S("AnIn1", S("mag", B("f", ValueKind.FLOAT, 32)), q, t)],
        SP=[S("Setp1", B("setVal", ValueKind.INTEGER, 32))],
    )
    urcb = ControlBlockInfo("CTRL", "LLN0", "urcbA01", "RP", S("urcbA01", B("RptID", ValueKind.VISIBLE_STRING, -65)))
    lln0 = _ln("LLN0", [urcb], ST=[S("Mod", B("stVal", ValueKind.INTEGER, 8), q, t)])
    ld = LogicalDeviceInfo("CTRL", datasets=["LLN0$Events"])
    for ln in (lln0, cswi, ggio):
        ld.lns[ln.name] = ln
    return DeviceModel(lds={"CTRL": ld})
