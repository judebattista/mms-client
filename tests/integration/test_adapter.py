"""Adapter against the simulated IED (real libiec61850 server in a subprocess)."""

from __future__ import annotations

import io
import threading
import time

import pytest

from mms_client.adapter import codec
from mms_client.adapter.client import IedClient
from mms_client.adapter.errors import ConnectError, EncodeError, ServiceError
from mms_client.adapter.types import AccessError, BitString, MmsKind, UtcTime, VarSpec
from tests.conftest import FIXTURES

pytestmark = pytest.mark.integration
LD = "SIMCTRL"
FIX = FIXTURES / "sim" / "basic.cfg"


@pytest.fixture
def client(sim):
    c = IedClient(request_timeout_ms=3000)
    c.connect("127.0.0.1", sim.port)
    yield c
    c.close()


def spec_of(c: IedClient, ln: str, *path: str) -> VarSpec:
    s = c.get_variable_spec(LD, ln).find(path)
    assert s is not None, path
    return s


def test_identity_and_params(client):
    ident = client.identify()
    assert (ident.vendor, ident.model, ident.revision) == ("SimVendor", "SimIED", "1.0")
    p = client.connection_params()
    assert p.max_pdu_size > 1000
    assert "read" in p.supported_services() and "fileDirectory" in p.supported_services()


def test_browse(client):
    assert client.get_domain_names() == [LD]
    names = client.get_domain_variable_names(LD)
    assert "GGIO1" in names and "LLN0" in names
    assert set(client.get_dataset_names(LD)) == {"LLN0$Events", "LLN0$Measurements"}
    members, deletable = client.get_dataset_directory(LD, "LLN0$Events")
    assert not deletable
    assert members[0].item == "GGIO1$ST$SPCSO1$stVal"
    spec = client.get_variable_spec(LD, "GGIO1")
    assert spec.kind is MmsKind.STRUCTURE
    oper = spec.find(["CO", "SPCSO1", "Oper"])
    assert [c.name for c in oper.children][:2] == ["ctlVal", "origin"]
    assert spec.find(["MX", "AnIn1", "mag", "f"]).type_name() == "float(32)"
    assert spec.find(["ST", "SPCSO1", "q"]).type_name() in ("bit-string(13)", "bit-string(<=13)")


def test_read_values_and_types(client):
    assert client.read(LD, "GGIO1$SP$Setp1$setVal") == 10
    q = client.read(LD, "GGIO1$ST$SPCSO1$q")
    assert isinstance(q, BitString) and q.size == 13
    t = client.read(LD, "GGIO1$ST$SPCSO1$t")
    assert isinstance(t, UtcTime)
    nam = client.read(LD, "LLN0$DC$NamPlt", spec_of(client, "LLN0", "DC", "NamPlt"))
    assert nam["configRev"] == "cfg-7"
    missing = client.read(LD, "GGIO1$ST$Nope$stVal")
    assert isinstance(missing, AccessError) and missing.error.name == "object-non-existent"
    multi = client.read_multiple(LD, ["GGIO1$SP$Setp1$setVal", "GGIO1$ST$Nope", "GGIO1$CF$Cfg1$cfgVal"])
    assert multi[0] == 10 and isinstance(multi[1], AccessError) and multi[2] == 5
    vals = client.read_dataset_values(LD, "LLN0$Events")
    assert len(vals) == 6


def test_write_paths(client):
    sp = spec_of(client, "GGIO1", "SP", "Setp1", "setVal")
    client.write(LD, "GGIO1$SP$Setp1$setVal", sp, 42)
    assert client.read(LD, "GGIO1$SP$Setp1$setVal") == 42
    client.write(LD, "GGIO1$SP$Setp1$setVal", sp, 10)
    # ST is not writable on this server: the device's refusal is reported as received (RW-6)
    st = spec_of(client, "GGIO1", "ST", "SPCSO1", "stVal")
    with pytest.raises(ServiceError) as ei:
        client.write(LD, "GGIO1$ST$SPCSO1$stVal", st, True)
    assert ei.value.error.key == "data-access:object-access-denied"
    # handler that refuses
    dc = spec_of(client, "GGIO1", "DC", "Cfg1", "dcText")
    with pytest.raises(ServiceError) as ei:
        client.write(LD, "GGIO1$DC$Cfg1$dcText", dc, "x")
    assert ei.value.error.name == "object-access-denied"
    # accepted but not applied (RW-5 is detected by the core via read-back)
    mag = spec_of(client, "GGIO1", "SP", "Setp1", "setMag")
    client.write(LD, "GGIO1$SP$Setp1$setMag", mag, 9.5)
    assert client.read(LD, "GGIO1$SP$Setp1$setMag") == 1.5
    # encode validation (RW-3)
    with pytest.raises(EncodeError):
        client.write(LD, "GGIO1$SP$Setp1$setVal", sp, "12")
    with pytest.raises(EncodeError):
        client.write(LD, "GGIO1$SP$Setp1$setVal", sp, 2**40)


def test_parse_text_uses_model_type():
    assert codec.parse_text(VarSpec(MmsKind.INTEGER, size=32), "0x10") == 16
    assert codec.parse_text(VarSpec(MmsKind.BOOLEAN), "on") is True
    assert codec.parse_text(VarSpec(MmsKind.FLOAT, size=32), "1.5") == 1.5
    with pytest.raises(EncodeError):
        codec.parse_text(VarSpec(MmsKind.INTEGER, size=8), "300")
    with pytest.raises(EncodeError):
        codec.parse_text(VarSpec(MmsKind.STRUCTURE, children=()), "1")


@pytest.mark.parametrize(
    ("obj", "model"),
    [("SPCSO1", 1), ("SPCSO2", 2), ("SPCSO3", 3), ("SPCSO4", 4), ("DPCSO1", 4), ("IntIn1", 3)],
)
def test_control_models(client, obj, model):
    ref = f"{LD}/GGIO1.{obj}"
    ctl = client.control(ref)
    assert ctl.ctl_model == model
    ctl.configure(or_ident="pytest", or_cat=2)
    val_spec = spec_of(client, "GGIO1", "CO", obj, "Oper", "ctlVal")
    value = 7 if obj == "IntIn1" else True
    if model == 2:
        assert ctl.select().ok
    if model == 4:
        assert ctl.select_with_value(val_spec, value).ok
    res = ctl.operate(val_spec, value)
    assert res.ok, res
    if model in (3, 4):
        term = ctl.wait_termination(3)
        assert term is not None and term.positive
    st = client.read(LD, f"GGIO1$ST${obj}$stVal")
    if obj == "DPCSO1":
        assert isinstance(st, BitString) and st.as_int_msb0() == 2  # on
    else:
        assert st == value
    assert client.read(LD, f"GGIO1$ST${obj}$origin$orCat") == 2


def test_control_rejected_with_add_cause(client):
    # GGIO2.Loc is true: station control is blocked by the switching hierarchy
    ctl = client.control(f"{LD}/GGIO2.SPCSO1")
    ctl.configure(or_ident="pytest", or_cat=2)
    spec = spec_of(client, "GGIO2", "CO", "SPCSO1", "Oper", "ctlVal")
    res = ctl.select_with_value(spec, True)
    assert not res.ok
    assert res.add_cause is not None and res.add_cause.name == "blocked-by-switching-hierarchy"
    assert res.ied_error.name in ("access-denied", "object-access-denied")
    ctl.configure(or_ident="pytest", or_cat=1)
    assert ctl.select_with_value(spec, True).ok
    assert ctl.cancel().ok


def test_reports_under_load_with_controls(client):
    """RSK-1 regression: blocking calls while reports and terminations arrive must not deadlock."""
    ref = f"{LD}/LLN0.RP.urcbEvents01"
    got: list = []
    ev = threading.Event()

    def on_report(r):
        got.append(r)
        ev.set()

    rcb = client.get_rcb(ref)
    client.install_report_handler(ref, rcb.rpt_id, on_report)
    client.set_rcb(ref, {"resv": True})
    client.set_rcb(ref, {"trg_ops": 1 | 16, "opt_flds": 0xFF, "rpt_ena": True})
    client.set_rcb(ref, {"gi": True})
    assert ev.wait(3), "no GI report"
    gi = got[0]
    assert "general-interrogation" in gi.entries[0].reasons
    assert len(gi.entries) == 6
    ctl = client.control(f"{LD}/GGIO1.SPCSO3")
    ctl.configure(or_ident="pytest", or_cat=2)
    spec = spec_of(client, "GGIO1", "CO", "SPCSO3", "Oper", "ctlVal")
    t0 = time.time()
    for i in range(40):
        assert ctl.operate(spec, bool(i % 2)).ok
        client.read(LD, "GGIO1$SP$Setp1$setVal")
    assert time.time() - t0 < 20
    deadline = time.time() + 3
    while time.time() < deadline and not any("data-change" in e.reasons for r in got for e in r.entries):
        time.sleep(0.05)
    assert any("data-change" in e.reasons for r in got for e in r.entries)
    seqs = [r.seq_num for r in got if r.seq_num is not None]
    assert seqs == sorted(seqs)
    client.set_rcb(ref, {"rpt_ena": False})
    client.set_rcb(ref, {"resv": False})
    client.uninstall_report_handler(ref)


def test_rcb_owner_reflects_bound_local_ip(sim):
    c = IedClient()
    c.connect("127.0.0.1", sim.port, local_ip="127.0.0.2")
    try:
        ref = f"{LD}/LLN0.RP.urcbEvents02"
        c.set_rcb(ref, {"resv": True})
        v = c.get_rcb(ref)
        assert v.resv is True
        assert v.owner is not None and v.owner[-4:] == bytes([127, 0, 0, 2])
        c.set_rcb(ref, {"resv": False})
        assert sim.wait_event(lambda e: e.get("event") == "connect" and e["peer"].startswith("127.0.0.2"))
    finally:
        c.close()


def test_file_services(client):
    names = {f.name for f in client.get_file_directory()}
    assert {"readme.txt", "COMTRADE_0001.dat"} <= names
    buf = io.BytesIO()
    n = client.get_file("COMTRADE_0001.dat", buf)
    assert n == 150000 == len(buf.getvalue())
    with pytest.raises(ServiceError):
        client.get_file("does-not-exist.bin", io.BytesIO())


def test_connect_refused():
    c = IedClient(connect_timeout_ms=1000)
    with pytest.raises(ConnectError) as ei:
        c.connect("127.0.0.1", 1)
    assert ei.value.error.domain.value == "ied"


def test_connection_loss_detected():
    from tests.sim.fixture import SimProcess

    with SimProcess(config=FIX) as p:
        c = IedClient(request_timeout_ms=2000)
        c.connect("127.0.0.1", p.port)
        assert c.is_connected
        p.kill()
        assert c.wait_closed(5)
        assert not c.is_connected
        with pytest.raises(ServiceError):
            c.read(LD, "GGIO1$SP$Setp1$setVal")
        c.close()

