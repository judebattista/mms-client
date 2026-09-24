"""Exceptions → failed :class:`CommandResult` with the raw code (CLI-7), exit code and hint (EXP-1)."""

from __future__ import annotations

import logging
import traceback
from typing import TYPE_CHECKING, Any

from mms_client import codes
from mms_client.adapter import ConnectError, EncodeError, NotConnectedError, ServiceError
from mms_client.codes import ErrorInfo
from mms_client.core.model import AmbiguousFcError, NotInModelError
from mms_client.core.refs import RefError
from mms_client.core.safety import ConfirmationDeclined, PolicyError
from mms_client.inventory import InventoryError

from .registry import UsageError
from .result import EXIT_CONNECT, EXIT_FAILED, EXIT_REFUSED, EXIT_USAGE, CommandResult, failure

if TYPE_CHECKING:
    from .context import CliContext
    from .registry import Command

log = logging.getLogger(__name__)

HINT_FIELDS = ("service", "fc", "cdc", "ln_class", "mode", "edition", "ctl_model")


class NotAvailable(Exception):
    """A command whose engine is not part of this build yet."""

    def __init__(self, command: str, needs: str) -> None:
        self.command = command
        super().__init__(f"`{command}` is not available yet in this build ({needs}).")


def from_exception(ctx: CliContext, cmd: Command | None, exc: BaseException) -> CommandResult:
    """Map an exception raised while running ``cmd`` to a failed result."""
    service = cmd.service if cmd else None
    if isinstance(exc, UsageError):
        r = failure(codes.tool("usage-error"), str(exc), exit_code=EXIT_USAGE, show_hint=False)
        r.data = {"usage": exc.usage.strip() if exc.usage else None}
        return r
    if isinstance(exc, AmbiguousFcError):
        return failure(
            codes.tool("ambiguous-fc"),
            str(exc),
            exit_code=EXIT_USAGE,
            data={"reference": exc.ref.iec(), "fcs": exc.fcs},
        )
    if isinstance(exc, NotInModelError):
        return failure(codes.tool("object-not-in-model"), f"{exc} (the device's model has no such object)", exit_code=EXIT_FAILED)
    if isinstance(exc, RefError):
        return failure(codes.tool("invalid-reference"), str(exc), exit_code=EXIT_USAGE)
    if isinstance(exc, EncodeError):
        return failure(codes.tool("invalid-value"), str(exc), exit_code=EXIT_USAGE)
    if isinstance(exc, ConnectError):
        ctx_h = {"service": "associate"}
        if ctx.session is not None and ctx.session.target.local_ip:
            ctx_h["tags"] = {"bound_ip": "yes", "local_ip": ctx.session.target.local_ip}
        return failure(exc.error, str(exc), exit_code=EXIT_CONNECT, hint_context=ctx_h, data={"service_error": exc.to_json()})
    if isinstance(exc, NotConnectedError):
        return failure(codes.tool("not-connected"), str(exc), exit_code=EXIT_CONNECT, show_hint=False)
    if isinstance(exc, ServiceError):
        return failure(
            exc.error,
            str(exc),
            exit_code=EXIT_FAILED,
            hint_context={"service": exc.service or service},
            data={"service_error": exc.to_json()},
        )
    if isinstance(exc, PolicyError):
        return failure(exc.error, str(exc), exit_code=EXIT_REFUSED)
    if isinstance(exc, ConfirmationDeclined):
        if ctx.ui.interactive:
            # The operator said no: not a fault, no hint needed.
            return failure(codes.tool("not-confirmed"), f"{exc}; nothing was sent", exit_code=EXIT_REFUSED, show_hint=False)
        return failure(codes.tool("non-interactive-no-prompt"), f"{exc}; nothing was sent", exit_code=EXIT_REFUSED)
    if isinstance(exc, InventoryError):
        return failure(codes.tool("inventory-invalid"), str(exc), exit_code=EXIT_USAGE)
    if isinstance(exc, NotAvailable):
        return failure(codes.tool("not-available"), str(exc), exit_code=EXIT_FAILED, show_hint=False)
    if isinstance(exc, FileExistsError):
        return failure(codes.tool("file-exists"), str(exc), exit_code=EXIT_USAGE, show_hint=False)
    if isinstance(exc, FileNotFoundError):
        return failure(codes.tool("file-not-found"), str(exc), exit_code=EXIT_USAGE, show_hint=False)
    if isinstance(exc, OSError):
        return failure(codes.tool("os-error"), str(exc), exit_code=EXIT_FAILED, show_hint=False)
    # Anything else is a bug in the tool: say so, keep the traceback in the log.
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    log.debug(tb)
    r = failure(
        codes.tool("internal-error"),
        f"internal error: {type(exc).__name__}: {exc} (the traceback is in the session log; please report it)",
        exit_code=EXIT_FAILED,
        show_hint=False,
    )
    r.data = {"exception": type(exc).__name__, "traceback": tb}
    return r


# ---------------------------------------------------------------------------------- hints
def catalogue() -> Any:
    """The explanation catalogue, or None if it cannot be loaded (degrade to raw codes)."""
    try:
        from mms_client.explain import default_catalogue

        return default_catalogue()
    except Exception:  # pragma: no cover - catalogue missing or broken in this build
        log.debug("explanation catalogue unavailable", exc_info=True)
        return None


def hint_context(values: dict[str, Any]) -> Any:
    try:
        from mms_client.explain import HintContext
    except Exception:  # pragma: no cover
        return None
    kw = {k: values[k] for k in HINT_FIELDS if values.get(k) is not None}
    tags = values.get("tags")
    if isinstance(tags, dict) and tags:
        kw["tags"] = tags
    try:
        return HintContext(**kw)
    except Exception:  # pragma: no cover - unexpected context values
        return HintContext()


def hint_for(error: ErrorInfo, values: dict[str, Any]) -> Any:
    """The one-line hint for ``error`` in this context (EXP-1, EXP-3), or None."""
    cat = catalogue()
    if cat is None:
        return None
    try:
        return cat.hint(error, hint_context(values))
    except Exception:  # pragma: no cover - never let a hint break a command
        log.debug("hint lookup failed", exc_info=True)
        return None


def context_for(ctx: CliContext, cmd: Command | None, result: CommandResult) -> dict[str, Any]:
    """What the tool knew when the failure happened (EXP-3): the core's context for the error,
    the command's own additions, and the session's mode and edition."""
    values: dict[str, Any] = {}
    s = ctx.session
    if s is not None and s.last_error is not None and s.last_error.error == result.error:
        values.update(s.last_error.context)
    values.update(result.hint_context)
    if cmd is not None and cmd.service:
        values.setdefault("service", cmd.service)
    values.setdefault("mode", ctx.mode.value)
    if s is not None:
        ed = s.edition
        if ed and ed != "unknown":
            values.setdefault("edition", ed)
    return values
