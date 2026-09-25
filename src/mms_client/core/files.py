"""Read-only MMS file services (FIL-1 … FIL-4). There is deliberately no upload or delete (FIL-3):
on many IEDs uploading a file installs configuration or firmware."""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from mms_client.adapter import FileEntry, ServiceError

from .session import Session

SCL_SUFFIXES = (".cid", ".icd", ".iid", ".scd", ".xml", ".ssd", ".sed")


def list_files(session: Session, directory: str = "", *, remember_error: bool = True) -> list[FileEntry]:
    client = session.require_client()
    try:
        entries = client.get_file_directory(directory)
    except ServiceError as e:
        session.log.write("file", action="dir", path=directory or "/", ok=False, error=e.error)
        if remember_error:
            session.remember_error(e.error, {"service": "file-dir"}, str(e))
        raise
    session.log.write("file", action="dir", path=directory or "/", ok=True, count=len(entries))
    return entries


@dataclass(slots=True)
class Download:
    remote: str
    local: Path
    size: int
    sha256: str
    duration_s: float

    def to_json(self) -> dict:
        return {
            "remote": self.remote,
            "local": str(self.local),
            "size": self.size,
            "sha256": self.sha256,
            "duration_s": round(self.duration_s, 3),
        }


def get_file(
    session: Session,
    remote: str,
    out: str | Path | None = None,
    *,
    overwrite: bool = False,
    progress: Callable[[int], None] | None = None,
) -> Download:
    """Download a file as-is (FIL-2, FIL-4). The download is logged."""
    client = session.require_client()
    local = Path(out) if out else Path(Path(remote).name or "download.bin")
    if local.is_dir():
        local = local / (Path(remote).name or "download.bin")
    if local.exists() and not overwrite:
        raise FileExistsError(f"{local} exists (use --overwrite)")
    tmp = local.with_name(local.name + ".part")
    t0 = time.perf_counter()
    h = hashlib.sha256()

    class _Sink:
        def __init__(self, fh):
            self.fh = fh

        def write(self, b: bytes) -> int:
            h.update(b)
            return self.fh.write(b)

    try:
        with open(tmp, "wb") as fh:
            n = client.get_file(remote, _Sink(fh), progress=progress)  # type: ignore[arg-type]
    except ServiceError as e:
        tmp.unlink(missing_ok=True)
        session.log.write("file", action="get", remote=remote, ok=False, error=e.error)
        session.remember_error(e.error, {"service": "file-get"}, str(e))
        raise
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    tmp.replace(local)
    d = Download(remote, local.resolve(), n, h.hexdigest(), time.perf_counter() - t0)
    session.log.write("file", action="get", ok=True, **d.to_json())
    return d


def find_scl_files(session: Session) -> list[FileEntry]:
    """Candidate device-supplied SCL files (VER-8). A device without file services has none; that is not
    an error the operator asked about, so it does not become `explain last`."""
    try:
        entries = list_files(session, remember_error=False)
    except ServiceError:
        return []
    return [e for e in entries if e.name.lower().endswith(SCL_SUFFIXES)]
