"""Run the simulated IED in a subprocess (so server callbacks never share a GIL with the client)."""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class SimProcess:
    def __init__(
        self,
        *,
        config: str | os.PathLike | None = None,
        scl: str | os.PathLike | None = None,
        ied: str | None = None,
        port: int | None = None,
        options: dict[str, Any] | None = None,
    ) -> None:
        self.port = port or free_port()
        cmd = [sys.executable, "-m", "tests.sim", "--port", str(self.port), "--options", json.dumps(options or {})]
        if config:
            cmd += ["--config", str(config)]
        else:
            cmd += ["--scl", str(scl)]
            if ied:
                cmd += ["--ied", ied]
        self.events: list[dict] = []
        self.output: list[str] = []
        self._ready = threading.Event()
        self.info: dict[str, Any] = {}
        self.proc = subprocess.Popen(cmd, cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()
        if not self._ready.wait(20):
            self.stop()
            raise RuntimeError("simulator did not start:\n" + "\n".join(self.output[-40:]))

    def _read(self) -> None:
        assert self.proc.stdout
        for line in self.proc.stdout:
            line = line.rstrip("\n")
            self.output.append(line)
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if not isinstance(ev, dict):
                continue
            if ev.get("event") == "ready":
                self.info = ev
                self._ready.set()
            else:
                self.events.append(ev)
        self._ready.set()

    def wait_event(self, pred, timeout: float = 5.0) -> dict | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for ev in list(self.events):
                if pred(ev):
                    return ev
            time.sleep(0.02)
        return None

    @property
    def alive(self) -> bool:
        return self.proc.poll() is None

    def kill(self) -> None:
        """Abrupt death (no FIN from the application; the OS still closes the sockets)."""
        if self.alive:
            self.proc.send_signal(signal.SIGKILL)
            self.proc.wait(5)

    def stop(self) -> None:
        if self.alive:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.kill()

    def __enter__(self) -> SimProcess:
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()
