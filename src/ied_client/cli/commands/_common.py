"""Small helpers shared by the command modules (presentation only; logic lives in the core)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ied_client.codes import ErrorInfo
from ied_client.core.controls import parse_or_cat
from ied_client.core.sessionlog import read_log

from ..context import CliContext
from ..errors import catalogue


def explanation(query: str) -> Any:
    """``Catalogue.explain(query)`` or None (also when the catalogue is unavailable)."""
    cat = catalogue()
    if cat is None or not query:
        return None
    try:
        return cat.explain(query)
    except Exception:  # pragma: no cover - never let an explanation break a command
        return None


def summary_of(query: str) -> dict[str, str] | None:
    exp = explanation(query)
    if exp is None:
        return None
    return {"id": exp.id, "title": exp.title, "summary": exp.summary}


def orcat_override(ctx: CliContext) -> int | None:
    v = ctx.line.orcat
    if v is None and not ctx.in_shell:
        v = ctx.options.orcat
    return parse_or_cat(str(v)) if v is not None else None


def step_error(step: Any) -> ErrorInfo:
    """The most specific code of a failed control step (delegates to the core)."""
    from ied_client.core.controls import step_error as core_step_error

    return core_step_error(step)


def session_logs(ctx: CliContext) -> list[Path]:
    """Session logs in the log directory, newest first."""
    d = ctx.log_dir
    if not d.is_dir():
        return []
    return sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)


def latest_log_for(ctx: CliContext, device: str | None, *, exclude: Path | None = None) -> Path | None:
    """The newest earlier session log of ``device`` (by the device name in its session-start)."""
    for p in session_logs(ctx):
        if exclude is not None and p.resolve() == exclude.resolve():
            continue
        try:
            with open(p, encoding="utf-8") as fh:
                first = fh.readline()
        except OSError:
            continue
        if device is None or f'"device":"{device}"' in first.replace(" ", ""):
            return p
    return None


def last_error_in(entries: list[dict[str, Any]]) -> dict[str, Any] | None:
    for e in reversed(entries):
        if e.get("kind") == "error" and isinstance(e.get("error"), dict):
            return e
    return None


def load_log(path: Path) -> list[dict[str, Any]]:
    return read_log(path)


def load_reference(
    path: str | Path, *, kind: str | None = None, ied: str | None = None, host: str | None = None, live_model: Any = None
) -> Any:
    """``verify.reference.Reference.load`` with file problems turned into a usage error."""
    from ied_client.scl import SclError
    from ied_client.verify.reference import Reference

    from ..registry import UsageError

    try:
        k = kind or Reference.guess_kind(path)
        return Reference.load(k, path, ied, live_model=live_model, host=host)
    except (ValueError, SclError, OSError) as e:
        raise UsageError(f"cannot use {path} as a reference: {e}") from e
