"""Session JSONL log (LOG-1) and the data needed by restore (LOG-2) and incidents (LOG-3)."""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ied_client import ENV_PREFIX, TOOL_NAME

from .results import SCHEMA_VERSION, jsonable


def default_log_dir() -> Path:
    base = os.environ.get(f"{ENV_PREFIX}_LOG_DIR")
    if base:
        return Path(base)
    state = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(state) / TOOL_NAME / "sessions"


class SessionLog:
    """Append-only JSONL log. One entry per line: ``{"seq", "ts", "kind", ...}``.

    Kinds: session-start, command, request (summarised), write, restore, control, rcb, check, file,
    report (sampled), diagnose, identity, note, error, session-end.
    """

    def __init__(self, path: Path | None, *, session_id: str | None = None, device: str | None = None) -> None:
        self.path = path
        self.session_id = session_id or uuid.uuid4().hex[:12]
        self.device = device
        self._seq = 0
        self._lock = threading.Lock()
        self._fh = None
        self.entries: list[dict[str, Any]] = []  # in-memory copy (restore, incident excerpts)
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = open(path, "a", encoding="utf-8")  # noqa: SIM115 - long-lived handle

    @classmethod
    def create(cls, device: str | None, log_dir: Path | None = None, *, enabled: bool = True) -> SessionLog:
        if not enabled:
            return cls(None, device=device)
        sid = uuid.uuid4().hex[:12]
        stamp = time.strftime("%Y%m%d-%H%M%S")
        safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in (device or "none"))
        return cls((log_dir or default_log_dir()) / f"{stamp}-{safe}-{sid}.jsonl", session_id=sid, device=device)

    def write(self, kind: str, **fields: Any) -> dict[str, Any]:
        with self._lock:
            self._seq += 1
            entry = {"seq": self._seq, "ts": round(time.time(), 6), "kind": kind, **jsonable(fields)}
            self.entries.append(entry)
            if self._fh is not None:
                self._fh.write(json.dumps(entry, separators=(",", ":"), sort_keys=False) + "\n")
                self._fh.flush()
            return entry

    def start(self, **info: Any) -> None:
        self.write("session-start", schema_version=SCHEMA_VERSION, session_id=self.session_id, device=self.device, **info)

    def close(self) -> None:
        with self._lock:
            if self._fh is not None:
                self._fh.close()
                self._fh = None

    def excerpt(self, last: int = 200) -> list[dict[str, Any]]:
        return self.entries[-last:]


@dataclass(frozen=True, slots=True)
class LoggedWrite:
    """A write that restore can undo."""

    seq: int
    ref: str  # IEC reference
    fc: str
    before: Any  # JSON form (value_to_json)
    after: Any
    kind: str  # "write" | "setgroup" | "rcb-param"
    extra: dict[str, Any]


def read_log(path: Path) -> list[dict[str, Any]]:
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def session_id_of(entries: list[dict[str, Any]]) -> str | None:
    """The session id recorded in a log's session-start entry."""
    return next((e.get("session_id") for e in entries if e.get("kind") == "session-start"), None)


def restored_seqs(markers: list[dict[str, Any]], source_session: str | None, *, same_log: bool) -> set[int]:
    """Sequence numbers of the source log's writes that a restore has already put back successfully.

    ``markers`` are log entries. Since Version 1.01 a restore records each outcome as a ``restore`` entry
    naming the source session (the writes it makes carry the same fields, but only the ``restore`` entry
    says whether the value was really put back). Logs written before Version 1.01 have ``write`` entries
    with ``restored_from`` and no session: those count only in the log they are in (``same_log``).
    """
    done: set[int] = set()
    for e in markers:
        if not e.get("ok") or e.get("restored_from") is None:
            continue
        if "restored_from_session" in e:
            counts = e.get("kind") == "restore" and e["restored_from_session"] == source_session
        else:
            counts = e.get("kind") == "write" and same_log
        if counts:
            done.add(int(e["restored_from"]))
    return done


def restorable_writes(entries: list[dict[str, Any]], *, already_restored: set[int] | frozenset[int] = frozenset()) -> Iterator[LoggedWrite]:
    """Successful writes in the log, newest first (LOG-2). Controls are never included (CTL-10); nor are
    the writes that a restore made, or writes that a restore has already put back (``already_restored``)."""
    for e in reversed(entries):
        if e.get("kind") != "write" or not e.get("ok") or e.get("restored_from") is not None:
            continue
        if e.get("seq") in already_restored:
            continue
        if e.get("write_kind") == "write-test":  # wrote the current value back: nothing to undo
            continue
        if e.get("before") is None:
            continue
        yield LoggedWrite(
            seq=e["seq"],
            ref=e["ref"],
            fc=e["fc"],
            before=e["before"],
            after=e.get("after"),
            kind=e.get("write_kind", "write"),
            extra={k: e[k] for k in ("setting_group",) if k in e},
        )
