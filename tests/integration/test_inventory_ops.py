"""inventory validate / from-scd against the simulator (INV-4)."""

from __future__ import annotations

import pytest

from mms_client.inventory import Device, IdentityOverride, Inventory, Reference
from mms_client.inventory_ops import from_scd, validate, write_back_identity
from tests.conftest import FIXTURES

pytestmark = pytest.mark.integration


def test_validate_against_simulator(sim, tmp_path):
    inv = Inventory(devices=[
        Device("sim", "127.0.0.1", sim.port, "ied", identity=IdentityOverride(vendor="OtherVendor")),
        Device("gone", "127.0.0.1", 1, "ied"),
        Device("withref", "127.0.0.1", sim.port, "bcu", reference=Reference("cid", tmp_path / "missing.cid")),
    ])
    items = {(i.subject, i.check): i for i in validate(inv, timeout_s=1.0)}
    assert items[("sim", "tcp")].status == "pass"
    assert items[("sim", "associate")].status == "pass"
    assert items[("sim", "identity-vendor")].status == "warn"
    assert items[("gone", "tcp")].status == "fail"
    assert items[("withref", "reference-file")].status == "fail"


def test_from_scd_and_write_back(tmp_path):
    inv = from_scd(FIXTURES / "scl" / "rack_mixed.scd", out_path=tmp_path / "exp.yaml")
    inv.save()
    text = (tmp_path / "exp.yaml").read_text()
    assert "scd: " in text and "rack_mixed.scd" in text
    from mms_client.inventory import load_inventory

    again = load_inventory(tmp_path / "exp.yaml")
    assert again.device("bcu1").reference.path.exists()
    assert {c.name for c in again.clients} == {"sm1-to-bcu1", "sm1-to-ied3"}

    class A:
        def __init__(self, v):
            self.value = v

    class Ident:
        vendor, model, firmware = A("X"), A("Y"), A("1.0")

    dev = write_back_identity(again, "ied3", Ident(), edition="Ed1")
    assert dev.identity.edition == "Ed1" and dev.identity.firmware == "1.0" and dev.identity.vendor == "Oldco"
