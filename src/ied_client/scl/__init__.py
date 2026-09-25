"""SCL (IEC 61850-6) support: parse SCD/CID/ICD files and derive the expected server model.

Public API::

    doc = load_scl("rack.scd")                 # SclDocument (any edition, incl. mixed SCDs)
    doc.ieds, doc.connected_ap("BCU1")          # IED list, addresses
    edition_of(doc, "BCU1")                     # EditionInfo("Ed2", reason, ...)
    srv = expand_server(doc, "BCU1")            # ExpectedServer: tree, attributes, datasets, RCBs
    resolve_client_ln(doc, srv.rcbs[0].clients[0])   # which client IED uses this RCB

Module layout: ``model`` (parsed file), ``parser`` (``load_scl``), ``expected`` / ``expand``
(the server model as a client sees it), ``btypes`` (bType → value type table), ``edition``,
``crossdoc`` (client relationships).
"""

from __future__ import annotations

from ied_client.scl.btypes import BTYPES, BTypeInfo, ValueType, btype_info, value_type_of
from ied_client.scl.crossdoc import (
    ClientRelationship,
    ResolvedClient,
    client_relationships,
    resolve_client_ln,
    role_hint,
)
from ied_client.scl.edition import EditionInfo, edition_of, edition_of_version
from ied_client.scl.errors import SclError
from ied_client.scl.expand import domain_name, expand_server
from ied_client.scl.expected import (
    OPT_FLDS_BITS,
    TRG_OPT_BITS,
    ExpectedAttribute,
    ExpectedControlBlock,
    ExpectedDataSet,
    ExpectedDO,
    ExpectedFCDA,
    ExpectedLD,
    ExpectedLN,
    ExpectedRCB,
    ExpectedServer,
    ExpectedSGCB,
)
from ied_client.scl.model import (
    DAI,
    DOI,
    FCDA,
    IED,
    LN,
    SDI,
    AccessPoint,
    ClientLN,
    ConnectedAP,
    ControlBlockAddress,
    DataSet,
    DataTypeTemplates,
    Header,
    LDevice,
    ReportControl,
    SclDocument,
    Server,
    Services,
    SettingControl,
    SubNetwork,
)
from ied_client.scl.parser import SCL_NS, load_scl

__all__ = [
    "BTYPES",
    "DAI",
    "DOI",
    "FCDA",
    "IED",
    "LN",
    "OPT_FLDS_BITS",
    "SCL_NS",
    "SDI",
    "TRG_OPT_BITS",
    "AccessPoint",
    "BTypeInfo",
    "ClientLN",
    "ClientRelationship",
    "ConnectedAP",
    "ControlBlockAddress",
    "DataSet",
    "DataTypeTemplates",
    "EditionInfo",
    "ExpectedAttribute",
    "ExpectedControlBlock",
    "ExpectedDO",
    "ExpectedDataSet",
    "ExpectedFCDA",
    "ExpectedLD",
    "ExpectedLN",
    "ExpectedRCB",
    "ExpectedSGCB",
    "ExpectedServer",
    "Header",
    "LDevice",
    "ReportControl",
    "ResolvedClient",
    "SclDocument",
    "SclError",
    "Server",
    "Services",
    "SettingControl",
    "SubNetwork",
    "ValueType",
    "btype_info",
    "client_relationships",
    "domain_name",
    "edition_of",
    "edition_of_version",
    "expand_server",
    "load_scl",
    "resolve_client_ln",
    "role_hint",
    "value_type_of",
]
