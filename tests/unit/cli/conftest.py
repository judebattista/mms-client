from __future__ import annotations

import pytest

from mms_client.adapter import MmsKind, VarSpec
from mms_client.core.model import DeviceModel, LogicalDeviceInfo, build_ln


def S(name, *children):
    return VarSpec(MmsKind.STRUCTURE, name, len(children), tuple(children))


def B(name, kind=MmsKind.BOOLEAN, size=None):
    return VarSpec(kind, name, size)


@pytest.fixture
def model() -> DeviceModel:
    """CTRL/{LLN0, CSWI1, GGIO1} built offline with build_ln (no network)."""
    q = B("q", MmsKind.BIT_STRING, -13)
    t = B("t", MmsKind.UTC_TIME)
    cswi = S(
        "CSWI1",
        S("ST", S("Pos", B("stVal", MmsKind.BIT_STRING, 2), q, t), S("Loc", B("stVal"), q, t)),
        S("CO", S("Pos", S("Oper", B("ctlVal"), B("ctlNum", MmsKind.UNSIGNED, 8)))),
        S("CF", S("Pos", B("ctlModel", MmsKind.INTEGER, 8), B("sboTimeout", MmsKind.UNSIGNED, 32))),
    )
    ggio = S(
        "GGIO1",
        S("MX", S("AnIn1", S("mag", B("f", MmsKind.FLOAT, 32)), q, t)),
        S("SP", S("Setp1", B("setVal", MmsKind.INTEGER, 32))),
    )
    lln0 = S(
        "LLN0",
        S("ST", S("Mod", B("stVal", MmsKind.INTEGER, 8), q, t)),
        S("RP", S("urcbA01", B("RptID", MmsKind.VISIBLE_STRING, -65))),
    )
    ld = LogicalDeviceInfo("CTRL", datasets=["LLN0$Events"])
    for spec in (lln0, cswi, ggio):
        ld.lns[spec.name] = build_ln("CTRL", spec.name, spec)
    return DeviceModel(lds={"CTRL": ld})
