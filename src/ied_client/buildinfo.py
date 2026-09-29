"""How this copy of the tool was installed (PLT-5).

The offline package (``packaging/build-deb.sh``) writes ``BUILD-INFO.json`` next to its private
Python, i.e. ``/opt/ied-client/BUILD-INFO.json`` beside ``/opt/ied-client/python``. A development
checkout (uv venv) has no such file.
"""

from __future__ import annotations

import functools
import json
import platform
import sys
from pathlib import Path
from typing import Any

BUILD_INFO_KIND = "ied-client-build"
BUILD_INFO_FILE = "BUILD-INFO.json"


def build_info_path() -> Path:
    # base_prefix, not prefix: a venv made from the bundled Python (the package tests) still finds it.
    return Path(sys.base_prefix).parent / BUILD_INFO_FILE


@functools.cache
def build_info() -> dict[str, Any] | None:
    """The package's build record, or None when running from a development checkout."""
    try:
        data = json.loads(build_info_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("kind") == BUILD_INFO_KIND else None


def describe() -> str:
    """One line: the package build (or "development checkout") and the Python version."""
    info = build_info()
    py = f"Python {platform.python_version()}"
    if info is None:
        return f"development checkout; {py}"
    parts = [f"package {info.get('package_version', '?')}"]
    if info.get("git_commit"):
        commit = f"commit {str(info['git_commit'])[:12]}"
        if info.get("git_dirty"):
            commit += "+local-changes"
        if info.get("source_date"):
            commit += f" ({info['source_date']})"
        parts.append(commit)
    return ", ".join(parts) + f"; {py}"
