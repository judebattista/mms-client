"""Protocol-independent connectivity probes: ICMP echo and TCP connect (DIA-1, DIA-5).

Rules: every probe has a timeout, can bind to a local address (NBR-2), needs no privileges, never
raises for network conditions, and records what it saw in ``raw`` so that an expert can check the
conclusion (CLI-7). A protocol module adds the probes of its own layers on top of these.
"""

from __future__ import annotations

import errno
import math
import os
import re
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any

from ied_client import codes
from ied_client.codes import ErrorInfo

DEFAULT_TIMEOUT_S = 3.0


@dataclass
class ProbeResult:
    """The outcome of one probe (or one step of the association probe).

    ``layer`` is the layer the probe tests (for the ``iso-association`` summary: the layer where
    the attempt stopped). ``ok`` is None when the probe could not decide (e.g. ping not permitted).
    For association-related probes ``outcome`` is a key of :data:`codes.ASSOCIATION_OUTCOMES`.
    """

    layer: str
    name: str
    ok: bool | None
    outcome: str
    duration_s: float
    detail: str
    error: ErrorInfo | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "layer": self.layer,
            "name": self.name,
            "ok": self.ok,
            "outcome": self.outcome,
            "duration_s": round(self.duration_s, 6),
            "detail": self.detail,
            "error": self.error.to_json() if self.error else None,
            "raw": jsonable(self.raw),
        }


def jsonable(v: Any) -> Any:
    if isinstance(v, bytes):
        return v.hex()
    if isinstance(v, dict):
        return {str(k): jsonable(x) for k, x in v.items()}
    if isinstance(v, list | tuple):
        return [jsonable(x) for x in v]
    if hasattr(v, "to_json"):
        return v.to_json()
    return v


# ---------------------------------------------------------------------------
# ICMP
# ---------------------------------------------------------------------------
_NOT_CONCLUSIVE = (
    "not conclusive on its own: many IEDs do not answer ping; the TCP probe decides reachability"
)


def ping(host: str, timeout: float = 1.0, *, local_ip: str | None = None) -> ProbeResult:
    """One ICMP echo using the system ``ping`` (no raw sockets, no privileges).

    ``ok`` is None when ping is missing or not permitted. A missing reply is reported, but callers
    must not treat it as fatal (many IEDs drop ICMP).
    """
    t0 = time.monotonic()
    raw: dict[str, Any] = {}

    def result(ok: bool | None, outcome: str, detail: str) -> ProbeResult:
        return ProbeResult("network", "icmp", ok, outcome, time.monotonic() - t0, detail, raw=raw)

    exe = shutil.which("ping")
    if exe is None:
        return result(None, "unavailable", "ICMP not tested: no 'ping' command found")
    wait = max(1, math.ceil(timeout))
    cmd = [exe, "-n", "-c", "1", "-W", str(wait)]
    if local_ip:
        cmd += ["-I", local_ip]
    cmd.append(host)
    raw["command"] = " ".join(cmd)
    env = dict(os.environ, LC_ALL="C", LANG="C")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=wait + 3, env=env, check=False)
    except subprocess.TimeoutExpired:
        return result(None, "error", "ICMP not tested: ping did not finish in time")
    except OSError as e:
        return result(None, "unavailable", f"ICMP not tested: {e}")
    out = (proc.stdout + "\n" + proc.stderr).strip()
    raw.update(returncode=proc.returncode, output=out[-2000:])
    low = out.lower()
    if proc.returncode == 0:
        m = re.search(r"time[=<]([\d.]+)\s*ms", out)
        raw["rtt_ms"] = rtt = float(m.group(1)) if m else None
        return result(
            True, "reply", f"ICMP echo reply from {host}" + (f" in {rtt} ms" if rtt is not None else "")
        )
    if "not permitted" in low or "permission denied" in low:
        return result(None, "not-permitted", f"ICMP not tested: ping not permitted here ({out[:120]})")
    if "unknown host" in low or "name or service not known" in low or "temporary failure in name" in low:
        return result(None, "unknown-host", f"ICMP not tested: cannot resolve {host}")
    if "unreachable" in low:
        return result(
            False,
            "host-unreachable",
            f"ICMP: destination unreachable reported for {host} (ping exit {proc.returncode}); {_NOT_CONCLUSIVE}",
        )
    if proc.returncode == 1:
        return result(
            False, "no-reply", f"ICMP: no echo reply from {host} within {wait} s; {_NOT_CONCLUSIVE}"
        )
    return result(None, "error", f"ICMP not tested: ping exit {proc.returncode} ({out[:120]})")


# ---------------------------------------------------------------------------
# TCP
# ---------------------------------------------------------------------------
_UNREACHABLE_ERRNOS = {errno.EHOSTUNREACH, errno.ENETUNREACH, errno.EHOSTDOWN, errno.ENETDOWN}


def errno_text(e: OSError) -> str:
    if e.errno is None:
        return str(e)
    return f"errno {e.errno} {errno.errorcode.get(e.errno, '?')}"


def open_tcp(
    host: str, port: int, timeout: float, local_ip: str | None
) -> tuple[socket.socket | None, ProbeResult]:
    """Connect; return the socket (or None) and the ``tcp-<port>`` ProbeResult."""
    raw: dict[str, Any] = {"host": host, "port": port, "local_ip": local_ip, "timeout_s": timeout}
    t0 = time.monotonic()

    def result(ok: bool | None, outcome: str, detail: str, error: ErrorInfo | None = None) -> ProbeResult:
        if error is None and ok is False:
            error = codes.association(outcome)
        return ProbeResult("network", f"tcp-{port}", ok, outcome, time.monotonic() - t0, detail, error, raw)

    try:
        addr = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)[0][4]
    except (OSError, IndexError) as e:
        return None, result(None, "unknown", f"cannot resolve {host!r}: {e}")
    raw["address"] = addr[0]
    target = f"TCP {addr[0]}:{port}"
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if local_ip:
        try:
            sock.bind((local_ip, 0))
        except OSError as e:
            sock.close()
            raw["errno"] = e.errno
            detail = (
                f"cannot bind to local address {local_ip} ({errno_text(e)}): the address is not configured "
                "on this host, so the device was not contacted"
            )
            return None, result(None, "unknown", detail, codes.tool("local-address-missing"))
    try:
        sock.settimeout(timeout)
        sock.connect(addr)
    except TimeoutError:
        sock.close()
        detail = (
            f"{target}: no answer to the connection request within {timeout:g} s "
            "(SYN unanswered: host down, filtered, or on an unreachable network)"
        )
        return None, result(False, "tcp-timeout", detail)
    except ConnectionRefusedError as e:
        sock.close()
        raw["errno"] = e.errno
        detail = f"{target}: connection refused (RST, {errno_text(e)}): nothing listens on this port"
        return None, result(False, "tcp-refused", detail)
    except OSError as e:
        sock.close()
        raw["errno"] = e.errno
        if e.errno in _UNREACHABLE_ERRNOS:
            detail = f"{target}: host unreachable ({errno_text(e)}; no ARP reply or no route)"
            return None, result(False, "host-unreachable", detail)
        return None, result(False, "unknown", f"{target}: connect failed ({errno_text(e)})")
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    local = sock.getsockname()
    raw["local_address"] = f"{local[0]}:{local[1]}"
    ms = (time.monotonic() - t0) * 1000
    return sock, result(
        True, "accepted", f"{target}: connection accepted in {ms:.1f} ms (from {local[0]}:{local[1]})"
    )


def tcp_connect(
    host: str, port: int, timeout: float = DEFAULT_TIMEOUT_S, local_ip: str | None = None
) -> ProbeResult:
    """TCP connect and close. Outcomes: accepted, tcp-refused, tcp-timeout, host-unreachable (or unknown)."""
    sock, result = open_tcp(host, port, timeout, local_ip)
    if sock is not None:
        sock.close()
    return result
