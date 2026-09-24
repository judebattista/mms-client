"""A session: one persistent association with one device (CLI-1), plus everything the operator
set for it (mode, orCat, terse), the cached model (MDL-4), the cleanup registry (RPT-7) and
the session log (LOG-1).

Everything the CLI can do goes through this object and the service modules next to it, so the
same operations are available from Python without the CLI (ARC-1).
"""

from __future__ import annotations

import signal
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mms_client import codes
from mms_client.adapter import ConnectError, IedClient, NotConnectedError, ServiceError
from mms_client.codes import ErrorInfo
from mms_client.inventory import ClientRelation, Device, Inventory

from .model import DeviceModel, browse
from .results import default_or_ident
from .safety import Interaction, Mode, NonInteractive, Policy, PolicyError, SafetyProfile
from .sessionlog import SessionLog


@dataclass(slots=True)
class Target:
    """Where to connect, and as whom (INV-5, NBR-1)."""

    host: str
    port: int = 102
    name: str | None = None  # inventory name, if any
    device: Device | None = None
    as_client: ClientRelation | None = None
    local_ip: str | None = None

    @property
    def label(self) -> str:
        return self.name or self.host


def resolve_target(
    spec: str, inventory: Inventory | None, *, port: int | None = None, as_client: str | None = None, local_ip: str | None = None
) -> Target:
    """Device name from the inventory, or a raw IP / host[:port] for quick use (INV-5)."""
    dev = inventory.device(spec) if inventory else None
    if dev is not None:
        t = Target(dev.ip, port or dev.port, dev.name, dev)
    else:
        host, sep, p = spec.rpartition(":")
        if sep and p.isdigit() and host:
            t = Target(host, int(p))
        else:
            t = Target(spec, port or 102)
    if as_client:
        if inventory is None:
            raise PolicyError(codes.tool("inventory-required"), "--as needs an inventory (--inventory FILE)")
        rel = inventory.client(as_client, t.name)
        if rel is None:
            raise PolicyError(
                codes.tool("unknown-client"),
                f"no client relationship {as_client!r} for {t.label} in the inventory",
            )
        t.as_client = rel
        t.local_ip = rel.source_ip
    if local_ip:
        t.local_ip = local_ip
    return t


@dataclass
class CleanupAction:
    """Something the tool changed on the device and must undo (RPT-7)."""

    key: str
    description: str
    undo: Callable[[], None]
    if_lost: str  # what lingers if the association is gone and we cannot undo


@dataclass
class LastError:
    error: ErrorInfo
    context: dict[str, Any] = field(default_factory=dict)
    message: str = ""


class Session:
    """One association with one device and the operator's settings for it."""

    def __init__(
        self,
        target: Target,
        *,
        inventory: Inventory | None = None,
        policy: Policy | None = None,
        ui: Interaction | None = None,
        log: SessionLog | None = None,
        terse: bool = False,
        connect_timeout_ms: int = 5000,
        request_timeout_ms: int = 10000,
        or_ident: str | None = None,
    ) -> None:
        self.target = target
        self.inventory = inventory
        safety = SafetyProfile(inventory.safety) if inventory else SafetyProfile.STRICT
        self.policy = policy or Policy(safety=safety)
        if policy is None and inventory is not None:
            self.policy.safety = safety
        self.ui: Interaction = ui or NonInteractive()
        self.log = log or SessionLog(None, device=target.label)
        self.terse = terse
        self.connect_timeout_ms = connect_timeout_ms
        self.request_timeout_ms = request_timeout_ms
        self.client: IedClient | None = None
        self._model: DeviceModel | None = None
        self.identity: Any = None  # core.identity.IdentityReport, filled lazily
        self.or_cat: int | None = None
        self.or_ident = or_ident or default_or_ident()
        self.cleanups: list[CleanupAction] = []
        self.last_error: LastError | None = None
        self.connection_lost = threading.Event()
        self.subscriptions: dict[str, Any] = {}  # rcb reference -> core.reports.Subscription
        self.selected: dict[str, Any] = {}  # control objects selected with `select` (CTL-5)
        self.reference: Any = None  # verify reference in use, if any
        self.cwd: tuple[str, ...] = ()
        self.last_results: dict[str, Any] = {}  # "check" / "diagnose" -> latest report (LOG-3)

    # ------------------------------------------------------------------ properties
    @property
    def mode(self) -> Mode:
        return self.policy.mode

    @property
    def device_name(self) -> str:
        return self.target.label

    @property
    def edition(self) -> str:
        ident = self.identity
        if ident is None:
            return "unknown"
        return getattr(getattr(ident, "edition", None), "value", None) or "unknown"

    # ------------------------------------------------------------------ connection
    def connect(self) -> None:
        """Open the association (checks the bind address first, NBR-2)."""
        if self.client is not None and self.client.is_connected:
            return
        if self.target.local_ip:
            self._check_bind_address(self.target.local_ip)
        client = IedClient(connect_timeout_ms=self.connect_timeout_ms, request_timeout_ms=self.request_timeout_ms)
        t0 = time.perf_counter()
        try:
            client.connect(self.target.host, self.target.port, local_ip=self.target.local_ip)
        except ConnectError as e:
            self.log.write(
                "request",
                service="associate",
                target=f"{self.target.host}:{self.target.port}",
                local_ip=self.target.local_ip,
                ok=False,
                error=e.error,
                duration_s=time.perf_counter() - t0,
            )
            self.remember_error(e.error, {"service": "associate"}, str(e))
            raise
        self.client = client
        self.connection_lost.clear()
        client.add_closed_listener(self.connection_lost.set)
        self.log.write(
            "request",
            service="associate",
            target=f"{self.target.host}:{self.target.port}",
            local_ip=self.target.local_ip,
            as_client=self.target.as_client.name if self.target.as_client else None,
            ok=True,
            duration_s=time.perf_counter() - t0,
        )

    def _check_bind_address(self, ip: str) -> None:
        try:
            from mms_client.diagnosis.net import check_bind_address
        except ImportError:  # diagnosis package not available
            return
        check = check_bind_address(ip, self.target.host)
        if not check.ok:
            raise PolicyError(codes.tool("local-address-missing"), check.message)

    def require_client(self) -> IedClient:
        if self.client is None:
            raise NotConnectedError(f"not connected to {self.device_name} (use `connect`)")
        if not self.client.is_connected:
            raise NotConnectedError(
                f"the association with {self.device_name} is closed"
                + (" (connection lost)" if self.connection_lost.is_set() else "")
            )
        return self.client

    @property
    def connected(self) -> bool:
        return self.client is not None and self.client.is_connected

    def disconnect(self) -> list[str]:
        """Undo our changes (RPT-7), then release the association. Returns cleanup notes."""
        notes = self.run_cleanups()
        if self.client is not None:
            self.client.close()
            self.log.write("request", service="release", target=self.device_name, ok=True)
            self.client = None
        return notes

    def close(self) -> list[str]:
        notes = self.disconnect()
        self.log.write("session-end", cleanup=notes)
        self.log.close()
        return notes

    def __enter__(self) -> Session:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ model (MDL-4)
    def model(self, *, refresh: bool = False, progress: Callable[[str, int, int], None] | None = None) -> DeviceModel:
        if self._model is None or refresh:
            client = self.require_client()
            self._model = browse(client, progress=progress)
            self.log.write(
                "request",
                service="browse",
                target=self.device_name,
                ok=not self._model.errors,
                logical_devices=list(self._model.lds),
                errors=self._model.errors,
                duration_s=self._model.duration_s,
            )
        return self._model

    @property
    def cached_model(self) -> DeviceModel | None:
        return self._model

    # ------------------------------------------------------------------ cleanup registry (RPT-7)
    def register_cleanup(self, action: CleanupAction) -> None:
        self.cleanups = [a for a in self.cleanups if a.key != action.key]
        self.cleanups.append(action)

    def drop_cleanup(self, key: str) -> None:
        self.cleanups = [a for a in self.cleanups if a.key != key]

    def run_cleanups(self) -> list[str]:
        """Undo in reverse order. Anything we cannot undo is reported, with its effect (RPT-7)."""
        notes: list[str] = []
        while self.cleanups:
            action = self.cleanups.pop()
            if not self.connected:
                notes.append(f"could not undo: {action.description} — {action.if_lost}")
                self.log.write("rcb", action="cleanup-skipped", key=action.key, reason="not connected", lingering=action.if_lost)
                continue
            try:
                action.undo()
                notes.append(f"undone: {action.description}")
                self.log.write("rcb", action="cleanup", key=action.key, ok=True)
            except (ServiceError, NotConnectedError) as e:
                err = getattr(e, "error", None)
                notes.append(f"could not undo: {action.description} ({err or e}) — {action.if_lost}")
                self.log.write("rcb", action="cleanup", key=action.key, ok=False, error=err, lingering=action.if_lost)
        return notes

    def install_signal_cleanup(self) -> None:
        """Make SIGTERM behave like Ctrl-C so that cleanup runs (RPT-7)."""
        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)

    # ------------------------------------------------------------------ errors (explain last)
    def remember_error(self, error: ErrorInfo, context: dict[str, Any] | None = None, message: str = "") -> None:
        self.last_error = LastError(error, dict(context or {}), message)
        self.log.write("error", error=error, context=context or {}, message=message)

    # ------------------------------------------------------------------ misc
    def set_or_cat(self, value: int) -> None:
        from .controls import check_or_cat_allowed

        check_or_cat_allowed(self.policy, value)
        self.or_cat = value
        self.log.write("note", setting="orcat", value=value, name=codes.OR_CATS.get(value))

    def owner_name(self, owner: bytes | None) -> str | None:
        """Resolve an RCB Owner (usually the client's IP address) to an inventory name."""
        ip = owner_ip(owner)
        if ip is None:
            return None
        if self.inventory is not None:
            name = self.inventory.name_for_ip(ip)
            if name:
                return name
        if self.target.local_ip == ip or (self.client is not None and self.client.local_ip == ip):
            return "this tool"
        return None

    def log_path(self) -> Path | None:
        return self.log.path


def _raise_keyboard_interrupt(*_args: object) -> None:
    raise KeyboardInterrupt


def owner_ip(owner: bytes | None) -> str | None:
    """IEC 61850 Owner is an octet string; libiec61850 and most IEDs put the client's IPv4
    address in the last four octets (IPv6 in 16)."""
    if not owner or not any(owner):
        return None
    if len(owner) >= 16 and any(owner[:-4]):
        import ipaddress

        try:
            return str(ipaddress.IPv6Address(bytes(owner[-16:])))
        except ValueError:
            return None
    return ".".join(str(b) for b in owner[-4:])
