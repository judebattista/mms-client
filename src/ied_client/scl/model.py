"""Dataclasses for a parsed SCL document (the file as written, before expansion).

Names follow the SCL element and attribute names, in snake_case. Every element that can be
the subject of an error or warning carries ``line`` (1-based line in the source file).
Lists preserve document order.

Nothing here resolves type references: see :mod:`ied_client.scl.expand` for the server model
as a client sees it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Header and Communication
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Header:
    """``SCL/Header``."""

    id: str = ""
    version: str | None = None
    revision: str | None = None
    tool_id: str | None = None
    name_structure: str | None = None


@dataclass(slots=True)
class ControlBlockAddress:
    """A ``GSE`` or ``SMV`` element of a ConnectedAP (the control block's network address)."""

    kind: str  # "GSE" or "SMV"
    ld_inst: str
    cb_name: str
    address: dict[str, str] = field(default_factory=dict)
    min_time: int | None = None  # GSE MinTime (ms)
    max_time: int | None = None  # GSE MaxTime (ms)
    line: int | None = None


@dataclass(slots=True)
class ConnectedAP:
    """``Communication/SubNetwork/ConnectedAP``: where an IED access point sits on a subnetwork.

    ``address`` maps the ``P`` types (``IP``, ``IP-SUBNET``, ``IP-GATEWAY``, ``OSI-TSEL``,
    ``OSI-SSEL``, ``OSI-PSEL``, ``OSI-AP-Title``, ``OSI-AE-Qualifier``, ...) to their text.
    """

    ied_name: str
    ap_name: str
    subnetwork: str = ""
    subnetwork_type: str | None = None
    desc: str | None = None
    address: dict[str, str] = field(default_factory=dict)
    gse: list[ControlBlockAddress] = field(default_factory=list)
    smv: list[ControlBlockAddress] = field(default_factory=list)
    line: int | None = None

    @property
    def ip(self) -> str | None:
        return self.address.get("IP")

    @property
    def ip_subnet(self) -> str | None:
        return self.address.get("IP-SUBNET")

    @property
    def ip_gateway(self) -> str | None:
        return self.address.get("IP-GATEWAY")


@dataclass(slots=True)
class SubNetwork:
    """``Communication/SubNetwork``."""

    name: str
    type: str | None = None
    desc: str | None = None
    connected_aps: list[ConnectedAP] = field(default_factory=list)
    line: int | None = None


# ---------------------------------------------------------------------------
# Values and instance data (DOI / SDI / DAI)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Val:
    """One ``Val`` element. ``sgroup`` is the setting group it belongs to (SG/SE values)."""

    text: str
    sgroup: int | None = None


@dataclass(slots=True)
class DAI:
    """``DAI``: instance value / short address of one data attribute."""

    name: str
    vals: list[Val] = field(default_factory=list)
    s_addr: str | None = None
    val_kind: str | None = None
    val_import: bool | None = None
    ix: int | None = None
    desc: str | None = None
    line: int | None = None


@dataclass(slots=True)
class SDI:
    """``SDI``: instance data of a sub data object or a constructed attribute."""

    name: str
    ix: int | None = None
    desc: str | None = None
    sdis: list[SDI] = field(default_factory=list)
    dais: list[DAI] = field(default_factory=list)
    line: int | None = None


@dataclass(slots=True)
class DOI:
    """``DOI``: instance data of a data object."""

    name: str
    ix: int | None = None
    desc: str | None = None
    sdis: list[SDI] = field(default_factory=list)
    dais: list[DAI] = field(default_factory=list)
    line: int | None = None


# ---------------------------------------------------------------------------
# Datasets and control blocks
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class FCDA:
    """``FCDA``: one dataset member. ``do_name``/``da_name`` may contain dots (SDO / BDA)."""

    ld_inst: str
    prefix: str = ""
    ln_class: str = ""
    ln_inst: str = ""
    do_name: str = ""
    da_name: str = ""
    fc: str = ""
    ix: int | None = None
    line: int | None = None

    @property
    def ln_name(self) -> str:
        if self.ln_class == "LLN0":
            return "LLN0"
        return f"{self.prefix}{self.ln_class}{self.ln_inst}"


@dataclass(slots=True)
class DataSet:
    """``DataSet``."""

    name: str
    desc: str | None = None
    fcdas: list[FCDA] = field(default_factory=list)
    line: int | None = None


#: TrgOps flag names in SCL spelling.
TRG_OPS_NAMES: tuple[str, ...] = ("dchg", "qchg", "dupd", "period", "gi")

#: OptFields flag names in SCL spelling.
OPT_FIELDS_NAMES: tuple[str, ...] = (
    "seqNum",
    "timeStamp",
    "dataSet",
    "reasonCode",
    "dataRef",
    "entryID",
    "configRef",
    "bufOvfl",
    "segmentation",
)


@dataclass(slots=True)
class ClientLN:
    """``ClientLN``: a client logical node an RCB is assigned to."""

    ied_name: str
    ln_class: str
    ld_inst: str = ""
    prefix: str = ""
    ln_inst: str = ""
    ap_ref: str | None = None
    desc: str | None = None
    line: int | None = None

    @property
    def ln_name(self) -> str:
        if self.ln_class == "LLN0":
            return "LLN0"
        return f"{self.prefix}{self.ln_class}{self.ln_inst}"

    @property
    def label(self) -> str:
        """Human-readable ``IED[/AP]:LD/LN`` label."""
        ap = f"/{self.ap_ref}" if self.ap_ref else ""
        ld = f"{self.ld_inst}/" if self.ld_inst else ""
        return f"{self.ied_name}{ap}:{ld}{self.ln_name}"


@dataclass(slots=True)
class ReportControl:
    """``ReportControl``.

    ``trg_ops`` and ``opt_fields`` are the sets of flags that are true (SCL names). Schema
    defaults are applied by the parser: ``gi`` defaults to true; ``bufOvfl`` defaults to
    true in 2007 documents and false in 2003 documents.
    """

    name: str
    rpt_id: str | None = None
    dat_set: str | None = None
    conf_rev: int | None = None
    buffered: bool = False
    buf_time: int = 0
    indexed: bool = True
    intg_pd: int = 0
    desc: str | None = None
    trg_ops: frozenset[str] = frozenset()
    opt_fields: frozenset[str] = frozenset()
    rpt_enabled_max: int = 1
    has_rpt_enabled: bool = False
    client_lns: list[ClientLN] = field(default_factory=list)
    line: int | None = None


@dataclass(slots=True)
class LogControl:
    """``LogControl``. ``ld_inst``/``prefix``/``ln_class``/``ln_inst`` locate the log (Ed2)."""

    name: str
    dat_set: str | None = None
    log_name: str | None = None
    log_ena: bool = True
    reason_code: bool = True
    intg_pd: int = 0
    buf_time: int = 0
    trg_ops: frozenset[str] = frozenset()
    ld_inst: str | None = None
    prefix: str = ""
    ln_class: str | None = None
    ln_inst: str = ""
    desc: str | None = None
    line: int | None = None


@dataclass(slots=True)
class GSEControl:
    """``GSEControl`` (GOOSE control block; kept for structure comparison only)."""

    name: str
    dat_set: str | None = None
    conf_rev: int | None = None
    app_id: str | None = None
    type: str = "GOOSE"
    fixed_offs: bool = False
    desc: str | None = None
    line: int | None = None


@dataclass(slots=True)
class SampledValueControl:
    """``SampledValueControl`` (kept for structure comparison only)."""

    name: str
    dat_set: str | None = None
    conf_rev: int | None = None
    smv_id: str | None = None
    multicast: bool = True
    smp_rate: int | None = None
    nof_asdu: int | None = None
    desc: str | None = None
    line: int | None = None


@dataclass(slots=True)
class SettingControl:
    """``SettingControl`` (setting group control block, LLN0 only)."""

    num_of_sgs: int
    act_sg: int = 1
    resv_tms: int | None = None
    desc: str | None = None
    line: int | None = None


# ---------------------------------------------------------------------------
# IED structure
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class LN:
    """``LN0`` or ``LN``. ``name`` is ``LLN0`` or prefix + lnClass + inst."""

    ln_class: str
    ln_type: str
    inst: str = ""
    prefix: str = ""
    desc: str | None = None
    is_ln0: bool = False
    dois: list[DOI] = field(default_factory=list)
    datasets: list[DataSet] = field(default_factory=list)
    report_controls: list[ReportControl] = field(default_factory=list)
    log_controls: list[LogControl] = field(default_factory=list)
    gse_controls: list[GSEControl] = field(default_factory=list)
    smv_controls: list[SampledValueControl] = field(default_factory=list)
    setting_control: SettingControl | None = None
    logs: list[str] = field(default_factory=list)
    line: int | None = None

    @property
    def name(self) -> str:
        if self.is_ln0:
            return "LLN0"
        return f"{self.prefix}{self.ln_class}{self.inst}"


@dataclass(slots=True)
class LDevice:
    """``LDevice``."""

    inst: str
    ld_name: str | None = None
    desc: str | None = None
    ln0: LN | None = None
    lns: list[LN] = field(default_factory=list)
    line: int | None = None

    @property
    def all_lns(self) -> list[LN]:
        """LN0 (if any) followed by the other LNs, in document order."""
        return ([self.ln0] if self.ln0 is not None else []) + self.lns


@dataclass(slots=True)
class Server:
    """``Server``."""

    ldevices: list[LDevice] = field(default_factory=list)
    timeout: int | None = None
    desc: str | None = None
    line: int | None = None


@dataclass(slots=True)
class AccessPoint:
    """``AccessPoint``. ``server_at`` names another AP whose server this AP exposes (Ed2).

    ``lns`` holds LNs placed directly in the access point (client-only IEDs, Ed2).
    """

    name: str
    desc: str | None = None
    server: Server | None = None
    server_at: str | None = None
    lns: list[LN] = field(default_factory=list)
    line: int | None = None


@dataclass(slots=True)
class ReportSettings:
    """``Services/ReportSettings``. Most values are ``Fix``/``Conf``/``Dyn``."""

    cb_name: str | None = None
    dat_set: str | None = None
    rpt_id: str | None = None
    opt_fields: str | None = None
    buf_time: str | None = None
    trg_ops: str | None = None
    intg_pd: str | None = None
    resv_tms: bool | None = None
    owner: bool | None = None


@dataclass(slots=True)
class ConfReportControl:
    """``Services/ConfReportControl``."""

    max: int | None = None
    buf_mode: str | None = None
    buf_conf: bool | None = None
    max_buf: int | None = None


@dataclass(slots=True)
class SettingGroupsService:
    """``Services/SettingGroups``: SGEdit / ConfSG capabilities."""

    sg_edit: bool = False
    sg_edit_resv_tms: bool | None = None
    conf_sg: bool = False
    conf_sg_resv_tms: bool | None = None


@dataclass(slots=True)
class Services:
    """``IED/Services``.

    ``raw`` maps every service element to its attributes; nested elements appear under
    ``"Parent/Child"`` keys (e.g. ``"SettingGroups/SGEdit"``). Presence of a key means the
    element is present (e.g. ``"GetDirectory" in services.raw``).
    """

    report_settings: ReportSettings | None = None
    conf_report_control: ConfReportControl | None = None
    setting_groups: SettingGroupsService | None = None
    raw: dict[str, dict[str, str]] = field(default_factory=dict)

    def has(self, name: str) -> bool:
        return name in self.raw


@dataclass(slots=True)
class IED:
    """``IED``. ``original_scl_*`` come from Ed2.1 SCDs that integrate older IEDs."""

    name: str
    type: str | None = None
    manufacturer: str | None = None
    config_version: str | None = None
    desc: str | None = None
    original_scl_version: str | None = None
    original_scl_revision: str | None = None
    original_scl_release: str | None = None
    services: Services = field(default_factory=Services)
    access_points: list[AccessPoint] = field(default_factory=list)
    line: int | None = None

    @property
    def original_scl(self) -> str | None:
        """Combined original SCL version, e.g. ``"2003"``, ``"2007B"``, ``"2007B4"``."""
        if not self.original_scl_version:
            return None
        return (
            f"{self.original_scl_version}{self.original_scl_revision or ''}"
            f"{self.original_scl_release or ''}"
        )

    def access_point(self, name: str | None = None) -> AccessPoint | None:
        """The named access point; with no name, the first one that has (or points at) a server."""
        if name is not None:
            return next((ap for ap in self.access_points if ap.name == name), None)
        for ap in self.access_points:
            if ap.server is not None or ap.server_at:
                return ap
        return self.access_points[0] if self.access_points else None


# ---------------------------------------------------------------------------
# DataTypeTemplates
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class DODef:
    """A ``DO`` inside an ``LNodeType``."""

    name: str
    type: str
    transient: bool = False
    desc: str | None = None
    line: int | None = None


@dataclass(slots=True)
class DADef:
    """A ``DA`` inside a ``DOType``, or a ``BDA`` inside a ``DAType`` (``fc`` is None for BDAs).

    ``count`` is the array size (0 = not an array). An Ed2 ``count`` that names another
    attribute is kept in ``count_ref`` and ``count`` stays 0.
    """

    name: str
    b_type: str
    type: str | None = None
    fc: str | None = None
    count: int = 0
    count_ref: str | None = None
    dchg: bool = False
    qchg: bool = False
    dupd: bool = False
    val_kind: str | None = None
    val_import: bool | None = None
    s_addr: str | None = None
    vals: list[Val] = field(default_factory=list)
    desc: str | None = None
    is_bda: bool = False
    line: int | None = None


@dataclass(slots=True)
class SDODef:
    """An ``SDO`` inside a ``DOType``."""

    name: str
    type: str
    count: int = 0
    desc: str | None = None
    line: int | None = None


@dataclass(slots=True)
class LNodeType:
    """``LNodeType``."""

    id: str
    ln_class: str
    dos: list[DODef] = field(default_factory=list)
    desc: str | None = None
    ied_type: str | None = None
    line: int | None = None


@dataclass(slots=True)
class DOType:
    """``DOType``. ``children`` interleaves DAs and SDOs in document order."""

    id: str
    cdc: str
    children: list[DADef | SDODef] = field(default_factory=list)
    desc: str | None = None
    ied_type: str | None = None
    line: int | None = None

    @property
    def das(self) -> list[DADef]:
        return [c for c in self.children if isinstance(c, DADef)]

    @property
    def sdos(self) -> list[SDODef]:
        return [c for c in self.children if isinstance(c, SDODef)]


@dataclass(slots=True)
class DAType:
    """``DAType``."""

    id: str
    bdas: list[DADef] = field(default_factory=list)
    desc: str | None = None
    ied_type: str | None = None
    line: int | None = None


@dataclass(slots=True)
class EnumType:
    """``EnumType``. ``values`` maps ord → literal text, in document order."""

    id: str
    values: dict[int, str] = field(default_factory=dict)
    desc: str | None = None
    line: int | None = None

    def ord_of(self, text: str) -> int | None:
        """The ord whose literal is ``text`` (exact match first, then case-insensitive)."""
        for o, t in self.values.items():
            if t == text:
                return o
        low = text.lower()
        for o, t in self.values.items():
            if t.lower() == low:
                return o
        return None


@dataclass(slots=True)
class DataTypeTemplates:
    """``DataTypeTemplates``, indexed by id (first definition wins on duplicates)."""

    lnode_types: dict[str, LNodeType] = field(default_factory=dict)
    do_types: dict[str, DOType] = field(default_factory=dict)
    da_types: dict[str, DAType] = field(default_factory=dict)
    enum_types: dict[str, EnumType] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Document
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SclDocument:
    """A parsed SCL file (SCD, CID, ICD, IID, SSD ...).

    ``scl_version`` is the schema version of the file: ``"2003"`` (no version attributes,
    Ed1), ``"2007A"``, ``"2007B"`` (Ed2) or ``"2007B4"`` (Ed2.1, ``release`` ≥ 4).
    ``warnings`` collects everything odd that did not stop parsing, as ``file:line: text``.
    """

    source: str
    scl_version: str
    version: str | None = None
    revision: str | None = None
    release: str | None = None
    header: Header = field(default_factory=Header)
    subnetworks: list[SubNetwork] = field(default_factory=list)
    ieds: list[IED] = field(default_factory=list)
    templates: DataTypeTemplates = field(default_factory=DataTypeTemplates)
    warnings: list[str] = field(default_factory=list)

    @property
    def ied_names(self) -> list[str]:
        return [ied.name for ied in self.ieds]

    def ied(self, name: str) -> IED | None:
        """The IED called ``name``, or None."""
        return next((i for i in self.ieds if i.name == name), None)

    def connected_aps(self, ied_name: str | None = None) -> list[ConnectedAP]:
        """All ConnectedAPs (optionally only those of one IED), in document order."""
        return [
            cap
            for sn in self.subnetworks
            for cap in sn.connected_aps
            if ied_name is None or cap.ied_name == ied_name
        ]

    def connected_ap(self, ied_name: str, ap_name: str | None = None) -> ConnectedAP | None:
        """The ConnectedAP of an IED access point (IP address etc.).

        With no ``ap_name``: prefer the access point that carries the IED's MMS server and
        has an IP address, then any ConnectedAP with an IP address, then the first one.
        """
        caps = self.connected_aps(ied_name)
        if ap_name is not None:
            return next((c for c in caps if c.ap_name == ap_name), None)
        if not caps:
            return None
        ied = self.ied(ied_name)
        server_ap = ied.access_point() if ied is not None else None
        if server_ap is not None:
            for c in caps:
                if c.ap_name == server_ap.name and c.ip:
                    return c
        for c in caps:
            if c.ip:
                return c
        return caps[0]
