"""Structural rules from the specs that can be checked statically (ARC-1, ARC-4/PLT-4, ARC-5)."""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"
CLIENT = SRC / "ied_client"
MMS = SRC / "mms_protocol"


def _module_name(path: Path) -> str:
    parts = list(path.relative_to(SRC).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _imports(path: Path) -> set[str]:
    """Absolute names of everything ``path`` imports (relative imports resolved)."""
    tree = ast.parse(path.read_text(), filename=str(path))
    mod = _module_name(path)
    package = mod if path.name == "__init__.py" else mod.rpartition(".")[0]
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package.split(".")
                base = base[: len(base) - (node.level - 1)]
                prefix = ".".join(base)
                full = f"{prefix}.{node.module}" if node.module else prefix
            else:
                full = node.module or ""
            out.add(full)
            out.update(f"{full}.{a.name}" for a in node.names)
    return out


def _dynamic_imports(path: Path) -> set[str]:
    """Module names given as string constants to ``importlib.import_module`` / ``__import__``."""
    out: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
        if isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant):
            fn = ast.unparse(node.func)
            if fn.endswith("import_module") or fn == "__import__":
                out.add(str(node.args[0].value))
    return out


def _sources(root: Path) -> list[Path]:
    return sorted(root.rglob("*.py"))


def _is(module: str, name: str) -> bool:
    return module == name or module.startswith(name + ".")


def test_only_the_mms_adapter_imports_pyiec61850():
    """PLT-4: all access to pyiec61850-ng goes through the MMS module's adapter layer."""
    offenders = []
    for path in _sources(CLIENT) + _sources(MMS):
        if path.is_relative_to(MMS / "adapter"):
            continue
        if any(_is(m, "pyiec61850") for m in _imports(path) | _dynamic_imports(path)):
            offenders.append(str(path.relative_to(SRC)))
    assert offenders == []


def test_native_module_private_to_the_adapter():
    offenders = [
        str(path.relative_to(SRC))
        for path in _sources(CLIENT) + _sources(MMS)
        if not path.is_relative_to(MMS / "adapter") and any(_is(m, "mms_protocol.adapter._native") for m in _imports(path))
    ]
    assert offenders == []


def test_the_client_never_imports_a_protocol_module():
    """ARC-5: the client reaches protocol modules only through the ``ied_client.protocols`` entry points."""
    offenders = []
    for path in _sources(CLIENT):
        if any(_is(m, "mms_protocol") for m in _imports(path) | _dynamic_imports(path)):
            offenders.append(str(path.relative_to(SRC)))
    assert offenders == []


def test_the_protocol_contract_depends_only_on_the_client_basics():
    """The contract a protocol module implements (``ied_client.protocol``) stays small: it may use the codes
    and the reference type, but not the core services, the CLI or verification."""
    allowed = ("ied_client.protocol", "ied_client.codes", "ied_client.core.refs")
    offenders = []
    for path in _sources(CLIENT / "protocol"):
        tree = ast.parse(path.read_text())
        # imports needed only for type annotations are not a runtime dependency
        type_only = {
            id(n) for node in ast.walk(tree) if isinstance(node, ast.If) and "TYPE_CHECKING" in ast.unparse(node.test)
            for n in ast.walk(node)
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and not node.level and id(node) not in type_only:
                m = node.module or ""
                if _is(m, "ied_client") and not any(_is(m, a) for a in allowed):
                    offenders.append(f"{path.relative_to(SRC)}: {m}")
    assert offenders == []


def test_cli_does_not_touch_adapter_internals():
    """ARC-1: the CLI is a thin layer over the core library."""
    for path in _sources(CLIENT / "cli"):
        for m in _imports(path):
            assert not _is(m, "mms_protocol"), path
