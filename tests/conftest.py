from __future__ import annotations

from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


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
