from __future__ import annotations

import pytest

from ied_client.scl import ExpectedServer, SclDocument, expand_server, load_scl

from ._data import BCU_ED2, BROKEN_REFS, IED_ED1, RACK_MIXED


@pytest.fixture(scope="session")
def bcu_doc() -> SclDocument:
    return load_scl(BCU_ED2)


@pytest.fixture(scope="session")
def bcu(bcu_doc: SclDocument) -> ExpectedServer:
    return expand_server(bcu_doc, "BCU1")


@pytest.fixture(scope="session")
def rack_doc() -> SclDocument:
    return load_scl(RACK_MIXED)


@pytest.fixture(scope="session")
def ed1_doc() -> SclDocument:
    return load_scl(IED_ED1)


@pytest.fixture(scope="session")
def ed1(ed1_doc: SclDocument) -> ExpectedServer:
    return expand_server(ed1_doc, "TEMPLATE")


@pytest.fixture(scope="session")
def broken_doc() -> SclDocument:
    return load_scl(BROKEN_REFS)
