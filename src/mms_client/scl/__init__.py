"""SCL (IEC 61850-6) support: parse SCD/CID/ICD files and derive the expected MMS server model.

Public API::

    doc = load_scl("rack.scd")                 # SclDocument (any edition, incl. mixed SCDs)
    doc.ieds, doc.connected_ap("BCU1")          # IED list, addresses
    edition_of(doc, "BCU1")                     # EditionInfo("Ed2", reason, ...)
    srv = expand_server(doc, "BCU1")            # ExpectedServer: tree, attributes, datasets, RCBs
    resolve_client_ln(doc, srv.rcbs[0].clients[0])   # which client IED uses this RCB
    to_libiec61850_config(srv)                  # model config text for the test simulator

Module layout: ``model`` (parsed file), ``parser`` (``load_scl``), ``expected`` / ``expand``
(the server model as a client sees it), ``btypes`` (bType → MMS type table), ``edition``,
``crossdoc`` (client relationships), ``libiec_config`` (simulator export).
"""

from __future__ import annotations

from mms_client.scl.btypes import BTYPES, BTypeInfo, MmsType, btype_info, libiec_type_of, mms_type_of
from mms_client.scl.crossdoc import (
    ClientRelationship,
    ResolvedClient,
    client_relationships,
    resolve_client_ln,
    role_hint,
)
from mms_client.scl.edition import EditionInfo, edition_of, edition_of_version
from mms_client.scl.errors import SclError
from mms_client.scl.expand import domain_name, expand_server
from mms_client.scl.expected import (
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
from mms_client.scl.libiec_config import to_libiec61850_config
from mms_client.scl.model import (
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
from mms_client.scl.parser import SCL_NS, load_scl

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
    "MmsType",
    "ReportControl",
    "ResolvedClient",
    "SclDocument",
    "SclError",
    "Server",
    "Services",
    "SettingControl",
    "SubNetwork",
    "btype_info",
    "client_relationships",
    "domain_name",
    "edition_of",
    "edition_of_version",
    "expand_server",
    "libiec_type_of",
    "load_scl",
    "mms_type_of",
    "resolve_client_ln",
    "role_hint",
    "to_libiec61850_config",
]
