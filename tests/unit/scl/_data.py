"""Paths to the SCL fixtures (shared by the tests in this package)."""

from __future__ import annotations

from pathlib import Path

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "scl"
BCU_ED2 = FIXTURES / "bcu_ed2.cid"
RACK_MIXED = FIXTURES / "rack_mixed.scd"
IED_ED1 = FIXTURES / "ied_ed1.icd"
BROKEN_REFS = FIXTURES / "broken_refs.cid"


def bcu_text() -> str:
    """The BCU fixture as text, for tests that derive variants from it."""
    return BCU_ED2.read_text(encoding="utf-8")
