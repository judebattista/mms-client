"""MMS-level data types returned by the adapter, in addition to the client's shared types.

Nothing here refers to native memory: every value crossing the adapter boundary is a copy,
so callers never manage libiec61850 lifetimes.
"""

from __future__ import annotations

from dataclasses import dataclass

from ied_client.protocol.types import ValueKind

# libiec61850 MmsType numbering
MMS_KIND_BY_NUMBER: dict[int, ValueKind] = {
    0: ValueKind.ARRAY,
    1: ValueKind.STRUCTURE,
    2: ValueKind.BOOLEAN,
    3: ValueKind.BIT_STRING,
    4: ValueKind.INTEGER,
    5: ValueKind.UNSIGNED,
    6: ValueKind.FLOAT,
    7: ValueKind.OCTET_STRING,
    8: ValueKind.VISIBLE_STRING,
    9: ValueKind.GENERALIZED_TIME,
    10: ValueKind.BINARY_TIME,
    11: ValueKind.BCD,
    12: ValueKind.OBJ_ID,
    13: ValueKind.UNICODE_STRING,
    14: ValueKind.UTC_TIME,
    15: ValueKind.ACCESS_ERROR,
}


# MMS services-supported bit positions (ISO 9506 ServiceSupportOptions) that matter here.
SERVICE_BITS: dict[int, str] = {
    0: "status",
    1: "getNameList",
    2: "identify",
    3: "rename",
    4: "read",
    5: "write",
    6: "getVariableAccessAttributes",
    11: "defineNamedVariableList",
    12: "getNamedVariableListAttributes",
    13: "deleteNamedVariableList",
    46: "obtainFile",
    65: "readJournal",
    67: "initializeJournal",
    68: "reportJournalStatus",
    72: "fileOpen",
    73: "fileRead",
    74: "fileClose",
    75: "fileRename",
    76: "fileDelete",
    77: "fileDirectory",
    78: "unsolicitedStatus",
    79: "informationReport",
    83: "conclude",
    84: "cancel",
}


@dataclass(frozen=True, slots=True)
class ConnectionParams:
    """Negotiated MMS initiate parameters."""

    max_pdu_size: int
    max_serv_outstanding_calling: int
    max_serv_outstanding_called: int
    data_structure_nesting_level: int
    services_supported: bytes

    def supports(self, bit: int) -> bool:
        byte, off = divmod(bit, 8)
        if byte >= len(self.services_supported):
            return False
        return bool(self.services_supported[byte] & (0x80 >> off))

    def supported_services(self) -> list[str]:
        return [name for bit, name in sorted(SERVICE_BITS.items()) if self.supports(bit)]

    def to_json(self) -> dict:
        return {
            "max_pdu_size": self.max_pdu_size,
            "max_serv_outstanding_calling": self.max_serv_outstanding_calling,
            "max_serv_outstanding_called": self.max_serv_outstanding_called,
            "data_structure_nesting_level": self.data_structure_nesting_level,
            "services_supported_hex": self.services_supported.hex(),
            "services_supported": self.supported_services(),
        }


@dataclass(frozen=True, slots=True)
class VariableListEntry:
    """One entry of a named variable list (a dataset member), in MMS terms."""

    domain: str
    item: str
    array_index: int | None = None
    component: str | None = None

    def mms_ref(self) -> str:
        s = f"{self.domain}/{self.item}"
        if self.array_index is not None:
            s += f"({self.array_index})"
        if self.component:
            s += f"${self.component}"
        return s


# Keys accepted by IedClient.set_rcb, mapped to libiec61850 RCB_ELEMENT_* mask bits.
RCB_ELEMENTS: dict[str, int] = {
    "rpt_id": 1,
    "rpt_ena": 2,
    "resv": 4,
    "dataset": 8,
    "conf_rev": 16,
    "opt_flds": 32,
    "buf_tm": 64,
    "sq_num": 128,
    "trg_ops": 256,
    "intg_pd": 512,
    "gi": 1024,
    "purge_buf": 2048,
    "entry_id": 4096,
    "time_of_entry": 8192,
    "resv_tms": 16384,
    "owner": 32768,
}
