"""Structured results (ARC-2) and the versioned JSON envelope (ARC-3).

Every check produces a :class:`CheckResult` with a status, a category (Configuration or
Communication, never merged — VER-7), evidence, and the raw MMS exchanges behind it.
"""

from __future__ import annotations

import datetime as _dt
import getpass
import socket
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from mms_client import __version__
from mms_client.codes import ErrorInfo

SCHEMA_VERSION = "1.0"


class Status(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    WARN = "warn"
    INFO = "info"
    NOT_RUN = "not-run"


# Severity order used to summarise a category (worst wins).
_SEVERITY = {Status.FAIL: 4, Status.WARN: 3, Status.NOT_RUN: 2, Status.INFO: 1, Status.PASS: 0}


class Category(StrEnum):
    CONFIGURATION = "Configuration"
    COMMUNICATION = "Communication"


@dataclass(slots=True)
class RawExchange:
    """One MMS/ACSI service call behind a result (summarised, CLI-7)."""

    service: str
    target: str | None = None
    request: Any = None
    response: Any = None
    error: ErrorInfo | None = None
    duration_s: float | None = None
    at: float = field(default_factory=time.time)

    def to_json(self) -> dict:
        return {
            "service": self.service,
            "target": self.target,
            "request": self.request,
            "response": self.response,
            "error": self.error.to_json() if self.error else None,
            "duration_s": round(self.duration_s, 6) if self.duration_s is not None else None,
            "at": self.at,
        }


@dataclass(slots=True)
class CheckResult:
    """Outcome of one check (ARC-2)."""

    id: str
    title: str
    status: Status
    category: Category
    subject: str | None = None
    message: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    raw: list[RawExchange] = field(default_factory=list)
    error: ErrorInfo | None = None
    hint: Any = None  # mms_client.explain.Hint, filled in by the caller that owns the catalogue
    reason: str | None = None  # why a check was not run
    certainty: str | None = None  # "fact" | "likely" | "check" when the message states a diagnosis

    def to_json(self) -> dict:
        hint = self.hint.to_json() if hasattr(self.hint, "to_json") else self.hint
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status.value,
            "category": self.category.value,
            "subject": self.subject,
            "message": self.message,
            "evidence": _jsonable(self.evidence),
            "raw": [r.to_json() for r in self.raw],
            "error": self.error.to_json() if self.error else None,
            "hint": hint,
            "reason": self.reason,
            "certainty": self.certainty,
        }


def worst(statuses: list[Status]) -> Status:
    if not statuses:
        return Status.NOT_RUN
    return max(statuses, key=lambda s: _SEVERITY[s])


def category_status(results: list[CheckResult], category: Category) -> Status:
    """Summary status of one category; pass only if nothing failed or warned and something ran."""
    sts = [r.status for r in results if r.category is category]
    if not sts:
        return Status.NOT_RUN
    if all(s in (Status.NOT_RUN,) for s in sts):
        return Status.NOT_RUN
    w = worst([s for s in sts if s is not Status.NOT_RUN])
    return Status.PASS if w in (Status.PASS, Status.INFO) else w


@dataclass(slots=True)
class CheckReport:
    """Results of a verification run. Configuration and Communication stay separate (VER-7)."""

    device: str
    reference: dict[str, Any] | None
    results: list[CheckResult] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None

    def add(self, r: CheckResult) -> CheckResult:
        self.results.append(r)
        return r

    def summary(self) -> dict[str, str]:
        cfg = category_status(self.results, Category.CONFIGURATION)
        com = category_status(self.results, Category.COMMUNICATION)
        return {
            Category.CONFIGURATION.value: cfg.value,
            Category.COMMUNICATION.value: com.value,
            # A device passes only if both categories pass; there is no combined status.
            "device_passes": "yes" if (cfg is Status.PASS and com is Status.PASS) else "no",
        }

    def to_json(self) -> dict:
        return {
            "device": self.device,
            "reference": self.reference,
            "summary": self.summary(),
            "counts": {s.value: sum(1 for r in self.results if r.status is s) for s in Status},
            "results": [r.to_json() for r in self.results],
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


def _jsonable(x: Any) -> Any:
    from mms_client.adapter.types import value_to_json

    if isinstance(x, dict):
        return {str(k): _jsonable(v) for k, v in x.items()}
    if isinstance(x, list | tuple | set):
        return [_jsonable(v) for v in x]
    if hasattr(x, "to_json"):
        return x.to_json()
    if isinstance(x, StrEnum):
        return x.value
    return value_to_json(x)


def jsonable(x: Any) -> Any:
    """Convert results, values and dataclasses with ``to_json`` into JSON-compatible data."""
    return _jsonable(x)


def envelope(command: str, result: Any, *, device: str | None = None, ok: bool = True, **extra: Any) -> dict:
    """Versioned JSON wrapper for every command's output (ARC-3)."""
    from mms_client.adapter import libiec61850_version, pyiec61850_version

    env = {
        "schema_version": SCHEMA_VERSION,
        "tool": {
            "name": "mms-client",
            "version": __version__,
            "pyiec61850_ng": pyiec61850_version(),
            "libiec61850": libiec61850_version(),
        },
        "generated_at": _dt.datetime.now(_dt.UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "command": command,
        "device": device,
        "ok": ok,
        "result": _jsonable(result),
    }
    env.update({k: _jsonable(v) for k, v in extra.items()})
    return env


def default_or_ident() -> str:
    """ORG-3 default originator identification: ``mms-client/<user>@<host>``."""
    try:
        user = getpass.getuser()
    except Exception:
        user = "unknown"
    return f"mms-client/{user}@{socket.gethostname()}"
