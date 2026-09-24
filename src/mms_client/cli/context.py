"""State shared by all commands of one CLI run (a one-shot command, or a shell)."""

from __future__ import annotations

import contextlib
import signal
import sys
import threading
from pathlib import Path
from typing import IO, Any

from mms_client import __version__
from mms_client.adapter import NotConnectedError
from mms_client.core.controls import parse_or_cat
from mms_client.core.model import DeviceModel
from mms_client.core.safety import Interaction, Mode, Policy, SafetyProfile
from mms_client.core.session import Session, resolve_target
from mms_client.core.sessionlog import SessionLog, default_log_dir
from mms_client.inventory import Inventory, load_inventory

from .interact import PromptInteraction, TextNonInteractive
from .options import LineOptions, Options
from .render import Output


def stdin_is_tty() -> bool:
    try:
        return bool(sys.stdin and sys.stdin.isatty())
    except (AttributeError, ValueError, OSError):
        return False


class CliContext:
    """Options, output, the operator interaction and the current session.

    ``options`` are the session-wide options; ``line`` holds per-command overrides given on a
    shell line (in one-shot mode every option is session-wide). Use :meth:`opt` to read the
    effective value.
    """

    def __init__(
        self,
        options: Options | None = None,
        *,
        out: Output | None = None,
        stdout: IO[str] | None = None,
        ui: Interaction | None = None,
        in_shell: bool = False,
        interactive: bool | None = None,
    ) -> None:
        self.options = options or Options()
        self.line = LineOptions()
        self.stdout = stdout or sys.stdout
        self.out = out or Output.for_stream(self.stdout)
        self._fixed_ui = ui
        self._interactive = stdin_is_tty() if interactive is None else interactive
        self._prompt_ui: PromptInteraction | None = None
        self.in_shell = in_shell
        self.session: Session | None = None
        self.session_spec: str | None = None
        self._inventory: Inventory | None = None
        self._inventory_loaded = False
        self.argv: list[str] = []
        self.prev_cwd: tuple[str, ...] = ()
        self._old_sigterm: Any = None

    # ------------------------------------------------------------------ options
    def opt(self, name: str) -> Any:
        v = getattr(self.line, name, None)
        return v if v is not None else getattr(self.options, name)

    @property
    def json(self) -> bool:
        return bool(self.opt("json"))

    @property
    def terse(self) -> bool:
        if self.line.terse is not None:
            return self.line.terse
        return bool(self.options.terse or (self.session is not None and self.session.terse))

    @property
    def expert(self) -> bool:
        if self.session is not None:
            return self.session.policy.mode is Mode.EXPERT
        return bool(self.options.expert)

    @property
    def mode(self) -> Mode:
        return Mode.EXPERT if self.expert else Mode.STANDARD

    # ------------------------------------------------------------------ interaction
    @property
    def ui(self) -> Interaction:
        """The operator interaction for the current command: never blocks with --json or when
        stdin is not a terminal (IDN-5)."""
        if self._fixed_ui is not None:
            return self._fixed_ui
        if self.json or not self._interactive:
            return TextNonInteractive(None if self.json else self.out)
        if self._prompt_ui is None:
            self._prompt_ui = PromptInteraction(self.out)
        return self._prompt_ui

    # ------------------------------------------------------------------ inventory
    def inventory(self) -> Inventory | None:
        if not self._inventory_loaded:
            self._inventory_loaded = True
            if self.options.inventory:
                self._inventory = load_inventory(self.options.inventory)
        return self._inventory

    @property
    def log_dir(self) -> Path:
        return Path(self.options.log_dir) if self.options.log_dir else default_log_dir()

    # ------------------------------------------------------------------ session
    @property
    def device_label(self) -> str | None:
        if self.session is not None:
            return self.session.device_name
        return self.session_spec

    def make_session(
        self, spec: str, *, as_client: str | None = None, bind: str | None = None, port: int | None = None
    ) -> Session:
        """Create (not connect) a session for ``spec`` with its log (LOG-1). Sets ``self.session``."""
        o = self.options
        inv = self.inventory()
        target = resolve_target(
            spec, inv, port=port or o.port, as_client=as_client or o.as_client, local_ip=bind or o.bind
        )
        safety = SafetyProfile(inv.safety) if inv else SafetyProfile.STRICT
        policy = Policy(mode=Mode.EXPERT if o.expert else Mode.STANDARD, safety=safety, yes=bool(o.yes))
        log = SessionLog.create(target.label, self.log_dir, enabled=not o.no_log)
        timeout = o.timeout
        s = Session(
            target,
            inventory=inv,
            policy=policy,
            ui=self.ui,
            log=log,
            terse=bool(o.terse),
            connect_timeout_ms=timeout or 5000,
            request_timeout_ms=timeout or 10000,
            or_ident=o.or_ident,
        )
        log.start(
            tool_version=__version__,
            argv=self.argv,
            shell=self.in_shell,
            host=target.host,
            port=target.port,
            local_ip=target.local_ip,
            as_client=target.as_client.name if target.as_client else None,
            mode=policy.mode.value,
            safety=safety.value,
            inventory=str(inv.path) if inv and inv.path else None,
            experiment=inv.experiment if inv else None,
        )
        try:
            if o.orcat:
                s.set_or_cat(parse_or_cat(str(o.orcat)))
        except Exception:
            s.close()
            raise
        self.session = s
        self.session_spec = spec
        self._install_sigterm(s)
        return s

    def _install_sigterm(self, s: Session) -> None:
        if threading.current_thread() is not threading.main_thread():
            return
        if self._old_sigterm is None:
            self._old_sigterm = signal.getsignal(signal.SIGTERM)
        s.install_signal_cleanup()  # SIGTERM behaves like Ctrl-C, so cleanup runs (RPT-7)

    def require_session(self) -> Session:
        s = self.session
        if s is None:
            raise NotConnectedError("no device: start with `connect <device>`" if self.in_shell else "no device given")
        s.require_client()
        return s

    def connect(self) -> Session:
        s = self.session
        if s is None:
            raise NotConnectedError("no device: use `connect <device>`")
        if s.client is not None and not s.connected:
            # a lost association: release what is left of it before opening a new one
            s.disconnect()
        s.connect()
        return s

    def ensure_model(self, *, refresh: bool = False) -> DeviceModel:
        """The browsed model (MDL-4), with a progress indicator on a terminal."""
        s = self.require_session()
        if s.cached_model is not None and not refresh:
            return s.cached_model
        if self.json or not self.out.is_terminal:
            return s.model(refresh=refresh)
        with self.out.console.status("Reading the device model …") as status:

            def progress(name: str, i: int, n: int) -> None:
                status.update(f"Reading the device model: {name} ({i}/{n})")

            return s.model(refresh=refresh, progress=progress)

    def close_session(self) -> list[str]:
        """Undo the tool's changes (RPT-7), release the association and close the log."""
        s = self.session
        if s is None:
            return []
        self.session = None
        try:
            notes = s.close()
        finally:
            if self._old_sigterm is not None and threading.current_thread() is threading.main_thread():
                with contextlib.suppress(ValueError, TypeError):
                    signal.signal(signal.SIGTERM, self._old_sigterm)
                self._old_sigterm = None
        return notes
