"""An IEC 61850 IED client for verifying and troubleshooting IEDs in a test rack (IED-CLIENT-SPEC.md).

The protocol it speaks to a device comes from a protocol module (see :mod:`ied_client.protocol`).
"""

import os
from pathlib import Path


def _version() -> str:
    """The installed distribution's version (``pyproject.toml`` is the single source)."""
    try:
        from importlib.metadata import PackageNotFoundError, version

        return version("ied-client")
    except PackageNotFoundError:  # pragma: no cover - running from a bare source tree
        return "0+unknown"


__version__ = _version()

# The product's name: the command, the JSON envelope's tool name, the state directory, the default orIdent
# and the ``kind`` of the files it writes (snapshots, incidents).
TOOL_NAME = "ied-client"
ENV_PREFIX = "IED_CLIENT"  # environment variables: <ENV_PREFIX>_INVENTORY, <ENV_PREFIX>_LOG_DIR

# The name the tool had while MMS was its only protocol. Still accepted during the transition: the command,
# the environment variables, session logs in the old state directory and snapshots of the old kind.
LEGACY_TOOL_NAME = "mms-client"
LEGACY_ENV_PREFIX = "MMS_CLIENT"


def env(name: str) -> str | None:
    """The environment variable ``<ENV_PREFIX>_<name>``, else its old spelling ``MMS_CLIENT_<name>``."""
    return os.environ.get(f"{ENV_PREFIX}_{name}") or os.environ.get(f"{LEGACY_ENV_PREFIX}_{name}") or None


def state_dir(name: str = TOOL_NAME) -> Path:
    """The tool's state directory (session logs, shell history); ``LEGACY_TOOL_NAME`` gives the old one."""
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / name
