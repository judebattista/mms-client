"""The MMS protocol module (MMS-PROTOCOL-SPEC.md): the :class:`~ied_client.protocol.api.ProtocolModule`
the client finds through the ``ied_client.protocols`` entry point."""

from __future__ import annotations

import importlib.resources
from typing import Any

from ied_client.diagnosis.probe import ProbeResult
from ied_client.protocol.api import FailedAssociation

from . import codes as _codes  # noqa: F401  (registers the MMS code domains)
from .association import MmsAssociation
from .names import MmsNaming


def _data_files(subdir: str | None, suffix: str = ".yaml") -> list[tuple[str, str]]:
    data = importlib.resources.files("mms_protocol").joinpath("data")
    node = data.joinpath(subdir) if subdir else data
    label = f"mms_protocol/data/{subdir}" if subdir else "mms_protocol/data"
    return [
        (f"{label}/{child.name}", child.read_text(encoding="utf-8"))
        for child in sorted(node.iterdir(), key=lambda c: c.name)
        if child.name.endswith(suffix) and child.is_file()
    ]


class MmsProtocol:
    name = "mms"
    display_name = "MMS"
    default_port = 102
    names = MmsNaming()
    identify_source = "mms-identify"
    blind_spots = (
        "GOOSE and Sampled Values are not MMS: paths that use them (e.g. bay-level interlocking between "
        "devices) cannot be observed by this tool."
    )
    # IEC 61850-8-1 maps a select without value to a read of SBO, which carries no originator (ORG-5).
    sbo_normal_select_note = "SBO with normal security: the select carries no orCat"
    association_layers = ("network", "transport-session", "mms-initiate", "association")

    @property
    def next_steps(self) -> dict[str, list[str]]:
        from .diagnosis.connection import NEXT_STEPS

        return NEXT_STEPS

    def open(
        self,
        host: str,
        port: int,
        *,
        local_ip: str | None = None,
        connect_timeout_ms: int = 5000,
        request_timeout_ms: int = 10000,
    ) -> MmsAssociation:
        from .adapter import IedClient

        client = IedClient(connect_timeout_ms=connect_timeout_ms, request_timeout_ms=request_timeout_ms)
        try:
            client.connect(host, port, local_ip=local_ip)
        except BaseException:
            client.close()
            raise
        return MmsAssociation(client)

    def versions(self) -> dict[str, str]:
        from .adapter import libiec61850_version, pyiec61850_version

        return {"pyiec61850_ng": pyiec61850_version(), "libiec61850": libiec61850_version()}

    def describe_association(self, info: dict[str, Any]) -> list[tuple[str, Any]]:
        return [
            ("max PDU size", info["max_pdu_size"]),
            ("outstanding calls", f"calling {info['max_serv_outstanding_calling']}, called {info['max_serv_outstanding_called']}"),
            ("nesting level", info["data_structure_nesting_level"]),
            ("services", ", ".join(info["services_supported"])),
        ]

    def connection_diagnosis(self):
        from .diagnosis.connection import MmsConnectionDiagnosis

        return MmsConnectionDiagnosis()

    def classify_failed_association(
        self, host: str, port: int, tcp: ProbeResult, timeout_s: float, local_ip: str | None
    ) -> FailedAssociation | None:
        from .diagnosis.connection import classify_failed_association

        return classify_failed_association(host, port, tcp, timeout_s, local_ip)

    def catalogue_sources(self) -> list[tuple[str, str]]:
        return _data_files("hints")

    def quirks_sources(self) -> list[tuple[str, str]]:
        return [s for s in _data_files(None) if s[0].endswith("/quirks.yaml")]
