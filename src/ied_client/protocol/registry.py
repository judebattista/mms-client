"""Finding protocol modules.

Modules are found through the entry-point group ``ied_client.protocols`` (name → the module's
:class:`~ied_client.protocol.api.ProtocolModule` object), or registered in code with
:func:`register` (tests, embedding). The client never imports a protocol module by name.
"""

from __future__ import annotations

import importlib.metadata
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .api import ProtocolModule

GROUP = "ied_client.protocols"
# The protocol of a device whose inventory entry (or command line) names none (INV-6).
DEFAULT_PROTOCOL = "mms"

_modules: dict[str, ProtocolModule] = {}


class UnknownProtocolError(LookupError):
    def __init__(self, name: str) -> None:
        self.name = name
        known = ", ".join(available()) or "none installed"
        super().__init__(f"unknown protocol {name!r} (available: {known})")


def _entry_points() -> dict[str, importlib.metadata.EntryPoint]:
    return {ep.name: ep for ep in importlib.metadata.entry_points(group=GROUP)}


def register(module: ProtocolModule) -> None:
    """Make ``module`` available under ``module.name`` (replacing an installed module of that name)."""
    _modules[module.name] = module


def unregister(name: str) -> None:
    _modules.pop(name, None)


def available() -> list[str]:
    return sorted(set(_entry_points()) | set(_modules))


def get(name: str | None = None) -> ProtocolModule:
    """The protocol module called ``name`` (default: :data:`DEFAULT_PROTOCOL`)."""
    name = name or DEFAULT_PROTOCOL
    module = _modules.get(name)
    if module is None:
        ep = _entry_points().get(name)
        if ep is None:
            raise UnknownProtocolError(name)
        module = ep.load()
        _modules[name] = module
    return module


def load_all() -> list[ProtocolModule]:
    """Every available protocol module (importing one registers its code domains)."""
    return [get(n) for n in available()]
