from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def pytest_configure(config):
    # Tests never see this machine's local field data (FLD-1: /etc/ied-client, ~/.config/ied-client);
    # the ones about local data point IED_CLIENT_LOCAL_DIRS at their own directories.
    os.environ["IED_CLIENT_LOCAL_DIRS"] = tempfile.mkdtemp(prefix="ied-client-test-local-")


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    return FIXTURES


@pytest.fixture(scope="module")
def sim():
    """The basic simulated IED (tests/fixtures/sim/basic.cfg) in a subprocess."""
    from tests.sim.fixture import SimProcess

    with SimProcess(
        config=FIXTURES / "sim" / "basic.cfg",
        options={
            "files": str(FIXTURES / "sim" / "files"),
            "measurements": ["SIMCTRL/GGIO1.AnIn1.mag.f"],
            "measurement_period_ms": 50,
            "deny_write": ["SIMCTRL/GGIO1.Cfg1.dcText"],
            "ignore_write": ["SIMCTRL/GGIO1.Setp1.setMag"],
            "identity": ["SimVendor", "SimIED", "1.0"],
        },
    ) as p:
        yield p
