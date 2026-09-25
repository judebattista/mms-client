"""The client runs with any protocol module plugged in (IED-CLIENT-SPEC.md §4.1).

``FakeProtocol`` is a complete, in-memory protocol module with its own naming (``LD|LN|FC|DO|DA``), its
own code domain and catalogue file. Registered in the protocol registry, it drives the unchanged client:
sessions, browsing, read/write, datasets, identity, snapshots, diagnose, the CLI and the explanation
catalogue. Nothing here touches MMS.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from typing import IO, Any

import pytest

from ied_client import codes
from ied_client.cli.main import main
from ied_client.codes import ErrorInfo
from ied_client.core import datasets, readwrite
from ied_client.core.identity import identify
from ied_client.core.refs import ObjectRef, RefError
from ied_client.core.results import Status
from ied_client.core.safety import Policy
from ied_client.core.session import Session, resolve_target
from ied_client.diagnosis.diagnose import LayerOutcome, diagnose
from ied_client.explain import Catalogue
from ied_client.inventory import parse_inventory
from ied_client.protocol import (
    AccessError,
    Association,
    ConnectError,
    DataSetMember,
    DatasetRef,
    ProtocolModule,
    ServerIdentity,
    ServiceError,
    UnknownProtocolError,
    ValueKind,
    VarSpec,
    registry,
)
from ied_client.protocol.values import coerce
from ied_client.verify.snapshot import capture

DOMAIN = "fake"
FAKE_ERRORS = {0: "ok", 1: "denied", 2: "no-such-object"}

S = ValueKind.STRUCTURE


def _struct(name: str | None, *children: VarSpec) -> VarSpec:
    return VarSpec(S, name, len(children), tuple(children))


GGIO1 = _struct(
    "GGIO1",
    _struct("ST", _struct("Ind1", VarSpec(ValueKind.BOOLEAN, "stVal"), VarSpec(ValueKind.BIT_STRING, "q", -13))),
    _struct("SP", _struct("Setp", VarSpec(ValueKind.INTEGER, "setVal", 32))),
)
LLN0 = _struct("LLN0", _struct("ST", _struct("Mod", VarSpec(ValueKind.INTEGER, "stVal", 8))))


class FakeNaming:
    key = "fake"
    label = "fake name"

    def looks_native(self, text: str) -> bool:
        return "|" in text

    def data(self, ref: ObjectRef) -> str:
        return "|".join(x for x in (ref.ld, ref.ln, ref.fc, *ref.path) if x)

    def parse_data(self, text: str, default_ld: str | None = None) -> ObjectRef:
        parts = text.split("|")
        if len(parts) < 4:
            raise RefError(f"{text!r}: expected LD|LN|FC|DO...")
        return ObjectRef(parts[0], parts[1], tuple(parts[3:]), parts[2])

    def dataset(self, ds: DatasetRef) -> str:
        return f"{ds.ld}|{ds.ln}|{ds.name}"

    def parse_dataset(self, text: str, default_ld: str | None = None) -> DatasetRef:
        ld, ln, name = text.split("|")
        return DatasetRef(ld, ln, name)

    def control_block(self, ld: str, ln: str, fc: str, name: str) -> str:
        return f"{ld}|{ln}|{fc}|{name}"

    def control_block_to_iec(self, text: str) -> str:
        return text.replace("|", ".")


def _err(name: str) -> ErrorInfo:
    return codes.info(DOMAIN, {v: k for k, v in FAKE_ERRORS.items()}[name])


class FakeAssociation:
    """An in-memory device: LD1 with LLN0 and GGIO1, one dataset."""

    def __init__(self, host: str, port: int) -> None:
        self.host, self.port, self.local_ip = host, port, None
        self.values: dict[tuple[str, str, str, tuple[str, ...]], Any] = {
            ("LD1", "GGIO1", "ST", ("Ind1", "stVal")): True,
            ("LD1", "GGIO1", "SP", ("Setp", "setVal")): 7,
            ("LD1", "LLN0", "ST", ("Mod", "stVal")): 1,
        }
        self.open = True
        self.writes: list[tuple[str, Any]] = []

    @property
    def is_connected(self) -> bool:
        return self.open

    def close(self, *, graceful: bool = True) -> None:
        self.open = False

    def release(self) -> ErrorInfo:
        self.open = False
        return _err("ok")

    def abort(self) -> ErrorInfo:
        return self.release()

    def add_closed_listener(self, cb: Callable[[], None]) -> None:
        pass

    def identify(self) -> ServerIdentity:
        return ServerIdentity("FakeVendor", "FakeModel", "1.0")

    def association_info(self) -> dict[str, Any]:
        return {"link": "loopback", "frame_size": 1024}

    def logical_devices(self) -> list[str]:
        return ["LD1"]

    def logical_nodes(self, ld: str) -> list[str]:
        return ["LLN0", "GGIO1"]

    def logical_node_spec(self, ld: str, ln: str) -> VarSpec:
        return {"LLN0": LLN0, "GGIO1": GGIO1}[ln]

    def datasets(self, ld: str) -> list[DatasetRef]:
        return [DatasetRef(ld, "LLN0", "Events")]

    def dataset_directory(self, ds: DatasetRef) -> tuple[list[DataSetMember], bool]:
        ref = ObjectRef("LD1", "GGIO1", ("Ind1",), "ST")
        return [DataSetMember(FakeNaming().data(ref), ref), DataSetMember("LD1|???", None, "unparseable")], False

    def _value(self, ref: ObjectRef) -> Any:
        spec = self.logical_node_spec(ref.ld, ref.ln or "").child(ref.fc or "")
        spec = spec.find(ref.path) if spec is not None else None
        if spec is None:
            return AccessError(_err("no-such-object"))
        if spec.is_basic:
            return self.values.get((ref.ld, ref.ln, ref.fc, ref.path), 0)
        return {c.name: self._value(ref.child(c.name or "")) for c in spec.children}

    def read(self, ref: ObjectRef, spec: VarSpec | None = None) -> Any:
        return self._value(ref)

    def read_many(self, refs: Sequence[ObjectRef], specs: Sequence[VarSpec | None] | None = None) -> list[Any]:
        return [self._value(r) for r in refs]

    def write(self, ref: ObjectRef, spec: VarSpec, value: Any) -> None:
        if ref.fc == "ST":
            raise ServiceError("write", FakeNaming().data(ref), _err("denied"))
        self.values[(ref.ld, ref.ln, ref.fc, ref.path)] = coerce(spec, value)
        self.writes.append((FakeNaming().data(ref), value))

    def _unsupported(self, service: str) -> ServiceError:
        return ServiceError(service, None, codes.tool("service-not-supported-by-protocol"))

    def get_rcb(self, reference: str):
        raise self._unsupported("get-rcb")

    def set_rcb(self, reference: str, changes: dict[str, object], *, single_request: bool = True) -> None:
        raise self._unsupported("set-rcb")

    def install_report_handler(self, reference, rpt_id, callback, member_specs=None) -> None:
        raise self._unsupported("subscribe")

    def uninstall_report_handler(self, reference: str) -> None:
        pass

    def file_directory(self, directory: str = "") -> list:
        return []

    def get_file(self, remote: str, sink: IO[bytes], *, progress=None) -> int:
        raise self._unsupported("file-get")

    def control(self, ref: ObjectRef):
        raise self._unsupported("control-create")


class FakeConnectionDiagnosis:
    def run(self, session: Session, report, *, timeout_s: float, icmp: bool) -> bool:
        session.connect()
        report.layers.append(LayerOutcome("fake-link", Status.PASS, summary="loopback link up"))
        return True

    def after_identity(self, session: Session, report, layer: LayerOutcome) -> None:
        pass


FAKE_HINTS = """\
version: 1
entries:
- id: fake.denied
  keys: [fake:denied]
  title: Refused by the fake device
  summary: The fake device refuses writes to status values.
  hint: "Status values of the fake device are read-only"
  certainty: fact
"""


class FakeProtocol:
    name = "fake"
    display_name = "FAKE"
    default_port = 9999
    names = FakeNaming()
    identify_source = "fake-identify"
    blind_spots = "The fake protocol sees only its own loopback device."
    sbo_normal_select_note = ""
    association_layers = ("fake-link",)
    next_steps = {"fake-link": ["Check the fake link."]}

    def __init__(self) -> None:
        self.opened: list[FakeAssociation] = []

    def open(self, host: str, port: int, *, local_ip=None, connect_timeout_ms=5000, request_timeout_ms=10000) -> FakeAssociation:
        if host == "unreachable":
            raise ConnectError("associate", host, _err("denied"))
        a = FakeAssociation(host, port)
        self.opened.append(a)
        return a

    def versions(self) -> dict[str, str]:
        return {"fakelib": "9.9"}

    def describe_association(self, info: dict[str, Any]) -> list[tuple[str, Any]]:
        return [("link", info["link"]), ("frame size", info["frame_size"])]

    def connection_diagnosis(self) -> FakeConnectionDiagnosis:
        return FakeConnectionDiagnosis()

    def classify_failed_association(self, host, port, tcp, timeout_s, local_ip):
        return None

    def catalogue_sources(self) -> list[tuple[str, str]]:
        return [("fake/hints.yaml", FAKE_HINTS)]

    def quirks_sources(self) -> list[tuple[str, str]]:
        return []


@pytest.fixture
def fake():
    codes.register_domain(DOMAIN, FAKE_ERRORS, preference=15)
    module = FakeProtocol()
    registry.register(module)
    try:
        yield module
    finally:
        registry.unregister("fake")
        codes._DOMAINS.pop(DOMAIN, None)  # keep the global registry as other tests expect it


def _session(fake: FakeProtocol) -> Session:
    s = Session(resolve_target("10.1.1.1", None, protocol="fake"), policy=Policy(yes=True))
    s.connect()
    return s


def test_fake_module_satisfies_the_contract(fake):
    assert isinstance(fake, ProtocolModule)
    assert isinstance(fake.open("h", 1), Association)
    assert "fake" in registry.available() and registry.get("fake") is fake


def test_target_takes_the_protocols_default_port(fake):
    t = resolve_target("10.1.1.1", None, protocol="fake")
    assert (t.protocol, t.port) == ("fake", 9999)
    inv = parse_inventory({"devices": [{"name": "f1", "ip": "10.1.1.2", "protocol": "fake"}]})
    dev = inv.devices[0]
    assert (dev.protocol, dev.port) == ("fake", 9999)
    assert dev.to_yaml(None)["protocol"] == "fake"
    t = resolve_target("f1", inv)
    assert (t.host, t.port, t.protocol) == ("10.1.1.2", 9999, "fake")


def test_session_browses_reads_and_writes_through_the_module(fake):
    s = _session(fake)
    try:
        model = s.model()
        assert list(model.lds["LD1"].lns) == ["LLN0", "GGIO1"]
        assert model.lds["LD1"].datasets == [DatasetRef("LD1", "LLN0", "Events")]
        r = readwrite.read(s, "LD1/GGIO1.Ind1.stVal")
        assert (r.value, r.ref.fc, r.to_json()["fake"]) == (True, "ST", "LD1|GGIO1|ST|Ind1|stVal")
        # the protocol's own names are accepted as input
        assert readwrite.read(s, "LD1|GGIO1|SP|Setp|setVal").value == 7
        w = readwrite.write(s, "LD1/GGIO1.Setp.setVal", "12")
        assert (w.ok, w.applied, w.after) == (True, True, 12)
        assert fake.opened[-1].writes == [("LD1|GGIO1|SP|Setp|setVal", 12)]
        # a refusal carries the module's own code
        s.policy.mode = s.policy.mode.EXPERT
        w = readwrite.write(s, "LD1/GGIO1.Ind1.stVal", "false")
        assert not w.ok and w.error.key == "fake:denied"
    finally:
        s.close()


def test_datasets_identity_and_snapshot(fake):
    s = _session(fake)
    try:
        info = datasets.dataset_members(s, DatasetRef("LD1", "LLN0", "Events"))
        assert info.reference == "LD1|LLN0|Events"
        assert [(m.reference, m.resolves) for m in info.members] == [("LD1/GGIO1.Ind1", True), (None, False)]
        ident = identify(s, log=False)
        assert (ident.vendor.value, ident.vendor.source) == ("FakeVendor", "FAKE Identify")
        snap = capture(s, delay_s=0)
        assert snap.structure["datasets"] == {"LD1|LLN0|Events": ["LD1|GGIO1|ST|Ind1", "LD1|???"]}
        assert snap.configuration["LD1/GGIO1.Setp.setVal[SP]"] == 7
        assert snap.configuration["identity:fake-identify.vendor"] == "FakeVendor"
        assert snap.metadata["tool"]["fakelib"] == "9.9"
    finally:
        s.close()


def test_diagnose_runs_the_modules_layers(fake):
    s = Session(resolve_target("10.1.1.1", None, protocol="fake"))
    try:
        rep = diagnose(s, timeout_s=0.1, icmp=False)
    finally:
        s.close()
    assert [lay.layer for lay in rep.layers][:3] == ["fake-link", "identity", "model"]
    assert rep.stopped_at is None and rep.verdict.text.startswith("No FAKE problem found")
    assert rep.notes == ["The fake protocol sees only its own loopback device."]


def test_cli_one_shot_uses_the_module(fake, capsys, tmp_path):
    code = main(["--protocol", "fake", "--json", "--no-log", "read", "10.1.1.1", "LD1/GGIO1.Ind1.stVal"])
    env = json.loads(capsys.readouterr().out)
    assert code == 0 and env["ok"]
    assert env["tool"]["fakelib"] == "9.9" and "libiec61850" not in env["tool"]
    assert env["result"]["fake"] == "LD1|GGIO1|ST|Ind1|stVal" and env["result"]["value"] is True
    code = main(["--protocol", "fake", "--json", "--no-log", "info", "10.1.1.1"])
    env = json.loads(capsys.readouterr().out)
    assert code == 0 and env["result"]["association"]["connection_params"] == {"link": "loopback", "frame_size": 1024}


def test_cli_rejects_an_unknown_protocol(capsys):
    code = main(["--protocol", "nope", "--json", "--no-log", "read", "10.1.1.1", "LD1/GGIO1.Ind1.stVal"])
    env = json.loads(capsys.readouterr().out)
    assert code == 2 and env["error"]["key"] == "tool:unknown-protocol"
    with pytest.raises(UnknownProtocolError):
        registry.get("nope")


def test_the_modules_catalogue_is_loaded(fake):
    cat = Catalogue.load()
    hint = cat.hint(codes.parse_key("fake:denied"))
    assert hint is not None and hint.text == "Status values of the fake device are read-only"
    assert cat.explain("fake:no-such-object") is None or cat.explain("fake:no-such-object").id == "code.unlisted"
