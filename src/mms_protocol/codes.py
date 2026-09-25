"""The MMS module's code domains, registered with :mod:`ied_client.codes` on import.

Numeric values follow libiec61850 v1.6.1 (``iec61850_client.h``, ``mms_common.h``,
``mms_value.h``) and the ACSE / ISO session standards for the association-level codes. Names are
our own lower-kebab-case spellings.
"""

from __future__ import annotations

from enum import StrEnum

from ied_client import codes
from ied_client.codes import ErrorInfo


class MmsDomain(StrEnum):
    IED = "ied"  # libiec61850 IedClientError
    MMS = "mms"  # libiec61850 MmsError (service errors, rejects, file errors)
    DATA_ACCESS = "data-access"  # MMS DataAccessError (read/write result per variable)
    ACSE_DIAG = "acse-diag"  # ACSE AARE result-source-diagnostic (service-user)


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

# How an association attempt ended, established by our own probes below libiec61850 (DIA-3, DIA-4, DIA-6).
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

codes.register_domain(MmsDomain.DATA_ACCESS, DATA_ACCESS_ERRORS, aliases=("da", "dataaccesserror"), preference=10)
codes.register_domain(MmsDomain.IED, IED_ERRORS, aliases=("iederror",), preference=20)
codes.register_domain(MmsDomain.MMS, MMS_ERRORS, aliases=("mmserror",), preference=30)
codes.register_domain(MmsDomain.ACSE_DIAG, ACSE_USER_DIAGNOSTICS, aliases=("acse",), preference=70)
codes.register_association_outcomes(ASSOCIATION_OUTCOMES)
# libiec61850 constant names, so that `explain IED_ERROR_ACCESS_DENIED` works.
codes.register_constant_prefixes(
    (
        ("IED_ERROR_", MmsDomain.IED),
        ("DATA_ACCESS_ERROR_", MmsDomain.DATA_ACCESS),
        ("MMS_ERROR_", MmsDomain.MMS),
        ("ADD_CAUSE_", codes.Domain.ADD_CAUSE),
    )
)


def ied_error(code: int) -> ErrorInfo:
    return codes.info(MmsDomain.IED, code)


def mms_error(code: int) -> ErrorInfo:
    return codes.info(MmsDomain.MMS, code)


def data_access_error(code: int) -> ErrorInfo:
    return codes.info(MmsDomain.DATA_ACCESS, code)


def acse_diagnostic(code: int) -> ErrorInfo:
    return codes.info(MmsDomain.ACSE_DIAG, code)
