"""Canonical names for every numeric code the tool reports.

Every layer (adapter, core, explanation catalogue, JSON output) refers to codes through
:class:`ErrorInfo` and the tables below, so that a code has exactly one spelling everywhere.
The catalogue (``mms_client/data/hints.yaml``) is keyed by ``ErrorInfo.key``, i.e.
``"<domain>:<name>"`` such as ``"data-access:object-access-denied"``.

Numeric values follow libiec61850 v1.6.1 (``iec61850_client.h``, ``mms_common.h``,
``mms_value.h``, ``iec61850_common.h``) and the ACSE / ISO session standards for the
association-level codes. Names are our own lower-kebab-case spellings.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Domain(StrEnum):
    """Which numbering scheme a code belongs to."""

    IED = "ied"  # libiec61850 IedClientError
    MMS = "mms"  # libiec61850 MmsError (service errors, rejects, file errors)
    DATA_ACCESS = "data-access"  # MMS DataAccessError (read/write result per variable)
    ADD_CAUSE = "add-cause"  # IEC 61850 control AddCause
    CTL_ERROR = "ctl-error"  # IEC 61850 control LastApplError.Error
    ASSOCIATION = "association"  # how an association attempt failed (our own classification)
    ACSE_DIAG = "acse-diag"  # ACSE AARE result-source-diagnostic (service-user)
    TOOL = "tool"  # refusals and failures raised by this tool itself (policy, input)
    CHECK = "check"  # check-result identifiers (for hints on failed checks)


@dataclass(frozen=True, slots=True)
class ErrorInfo:
    """One code in one domain, with its canonical name."""

    domain: Domain
    code: int | None
    name: str

    @property
    def key(self) -> str:
        return f"{self.domain.value}:{self.name}"

    def __str__(self) -> str:
        if self.code is None:
            return self.key
        return f"{self.key} ({self.code})"

    def to_json(self) -> dict:
        return {"domain": self.domain.value, "code": self.code, "name": self.name}


# ---------------------------------------------------------------------------
# libiec61850 IedClientError
# ---------------------------------------------------------------------------
IED_ERRORS: dict[int, str] = {
    0: "ok",
    1: "not-connected",
    2: "already-connected",
    3: "connection-lost",
    4: "service-not-supported",
    5: "connection-rejected",
    6: "outstanding-call-limit-reached",
    10: "user-provided-invalid-argument",
    11: "enable-report-failed-dataset-mismatch",
    12: "object-reference-invalid",
    13: "unexpected-value-received",
    20: "timeout",
    21: "access-denied",
    22: "object-does-not-exist",
    23: "object-exists",
    24: "object-access-unsupported",
    25: "type-inconsistent",
    26: "temporarily-unavailable",
    27: "object-undefined",
    28: "invalid-address",
    29: "hardware-fault",
    30: "type-unsupported",
    31: "object-attribute-inconsistent",
    32: "object-value-invalid",
    33: "object-invalidated",
    34: "malformed-message",
    35: "object-constraint-conflict",
    98: "service-not-implemented",
    99: "unknown",
}

# ---------------------------------------------------------------------------
# libiec61850 MmsError
# ---------------------------------------------------------------------------
MMS_ERRORS: dict[int, str] = {
    0: "none",
    1: "connection-rejected",
    2: "connection-lost",
    3: "service-timeout",
    4: "parsing-response",
    5: "hardware-fault",
    6: "conclude-rejected",
    7: "invalid-arguments",
    8: "outstanding-call-limit",
    9: "other",
    10: "vmdstate-other",
    20: "application-reference-other",
    30: "definition-other",
    31: "definition-invalid-address",
    32: "definition-type-unsupported",
    33: "definition-type-inconsistent",
    34: "definition-object-undefined",
    35: "definition-object-exists",
    36: "definition-object-attribute-inconsistent",
    40: "resource-other",
    41: "resource-capability-unavailable",
    50: "service-other",
    55: "service-object-constraint-conflict",
    60: "service-preempt-other",
    70: "time-resolution-other",
    80: "access-other",
    81: "access-object-non-existent",
    82: "access-object-access-unsupported",
    83: "access-object-access-denied",
    84: "access-object-invalidated",
    85: "access-object-value-invalid",
    86: "access-temporarily-unavailable",
    90: "file-other",
    91: "file-filename-ambiguous",
    92: "file-file-busy",
    93: "file-filename-syntax-error",
    94: "file-content-type-invalid",
    95: "file-position-invalid",
    96: "file-file-access-denied",
    97: "file-file-non-existent",
    98: "file-duplicate-filename",
    99: "file-insufficient-space-in-filestore",
    100: "reject-other",
    101: "reject-unknown-pdu-type",
    102: "reject-invalid-pdu",
    103: "reject-unrecognized-service",
    104: "reject-unrecognized-modifier",
    105: "reject-request-invalid-argument",
}

# ---------------------------------------------------------------------------
# MMS DataAccessError (ISO 9506-2), as used by libiec61850 (mms_value.h)
# ---------------------------------------------------------------------------
DATA_ACCESS_ERRORS: dict[int, str] = {
    0: "object-invalidated",
    1: "hardware-fault",
    2: "temporarily-unavailable",
    3: "object-access-denied",
    4: "object-undefined",
    5: "invalid-address",
    6: "type-unsupported",
    7: "type-inconsistent",
    8: "object-attribute-inconsistent",
    9: "object-access-unsupported",
    10: "object-non-existent",
    11: "object-value-invalid",
    12: "unknown",
}

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

# ---------------------------------------------------------------------------
# Association outcomes (DIA-3, DIA-4, DIA-6): our classification of how an
# association attempt ended, established by our own probes below libiec61850.
# ---------------------------------------------------------------------------
ASSOCIATION_OUTCOMES: dict[str, str] = {
    "accepted": "Association accepted",
    "host-unreachable": "No route to host / host unreachable",
    "tcp-refused": "TCP connection refused (RST) on the MMS port",
    "tcp-timeout": "No answer to the TCP connection request",
    "tcp-closed-immediately": "TCP accepted, then closed before any ISO response",
    "cotp-rejected": "COTP connection request rejected (DR/ER)",
    "cotp-no-response": "No answer to the COTP connection request",
    "cotp-invalid-response": "COTP response could not be understood",
    "session-refused": "ISO session connect refused (RF SPDU)",
    "session-aborted": "ISO session aborted by the server (AB SPDU)",
    "presentation-rejected": "Presentation connect rejected (CPR)",
    "acse-rejected-permanent": "ACSE association rejected (permanent)",
    "acse-rejected-transient": "ACSE association rejected (transient)",
    "acse-aborted": "ACSE abort (ABRT) received",
    "initiate-error": "MMS initiate rejected (initiate-ErrorPDU)",
    "initiate-no-response": "No answer to the association / MMS initiate request",
    "invalid-response": "Association response could not be understood",
    "accepted-then-closed": "Association accepted, then closed by the server",
    "tls-suspected": "TCP 102 closed but TCP 3782 (IEC 62351 TLS) open",
    "authentication-suspected": "Rejection pattern indicates ACSE authentication is required",
    "timeout": "Timeout during association",
    "unknown": "Association failed for an unknown reason",
}

# ACSE AARE result-source-diagnostic, acse-service-user values (ISO 8650).
ACSE_USER_DIAGNOSTICS: dict[int, str] = {
    0: "null",
    1: "no-reason-given",
    2: "application-context-name-not-supported",
    3: "calling-ap-title-not-recognized",
    4: "calling-ap-invocation-identifier-not-recognized",
    5: "calling-ae-qualifier-not-recognized",
    6: "calling-ae-invocation-identifier-not-recognized",
    7: "called-ap-title-not-recognized",
    8: "called-ap-invocation-identifier-not-recognized",
    9: "called-ae-qualifier-not-recognized",
    10: "called-ae-invocation-identifier-not-recognized",
    11: "authentication-mechanism-name-not-recognized",
    12: "authentication-mechanism-name-required",
    13: "authentication-failure",
    14: "authentication-required",
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

# Functional constraints, numbered as libiec61850's FunctionalConstraint enum.
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

# Reasons for inclusion in reports (bit values as libiec61850 reports them).
REASONS_FOR_INCLUSION: dict[int, str] = {
    0: "not-included",
    1: "data-change",
    2: "quality-change",
    4: "data-update",
    8: "integrity",
    16: "general-interrogation",
    32: "unknown",
}

# TrgOps bits (IEC 61850-8-1 bit positions mapped by libiec61850 into an integer).
TRG_OPS: dict[int, str] = {
    1: "dchg",
    2: "qchg",
    4: "dupd",
    8: "period",
    16: "gi",
}

# OptFlds bits as libiec61850 exposes them (RPT_OPT_*; segmentation is not exposed).
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
# Constructors
# ---------------------------------------------------------------------------
_TABLES: dict[Domain, dict[int, str]] = {
    Domain.IED: IED_ERRORS,
    Domain.MMS: MMS_ERRORS,
    Domain.DATA_ACCESS: DATA_ACCESS_ERRORS,
    Domain.ADD_CAUSE: ADD_CAUSES,
    Domain.CTL_ERROR: CTL_ERRORS,
    Domain.ACSE_DIAG: ACSE_USER_DIAGNOSTICS,
}


def info(domain: Domain, code: int) -> ErrorInfo:
    """ErrorInfo for a numeric code; unknown numbers get the name ``unknown-<n>``."""
    table = _TABLES[domain]
    return ErrorInfo(domain, code, table.get(code, f"unknown-{code}"))


def ied_error(code: int) -> ErrorInfo:
    return info(Domain.IED, code)


def mms_error(code: int) -> ErrorInfo:
    return info(Domain.MMS, code)


def data_access_error(code: int) -> ErrorInfo:
    return info(Domain.DATA_ACCESS, code)


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
    domain = Domain(domain_s)
    table = _TABLES.get(domain)
    if table is not None:
        if name.lstrip("-").isdigit():
            return info(domain, int(name))
        for code, n in table.items():
            if n == name:
                return ErrorInfo(domain, code, n)
        raise KeyError(f"unknown {domain.value} code {name!r}")
    if domain is Domain.ASSOCIATION:
        return association(name)
    return ErrorInfo(domain, None, name)
