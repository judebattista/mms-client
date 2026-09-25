"""Exceptions a protocol module raises. Each service failure carries the raw code (CLI-7) as an ErrorInfo."""

from __future__ import annotations

from ied_client.codes import ErrorInfo


class ProtocolError(Exception):
    """Base class for everything a protocol module raises."""


class ServiceError(ProtocolError):
    """A service failed. ``error`` is the raw code reported by the stack or the device."""

    def __init__(self, service: str, target: str | None, error: ErrorInfo, detail: str | None = None) -> None:
        self.service = service
        self.target = target
        self.error = error
        self.detail = detail
        msg = f"{service}"
        if target:
            msg += f" {target}"
        msg += f" failed: {error}"
        if detail:
            msg += f" — {detail}"
        super().__init__(msg)

    def to_json(self) -> dict:
        return {
            "service": self.service,
            "target": self.target,
            "error": self.error.to_json(),
            "detail": self.detail,
        }


class ConnectError(ServiceError):
    """The association could not be established."""


class NotConnectedError(ProtocolError):
    """An operation was attempted without an open association."""


class EncodeError(ProtocolError, ValueError):
    """A value does not fit the type the device's model declares (RW-3)."""
