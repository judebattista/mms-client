"""Canonical names for every code the tool reports.

Every layer (protocol modules, core, explanation catalogue, JSON output) refers to codes through
:class:`ErrorInfo`, so that a code has exactly one spelling everywhere. The catalogue is keyed by
``ErrorInfo.key``, i.e. ``"<domain>:<name>"`` such as ``"add-cause:blocked-by-interlocking"``.

The domains defined here are protocol-independent: the IEC 61850 control model (AddCause,
LastApplError), the outcome of an association attempt, and the tool's own refusals and check
ids. A protocol module registers its own domains (e.g. its stack's error numbers) with
:func:`register_domain` when it is imported, and may add association outcomes with
:func:`register_association_outcomes`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum


class Domain(StrEnum):
    """The protocol-independent numbering schemes. Protocol modules add their own (plain strings)."""

    ADD_CAUSE = "add-cause"  # IEC 61850 control AddCause
    CTL_ERROR = "ctl-error"  # IEC 61850 control LastApplError.Error
    ASSOCIATION = "association"  # how an association attempt failed (a protocol module's classification)
    TOOL = "tool"  # refusals and failures raised by this tool itself (policy, input)
    CHECK = "check"  # check-result identifiers (for hints on failed checks)


@dataclass(frozen=True, slots=True)
class ErrorInfo:
    """One code in one domain, with its canonical name. ``domain`` is a :class:`Domain` or the name
    of a domain a protocol module registered."""

    domain: str
    code: int | None
    name: str

    @property
    def key(self) -> str:
        return f"{self.domain}:{self.name}"

    def __str__(self) -> str:
        if self.code is None:
            return self.key
        return f"{self.key} ({self.code})"

    def to_json(self) -> dict:
        return {"domain": str(self.domain), "code": self.code, "name": self.name}


# ---------------------------------------------------------------------------
# IEC 61850 control AddCause (IEC 61850-7-2), numbering as in libiec61850
# ---------------------------------------------------------------------------
ADD_CAUSES: dict[int, str] = {
    0: "unknown",
    1: "not-supported",
    2: "blocked-by-switching-hierarchy",
    3: "select-failed",
    4: "invalid-position",
    5: "position-reached",
    6: "parameter-change-in-execution",
    7: "step-limit",
    8: "blocked-by-mode",
    9: "blocked-by-process",
    10: "blocked-by-interlocking",
    11: "blocked-by-synchrocheck",
    12: "command-already-in-execution",
    13: "blocked-by-health",
    14: "1-of-n-control",
    15: "abortion-by-cancel",
    16: "time-limit-over",
    17: "abortion-by-trip",
    18: "object-not-selected",
    19: "object-already-selected",
    20: "no-access-authority",
    21: "ended-with-overshoot",
    22: "abortion-due-to-deviation",
    23: "abortion-by-communication-loss",
    24: "abortion-by-command",
    25: "none",
    26: "inconsistent-parameters",
    27: "locked-by-other-client",
}

# LastApplError.Error
CTL_ERRORS: dict[int, str] = {
    0: "no-error",
    1: "unknown",
    2: "timeout-test-not-ok",
    3: "operator-test-not-ok",
}

# Association outcomes (DIA-3, DIA-4, DIA-6): name -> description of how an association attempt ended.
# These are the outcomes the protocol-independent TCP probe reports; protocol modules add the outcomes
# of their own layers (and may word these more precisely) at import time.
ASSOCIATION_OUTCOMES: dict[str, str] = {
    "accepted": "Association accepted",
    "host-unreachable": "No route to host / host unreachable",
    "tcp-refused": "TCP connection refused (RST)",
    "tcp-timeout": "No answer to the TCP connection request",
    "unknown": "Association failed for an unknown reason",
}

# ---------------------------------------------------------------------------
# Control-related enumerations
# ---------------------------------------------------------------------------
CTL_MODELS: dict[int, str] = {
    0: "status-only",
    1: "direct-with-normal-security",
    2: "sbo-with-normal-security",
    3: "direct-with-enhanced-security",
    4: "sbo-with-enhanced-security",
}

OR_CATS: dict[int, str] = {
    0: "not-supported",
    1: "bay-control",
    2: "station-control",
    3: "remote-control",
    4: "automatic-bay",
    5: "automatic-station",
    6: "automatic-remote",
    7: "maintenance",
    8: "process",
}

# Short aliases accepted on the command line (ORG-2).
OR_CAT_ALIASES: dict[str, int] = {
    "bay": 1,
    "station": 2,
    "remote": 3,
    "auto-bay": 4,
    "auto-station": 5,
    "auto-remote": 6,
    "maintenance": 7,
    "process": 8,
    "not-supported": 0,
}

# Functional constraints, in the order of libiec61850's FunctionalConstraint enum (the values are
# that enum's numbers).
FCS: dict[str, int] = {
    "ST": 0,
    "MX": 1,
    "SP": 2,
    "SV": 3,
    "CF": 4,
    "DC": 5,
    "SG": 6,
    "SE": 7,
    "SR": 8,
    "OR": 9,
    "BL": 10,
    "EX": 11,
    "CO": 12,
    "US": 13,
    "MS": 14,
    "RP": 15,
    "BR": 16,
    "LG": 17,
    "GO": 18,
}

# FCs that clients normally write (MOD-2 / RW-6). Everything else needs expert mode.
NORMALLY_WRITABLE_FCS: frozenset[str] = frozenset({"SP", "SV", "CF", "DC", "EX", "BL"})

# The client's encoding of report and RCB bit fields. A protocol module converts its own encoding into
# these integers (they follow libiec61850, which the MMS module uses directly).
# Reasons for inclusion in reports.
REASONS_FOR_INCLUSION: dict[int, str] = {
    0: "not-included",
    1: "data-change",
    2: "quality-change",
    4: "data-update",
    8: "integrity",
    16: "general-interrogation",
    32: "unknown",
}

# TrgOps bits.
TRG_OPS: dict[int, str] = {
    1: "dchg",
    2: "qchg",
    4: "dupd",
    8: "period",
    16: "gi",
}

# OptFlds bits (segmentation is not represented).
OPT_FLDS: dict[int, str] = {
    1: "seqNum",
    2: "timeStamp",
    4: "reasonCode",
    8: "dataSet",
    16: "dataRef",
    32: "bufOvfl",
    64: "entryID",
    128: "confRev",
}


# ---------------------------------------------------------------------------
# Domain registry
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class DomainInfo:
    """What the explanation system knows about a domain.

    ``table`` maps numbers to names for numbered domains (None for named-only domains such as
    ``tool``). ``aliases`` are extra spellings ``explain`` accepts for the domain name. ``preference``
    orders the domains when a bare error name exists in several (lower first).
    """

    name: str
    table: Mapping[int, str] | None
    aliases: tuple[str, ...] = ()
    preference: int = 100


_DOMAINS: dict[str, DomainInfo] = {}
# (prefix, domain): constant names of a protocol stack that `explain` accepts, e.g. "IED_ERROR_".
_CONSTANT_PREFIXES: list[tuple[str, str]] = []
# Tool codes a protocol module raises; the catalogue must cover them like the client's own (EXP-7).
_REQUIRED_TOOL_CODES: list[str] = []


def register_domain(
    name: str, table: Mapping[int, str] | None = None, *, aliases: Iterable[str] = (), preference: int = 100
) -> str:
    """Make a domain known (idempotent; a later registration of the same name replaces the earlier)."""
    domain = _DOMAINS[name].name if name in _DOMAINS else name
    _DOMAINS[name] = DomainInfo(domain, table, tuple(aliases), preference)
    return domain


def register_association_outcomes(outcomes: Mapping[str, str]) -> None:
    """Add a protocol module's association outcomes (name -> description)."""
    ASSOCIATION_OUTCOMES.update(outcomes)


def register_constant_prefixes(prefixes: Iterable[tuple[str, str]]) -> None:
    for item in prefixes:
        if item not in _CONSTANT_PREFIXES:
            _CONSTANT_PREFIXES.append(item)


def register_required_tool_codes(names: Iterable[str]) -> None:
    for n in names:
        if n not in _REQUIRED_TOOL_CODES:
            _REQUIRED_TOOL_CODES.append(n)


register_domain(Domain.ADD_CAUSE, ADD_CAUSES, aliases=("addcause",), preference=40)
register_domain(Domain.CTL_ERROR, CTL_ERRORS, aliases=("lastapplerror", "ctl", "controlerror"), preference=50)
register_domain(Domain.ASSOCIATION, None, aliases=("assoc",), preference=60)
register_domain(Domain.TOOL, None, preference=80)
register_domain(Domain.CHECK, None, preference=90)


def _lookup(name: str) -> DomainInfo | None:
    info_ = _DOMAINS.get(name)
    if info_ is None:
        from ied_client.protocol.registry import load_all

        load_all()  # protocol modules register their domains when imported
        info_ = _DOMAINS.get(name)
    return info_


def domain(name: str) -> str:
    """The canonical domain for ``name``; ValueError for a domain nobody registered."""
    info_ = _lookup(name)
    if info_ is None:
        raise ValueError(f"{name!r} is not a known code domain")
    return info_.name


def domains() -> list[DomainInfo]:
    """Every registered domain, in bare-name preference order."""
    return sorted(_DOMAINS.values(), key=lambda d: d.preference)


def numbered_tables() -> dict[str, Mapping[int, str]]:
    """Domain -> number table, for every numbered domain."""
    return {d.name: d.table for d in domains() if d.table is not None}


def constant_prefixes() -> list[tuple[str, str]]:
    return list(_CONSTANT_PREFIXES)


def required_tool_codes() -> list[str]:
    return list(_REQUIRED_TOOL_CODES)


# ---------------------------------------------------------------------------
# Constructors
# ---------------------------------------------------------------------------
def info(domain_: str, code: int) -> ErrorInfo:
    """ErrorInfo for a numeric code; unknown numbers get the name ``unknown-<n>``."""
    d = _lookup(domain_)
    if d is None or d.table is None:
        raise KeyError(f"{domain_!r} is not a numbered code domain")
    return ErrorInfo(d.name, code, d.table.get(code, f"unknown-{code}"))


def add_cause(code: int) -> ErrorInfo:
    return info(Domain.ADD_CAUSE, code)


def ctl_error(code: int) -> ErrorInfo:
    return info(Domain.CTL_ERROR, code)


def association(name: str) -> ErrorInfo:
    if name not in ASSOCIATION_OUTCOMES:
        raise KeyError(f"unknown association outcome {name!r}")
    return ErrorInfo(Domain.ASSOCIATION, None, name)


def tool(name: str) -> ErrorInfo:
    return ErrorInfo(Domain.TOOL, None, name)


def check(check_id: str) -> ErrorInfo:
    return ErrorInfo(Domain.CHECK, None, check_id)


def parse_key(key: str) -> ErrorInfo:
    """Parse ``"domain:name"`` (or ``"domain:<number>"``) back into an ErrorInfo."""
    domain_s, _, name = key.partition(":")
    d = _lookup(domain_s)
    if d is None:
        raise ValueError(f"{domain_s!r} is not a known code domain")
    if d.table is not None:
        if name.lstrip("-").isdigit():
            return info(d.name, int(name))
        for code, n in d.table.items():
            if n == name:
                return ErrorInfo(d.name, code, n)
        raise KeyError(f"unknown {d.name} code {name!r}")
    if d.name == Domain.ASSOCIATION:
        return association(name)
    return ErrorInfo(d.name, None, name)
