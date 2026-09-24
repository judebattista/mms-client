"""The expected server model of one IED, as an MMS client will see it (output of ``expand_server``).

Naming used throughout:

``domain``
    MMS domain name of a logical device: ``ldName`` if the SCL gives one (Ed2), else
    ``iedName + ldInst``.
``ref``
    IEC 61850 object reference, ``LD/LN.DO[.SDO].DA[.BDA]`` with ``LD`` = domain.
``mms_path``
    MMS item name inside the domain, ``LN$FC$DO[$SDO]$DA[$BDA]``.
``mms_ref``
    ``domain/mms_path``.

The tree (``ExpectedServer.lds`` → ``ExpectedLN`` → ``ExpectedDO`` → ``ExpectedAttribute``)
keeps the DO/SDO/DA/BDA distinction and the CDC of each DO/SDO, which a live device does not
reveal over MMS. ``ExpectedServer.attributes`` is the same attributes as a flat list
(pre-order, document order), including constructed attributes.

For FC ``SG`` attributes an ``SE`` copy is added next to each one (unless the DOType already
declares the SE attribute explicitly), because servers expose the edit buffer under FC SE.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field

from mms_client.scl.btypes import MmsType
from mms_client.scl.edition import EditionInfo
from mms_client.scl.model import (
    FCDA,
    ClientLN,
    ConnectedAP,
    ReportControl,
    Services,
)

#: DA trigger option bits (libiec61850 TRG_OPT_*), also used for RCB TrgOps.
TRG_OPT_BITS: dict[str, int] = {"dchg": 1, "qchg": 2, "dupd": 4, "period": 8, "gi": 16}
#: libiec61850 bit for "DO is transient" in a DA's trigger options.
TRG_OPT_TRANSIENT = 128
#: RCB OptFlds bits (libiec61850 RPT_OPT_*), keyed by the names used in ``codes.OPT_FLDS``.
OPT_FLDS_BITS: dict[str, int] = {
    "seqNum": 1,
    "timeStamp": 2,
    "reasonCode": 4,
    "dataSet": 8,
    "dataRef": 16,
    "bufOvfl": 32,
    "entryID": 64,
    "confRev": 128,
}


@dataclass(slots=True, eq=False)
class ExpectedAttribute:
    """A data attribute (DA) or sub-attribute (BDA), basic or constructed.

    ``value`` is the configured ``Val`` text (template ``Val`` overridden by DOI/SDI/DAI);
    ``parsed_value`` the same converted to Python (bool/int/float/str; the ord for Enums;
    raw text for bit strings, times and octet strings). ``values_by_sg`` holds per-setting-
    group values, and ``value`` is then the one for the active group. ``trigger_options`` is
    the set of ``dchg``/``qchg``/``dupd`` (inherited by BDAs from their DA).
    """

    name: str
    ref: str
    fc: str
    b_type: str
    mms_type: MmsType
    domain: str
    ln_name: str
    do_path: tuple[str, ...]
    da_path: tuple[str, ...]
    type_id: str | None = None
    enum_values: dict[int, str] | None = None
    count: int = 0
    trigger_options: frozenset[str] = frozenset()
    transient: bool = False
    value: str | None = None
    value_source: str | None = None
    parsed_value: bool | int | float | str | None = None
    values_by_sg: dict[int, str] = field(default_factory=dict)
    element_values: dict[int, str] = field(default_factory=dict)
    s_addr: str | None = None
    val_kind: str | None = None
    desc: str | None = None
    children: list[ExpectedAttribute] = field(default_factory=list)

    @property
    def is_constructed(self) -> bool:
        return self.b_type == "Struct" or bool(self.children)

    @property
    def is_leaf(self) -> bool:
        return not self.is_constructed

    @property
    def mms_path(self) -> str:
        return "$".join((self.ln_name, self.fc, *self.do_path, *self.da_path))

    @property
    def mms_ref(self) -> str:
        return f"{self.domain}/{self.mms_path}"

    @property
    def do_ref(self) -> str:
        """Reference of the (S)DO that holds this attribute."""
        return f"{self.domain}/{self.ln_name}." + ".".join(self.do_path)

    @property
    def trigger_options_int(self) -> int:
        """dchg=1, qchg=2, dupd=4 (libiec61850 numbering)."""
        return sum(TRG_OPT_BITS[n] for n in self.trigger_options)

    @property
    def enum_type(self) -> str | None:
        return self.type_id if self.b_type == "Enum" else None

    def child(self, name: str) -> ExpectedAttribute | None:
        return next((c for c in self.children if c.name == name), None)

    def walk(self) -> Iterator[ExpectedAttribute]:
        """This attribute and all sub-attributes, pre-order."""
        yield self
        for c in self.children:
            yield from c.walk()


@dataclass(slots=True, eq=False)
class ExpectedDO:
    """A data object or sub data object (``is_sdo``) with its CDC.

    ``children`` interleaves SDOs and DAs in DOType order (SE copies follow their SG DA).
    """

    name: str
    ref: str
    cdc: str
    type_id: str
    domain: str
    ln_name: str
    do_path: tuple[str, ...]
    is_sdo: bool = False
    transient: bool = False
    count: int = 0
    desc: str | None = None
    children: list[ExpectedDO | ExpectedAttribute] = field(default_factory=list)

    @property
    def sdos(self) -> list[ExpectedDO]:
        return [c for c in self.children if isinstance(c, ExpectedDO)]

    @property
    def attributes(self) -> list[ExpectedAttribute]:
        """Direct DAs (not those of SDOs)."""
        return [c for c in self.children if isinstance(c, ExpectedAttribute)]

    @property
    def fcs(self) -> set[str]:
        """Every FC used by this DO's attributes, including those of its SDOs."""
        return {a.fc for a in self.walk_attributes()}

    def child(self, name: str, fc: str | None = None) -> ExpectedDO | ExpectedAttribute | None:
        """Direct child (SDO or DA) by name; with ``fc``, only DAs with that FC match."""
        for c in self.children:
            if c.name != name:
                continue
            if fc is None or (isinstance(c, ExpectedAttribute) and c.fc == fc):
                return c
        return None

    def walk_attributes(self) -> Iterator[ExpectedAttribute]:
        """All attributes below this DO (SDOs included), pre-order."""
        for c in self.children:
            if isinstance(c, ExpectedDO):
                yield from c.walk_attributes()
            else:
                yield from c.walk()

    def walk_dos(self) -> Iterator[ExpectedDO]:
        yield self
        for c in self.children:
            if isinstance(c, ExpectedDO):
                yield from c.walk_dos()


@dataclass(slots=True, eq=False)
class ExpectedFCDA:
    """One dataset member, resolved against the expanded model.

    ``resolved`` is False when the member does not exist in the model (``problem`` says why);
    ``target`` is then None. ``ref`` / ``mms_ref`` are built from the FCDA even if unresolved.
    """

    fcda: FCDA
    ld_inst: str
    domain: str
    ln_name: str
    fc: str
    ref: str
    mms_ref: str
    resolved: bool
    problem: str | None = None
    target: ExpectedDO | ExpectedAttribute | None = None

    @property
    def mms_path(self) -> str:
        return self.mms_ref.split("/", 1)[1]


@dataclass(slots=True, eq=False)
class ExpectedDataSet:
    """A dataset. MMS name ``LN$name`` in the LD's domain."""

    name: str
    domain: str
    ld_inst: str
    ln_name: str
    members: list[ExpectedFCDA] = field(default_factory=list)
    desc: str | None = None
    line: int | None = None

    @property
    def ref(self) -> str:
        return f"{self.domain}/{self.ln_name}.{self.name}"

    @property
    def mms_name(self) -> str:
        return f"{self.ln_name}${self.name}"

    @property
    def mms_ref(self) -> str:
        return f"{self.domain}/{self.mms_name}"

    @property
    def resolved(self) -> bool:
        return all(m.resolved for m in self.members)


@dataclass(slots=True, eq=False)
class ExpectedRCB:
    """One report control block *instance* as the device exposes it.

    Instances: if the ReportControl is ``indexed`` (default true) and ``RptEnabled max`` =
    N > 1, the instances are ``name01`` .. ``nameNN``; otherwise the single instance ``name``.
    ``clients`` are the ClientLN assignments of this instance: with N indexed instances,
    ClientLN number i (document order) is assigned to instance i (the common convention;
    the standard does not fix it); a single instance gets all ClientLNs.

    ``trg_ops`` uses dchg=1, qchg=2, dupd=4, period=8, gi=16 (libiec61850 numbering).
    ``opt_fields`` uses the names of ``codes.OPT_FLDS`` (SCL ``configRef`` is ``confRev``),
    plus ``segmentation`` if set; ``bufOvfl`` and ``entryID`` are dropped for unbuffered RCBs
    (they only apply to BRCBs). ``opt_fields_int`` is the libiec61850 RPT_OPT_* value.
    ``dat_set`` is the DatSet value as the device reports it (``LD/LN$DS``).
    """

    name: str
    base_name: str
    index: int | None
    domain: str
    ld_inst: str
    ln_name: str
    buffered: bool
    rpt_id: str | None
    dat_set_name: str | None
    dat_set: str | None
    conf_rev: int | None
    trg_ops: int
    trg_ops_names: frozenset[str]
    opt_fields: frozenset[str]
    opt_fields_int: int
    buf_time: int
    intg_pd: int
    indexed: bool
    rpt_enabled_max: int
    clients: list[ClientLN] = field(default_factory=list)
    dataset: ExpectedDataSet | None = None
    has_owner: bool = False
    resv_tms: bool | None = None
    desc: str | None = None
    control: ReportControl | None = None
    line: int | None = None

    @property
    def fc(self) -> str:
        return "BR" if self.buffered else "RP"

    @property
    def mms_path(self) -> str:
        return f"{self.ln_name}${self.fc}${self.name}"

    @property
    def mms_ref(self) -> str:
        """e.g. ``BCU1CTRL/LLN0$BR$brcbA01``."""
        return f"{self.domain}/{self.mms_path}"

    @property
    def ref(self) -> str:
        """IEC-style reference with the FC, e.g. ``BCU1CTRL/LLN0.BR.brcbA01``."""
        return f"{self.domain}/{self.ln_name}.{self.fc}.{self.name}"

    @property
    def effective_rpt_id(self) -> str:
        """The configured rptID, or the RCB reference (``LD/LN$BR$name``) when none is set.

        IEC 61850-7-2 says a missing RptID means the RCB reference is used; libiec61850 reports
        the MMS-style reference. Other devices may format it differently.
        """
        return self.rpt_id or self.mms_ref

    @property
    def dat_set_ref(self) -> str | None:
        """Dataset reference in IEC notation (``LD/LN.DS``)."""
        if not self.dat_set_name:
            return None
        return f"{self.domain}/{self.ln_name}.{self.dat_set_name}"

    @property
    def dataset_exists(self) -> bool:
        return self.dat_set_name is None or self.dataset is not None


@dataclass(slots=True, eq=False)
class ExpectedSGCB:
    """Setting group control block (``LLN0$SP$SGCB``)."""

    domain: str
    ld_inst: str
    num_of_sgs: int
    act_sg: int
    resv_tms: int | None = None
    line: int | None = None

    @property
    def ref(self) -> str:
        return f"{self.domain}/LLN0.SGCB"

    @property
    def mms_ref(self) -> str:
        return f"{self.domain}/LLN0$SP$SGCB"


@dataclass(slots=True, eq=False)
class ExpectedControlBlock:
    """LCB / GoCB / SVCB, kept for structure comparison only.

    ``kind`` is ``LCB`` (FC LG), ``GoCB`` (GO), ``MSVCB`` (MS) or ``USVCB`` (US).
    ``details`` holds the remaining SCL attributes (appID, confRev, logName, address, ...).
    """

    kind: str
    name: str
    domain: str
    ld_inst: str
    ln_name: str
    fc: str
    dat_set_name: str | None = None
    details: dict[str, object] = field(default_factory=dict)
    line: int | None = None

    @property
    def ref(self) -> str:
        return f"{self.domain}/{self.ln_name}.{self.fc}.{self.name}"

    @property
    def mms_ref(self) -> str:
        return f"{self.domain}/{self.ln_name}${self.fc}${self.name}"


@dataclass(slots=True, eq=False)
class ExpectedLN:
    """A logical node with its data objects and control blocks."""

    name: str
    ln_class: str
    prefix: str
    inst: str
    ln_type: str
    domain: str
    ld_inst: str
    is_ln0: bool = False
    desc: str | None = None
    dos: list[ExpectedDO] = field(default_factory=list)
    datasets: list[ExpectedDataSet] = field(default_factory=list)
    rcbs: list[ExpectedRCB] = field(default_factory=list)
    lcbs: list[ExpectedControlBlock] = field(default_factory=list)
    gocbs: list[ExpectedControlBlock] = field(default_factory=list)
    svcbs: list[ExpectedControlBlock] = field(default_factory=list)
    sgcb: ExpectedSGCB | None = None
    logs: list[str] = field(default_factory=list)

    @property
    def ref(self) -> str:
        return f"{self.domain}/{self.name}"

    def do(self, name: str) -> ExpectedDO | None:
        return next((d for d in self.dos if d.name == name), None)

    def dataset(self, name: str) -> ExpectedDataSet | None:
        return next((d for d in self.datasets if d.name == name), None)


@dataclass(slots=True, eq=False)
class ExpectedLD:
    """A logical device (MMS domain)."""

    inst: str
    domain: str
    ld_name: str | None = None
    desc: str | None = None
    lns: list[ExpectedLN] = field(default_factory=list)

    def ln(self, name: str) -> ExpectedLN | None:
        return next((n for n in self.lns if n.name == name), None)


@dataclass(slots=True, eq=False)
class ExpectedServer:
    """The server model of one IED access point, as an MMS client will see it."""

    ied_name: str
    ap_name: str
    source: str
    edition: EditionInfo
    manufacturer: str | None = None
    ied_type: str | None = None
    config_version: str | None = None
    desc: str | None = None
    services: Services = field(default_factory=Services)
    connected_ap: ConnectedAP | None = None
    lds: list[ExpectedLD] = field(default_factory=list)
    attributes: list[ExpectedAttribute] = field(default_factory=list)
    datasets: list[ExpectedDataSet] = field(default_factory=list)
    rcbs: list[ExpectedRCB] = field(default_factory=list)
    sgcbs: list[ExpectedSGCB] = field(default_factory=list)
    lcbs: list[ExpectedControlBlock] = field(default_factory=list)
    gocbs: list[ExpectedControlBlock] = field(default_factory=list)
    svcbs: list[ExpectedControlBlock] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    _attr_index: dict[tuple[str, str], ExpectedAttribute] = field(default_factory=dict, repr=False)
    _do_index: dict[str, ExpectedDO] = field(default_factory=dict, repr=False)

    # -- convenience ---------------------------------------------------------
    @property
    def ip(self) -> str | None:
        return self.connected_ap.ip if self.connected_ap else None

    @property
    def domains(self) -> list[str]:
        return [ld.domain for ld in self.lds]

    def ld(self, name: str) -> ExpectedLD | None:
        """LD by MMS domain name, or by ldInst."""
        for ld in self.lds:
            if ld.domain == name:
                return ld
        return next((ld for ld in self.lds if ld.inst == name), None)

    def ln(self, ref: str) -> ExpectedLN | None:
        """LN by ``LD/LN`` (LD = domain or ldInst)."""
        ld_name, _, ln_name = ref.partition("/")
        ld = self.ld(ld_name)
        return ld.ln(ln_name) if ld else None

    def do(self, ref: str) -> ExpectedDO | None:
        """DO or SDO by reference ``LD/LN.DO[.SDO]``."""
        return self._do_index.get(ref)

    def attribute(self, ref: str, fc: str | None = None) -> ExpectedAttribute | None:
        """Attribute by reference ``LD/LN.DO[.SDO].DA[.BDA]``; with no ``fc``, the first match.

        The same reference can exist under two FCs (SG and its SE copy); pass ``fc`` to pick.
        """
        if fc is not None:
            return self._attr_index.get((ref, fc))
        return next((a for a in self.attributes if a.ref == ref), None)

    def attributes_for(self, ref: str) -> list[ExpectedAttribute]:
        """Every attribute with this reference (one per FC)."""
        return [a for a in self.attributes if a.ref == ref]

    def find(self, ref: str) -> ExpectedLD | ExpectedLN | ExpectedDO | ExpectedAttribute | None:
        """Whatever node ``ref`` names: LD, ``LD/LN``, DO/SDO or attribute."""
        if "/" not in ref:
            return self.ld(ref)
        if "." not in ref:
            return self.ln(ref)
        return self.do(ref) or self.attribute(ref)

    def dataset(self, ref: str) -> ExpectedDataSet | None:
        """Dataset by ``LD/LN.DS``, ``LD/LN$DS`` or bare name (first match)."""
        for ds in self.datasets:
            if ref in (ds.ref, ds.mms_ref, ds.name):
                return ds
        return None

    def rcb(self, name: str) -> ExpectedRCB | None:
        """RCB instance by instance name, ``mms_ref`` or ``ref``."""
        for r in self.rcbs:
            if name in (r.name, r.mms_ref, r.ref):
                return r
        return None

    def rcbs_of(self, base_name: str) -> list[ExpectedRCB]:
        """All instances of one ReportControl."""
        return [r for r in self.rcbs if r.base_name == base_name]

    def walk_dos(self) -> Iterator[ExpectedDO]:
        for ld in self.lds:
            for ln in ld.lns:
                for d in ln.dos:
                    yield from d.walk_dos()

    def iter_lns(self) -> Iterator[ExpectedLN]:
        for ld in self.lds:
            yield from ld.lns
