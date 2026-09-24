"""Structural rules from SPEC.md that can be checked statically."""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "mms_client"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
    return out


def test_only_adapter_imports_pyiec61850():
    """PLT-4: all access to pyiec61850-ng goes through the adapter layer."""
    offenders = []
    for path in SRC.rglob("*.py"):
        rel = path.relative_to(SRC)
        if rel.parts[0] == "adapter":
            continue
        if any(m == "pyiec61850" or m.startswith("pyiec61850.") for m in _imports(path)):
            offenders.append(str(rel))
        if "pyiec61850" in path.read_text() and "import" in path.read_text():
            src = path.read_text()
            if "importlib" in src and "pyiec61850" in src:
                offenders.append(str(rel))
    assert offenders == []


def test_native_module_private_to_adapter():
    offenders = []
    for path in SRC.rglob("*.py"):
        rel = path.relative_to(SRC)
        if rel.parts[0] == "adapter":
            continue
        if any("adapter._native" in m or m.endswith("._native") for m in _imports(path)):
            offenders.append(str(rel))
    assert offenders == []


def test_cli_does_not_touch_adapter_internals():
    """ARC-1: the CLI is a thin layer over the core library."""
    cli = SRC / "cli"
    if not cli.exists():
        return
    for path in cli.rglob("*.py"):
        for m in _imports(path):
            assert not m.startswith("mms_client.adapter.client"), path
