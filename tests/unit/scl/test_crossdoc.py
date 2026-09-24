"""ClientLN resolution, client relationships and role hints."""

from __future__ import annotations

import pytest

from mms_client.scl import (
    SclDocument,
    SclError,
    client_relationships,
    expand_server,
    resolve_client_ln,
    role_hint,
)


def test_resolve_client_in_scd(rack_doc: SclDocument) -> None:
    bcu = expand_server(rack_doc, "BCU1")
    rcb = bcu.rcb("brcbStatus02")
    res = resolve_client_ln(rack_doc, rcb.clients[0])
    assert res.resolved
    assert (res.ied_name, res.ied.name, res.access_point.name) == ("SM1", "SM1", "S1")
    assert res.ip == "10.0.0.2"
    assert res.problems == ()


def test_resolve_client_without_ap_ref(rack_doc: SclDocument) -> None:
    ied3 = expand_server(rack_doc, "IED3")
    (client,) = ied3.rcb("urcbA01").clients
    assert client.ap_ref is None
    res = resolve_client_ln(rack_doc, client)
    assert res.resolved and res.access_point.name == "S1" and res.ip == "10.0.0.2"


def test_client_not_in_cid(bcu_doc: SclDocument) -> None:
    rcb = expand_server(bcu_doc, "BCU1").rcb("brcbStatus02")
    res = resolve_client_ln(bcu_doc, rcb.clients[0])
    assert not res.resolved and res.ied is None and res.ip is None
    assert "client IED 'GW1' is not in" in res.problems[0]


def test_client_ln_missing_on_client(rack_doc: SclDocument) -> None:
    bcu = expand_server(rack_doc, "BCU1")
    client = bcu.rcb("brcbStatus01").clients[0]
    wrong = type(client)(ied_name="SM1", ln_class="IHMI", ld_inst="LD0", ln_inst="7", ap_ref="S1")
    res = resolve_client_ln(rack_doc, wrong)
    assert res.ied is not None and not res.ln_found and not res.resolved
    assert res.ip == "10.0.0.2"  # still knows where the client IED is
    assert any("SM1/S1:LD0/IHMI7 not found" in p for p in res.problems)


def test_client_relationships(rack_doc: SclDocument) -> None:
    rels = client_relationships(rack_doc)
    assert [(r.client_ied, r.server_ied) for r in rels] == [("SM1", "BCU1"), ("SM1", "IED3")]
    to_bcu = rels[0]
    assert (to_bcu.client_ip, to_bcu.server_ip, to_bcu.server_ap) == ("10.0.0.2", "10.0.0.11", "S1")
    assert to_bcu.client_lns == ["SM1/S1:LD0/IHMI1", "SM1/S1:LD0/ITCI1"]
    assert [r.name for r in to_bcu.rcbs] == ["brcbStatus01", "brcbStatus02", "urcbMeas", "brcbProt01"]
    assert to_bcu.client_in_document
    assert [r.name for r in rels[1].rcbs] == ["urcbA01"]


def test_client_relationships_in_cid(bcu_doc: SclDocument) -> None:
    rels = client_relationships(bcu_doc)
    assert [(r.client_ied, r.client_in_document, r.client_ip) for r in rels] == [
        ("SM1", False, None), ("GW1", False, None)]


def test_role_hints(rack_doc: SclDocument, ed1_doc: SclDocument) -> None:
    assert role_hint(rack_doc, "SM1")[0] == "station-manager"
    assert role_hint(rack_doc, "BCU1") == ("bcu", "has bay control logical nodes CSWI")
    assert role_hint(rack_doc, "IED3")[0] == "ied"
    assert role_hint(ed1_doc, "TEMPLATE")[0] == "ied"
    with pytest.raises(SclError):
        role_hint(rack_doc, "X")
