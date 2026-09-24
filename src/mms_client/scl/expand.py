"""Expansion of an IED's SCL description into the server model an MMS client will see.

``expand_server(doc, ied_name, ap_name=None)`` walks the IED's LDevices, instantiates every
LN from its LNodeType (DO → DOType → DA/SDO → DAType → BDA), applies instance data
(DOI/SDI/DAI values and short addresses), resolves dataset members and expands report
control blocks into the instances the device exposes. See :mod:`mms_client.scl.expected`
for the result and the naming conventions.

Broken type references (unknown lnType, DOType, DAType or EnumType id) raise
:class:`SclError` with file and line by default; with ``strict=False`` they become warnings
and the affected node is left out. Everything else that is odd (DOI naming a DO that does
not exist, an Enum value that is not in its EnumType, a dataset member that does not
resolve, more ClientLNs than RCB instances ...) is a warning in ``ExpectedServer.warnings``.
"""

from __future__ import annotations

from mms_client.codes import FCS
from mms_client.scl.btypes import MmsType, btype_info, mms_type_of
from mms_client.scl.edition import edition_of
from mms_client.scl.errors import SclError, location
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
from mms_client.scl.model import (
    DAI,
    FCDA,
    IED,
    LN,
    SDI,
    AccessPoint,
    DADef,
    DataSet,
    DOType,
    LDevice,
    ReportControl,
    SclDocument,
    SDODef,
    Val,
)

#: SCL OptFields name → name used in ``codes.OPT_FLDS`` / ``ExpectedRCB.opt_fields``.
_OPT_NAME = {
    "seqNum": "seqNum",
    "timeStamp": "timeStamp",
    "dataSet": "dataSet",
    "reasonCode": "reasonCode",
    "dataRef": "dataRef",
    "entryID": "entryID",
    "configRef": "confRev",
    "bufOvfl": "bufOvfl",
    "segmentation": "segmentation",
}
_BRCB_ONLY_OPT = frozenset({"bufOvfl", "entryID"})


def domain_name(ied_name: str, ld: LDevice) -> str:
    """MMS domain name of an LD: ``ldName`` if present (Ed2), else ``iedName + ldInst``."""
    return ld.ld_name or f"{ied_name}{ld.inst}"


def server_access_point(ied: IED, ap_name: str | None = None) -> AccessPoint:
    """The access point whose server to expand (following ``ServerAt``).

    Raises :class:`SclError` if there is no such access point or it has no server.
    """
    ap = ied.access_point(ap_name)
    if ap is None:
        what = f"access point {ap_name!r}" if ap_name else "an access point"
        raise SclError(f"IED {ied.name!r} has no {what}", line=ied.line)
    seen = {ap.name}
    target = ap
    while target.server is None and target.server_at:
        nxt = ied.access_point(target.server_at)
        if nxt is None or nxt.name in seen:
            break
        seen.add(nxt.name)
        target = nxt
    if target.server is None:
        raise SclError(f"IED {ied.name!r} access point {ap.name!r} has no Server", line=ap.line)
    return target


class _Expander:
    def __init__(self, doc: SclDocument, ied: IED, strict: bool) -> None:
        self.doc = doc
        self.t = doc.templates
        self.ied = ied
        self.strict = strict
        self.warnings: list[str] = []
        self.ld_by_inst: dict[str, ExpectedLD] = {}

    # -- messages --------------------------------------------------------------
    def warn(self, line: int | None, msg: str) -> None:
        self.warnings.append(f"{location(self.doc.source, line)}{msg}")

    def broken(self, line: int | None, msg: str) -> None:
        if self.strict:
            raise SclError(msg, source=self.doc.source, line=line)
        self.warn(line, msg)

    # -- entry -----------------------------------------------------------------
    def run(self, ap_name: str | None) -> ExpectedServer:
        ied = self.ied
        try:
            ap = server_access_point(ied, ap_name)
        except SclError as exc:
            raise SclError(exc.message, source=self.doc.source, line=exc.line) from None
        requested_ap = ied.access_point(ap_name) or ap
        server = ap.server
        assert server is not None
        out = ExpectedServer(
            ied_name=ied.name, ap_name=requested_ap.name, source=self.doc.source,
            edition=edition_of(self.doc, ied.name), manufacturer=ied.manufacturer, ied_type=ied.type,
            config_version=ied.config_version, desc=ied.desc, services=ied.services,
            connected_ap=self.doc.connected_ap(ied.name, requested_ap.name), warnings=self.warnings)

        pairs: list[tuple[LDevice, ExpectedLD]] = []
        for ld in server.ldevices:
            eld = ExpectedLD(inst=ld.inst, domain=domain_name(ied.name, ld), ld_name=ld.ld_name, desc=ld.desc)
            if ld.inst in self.ld_by_inst:
                self.warn(ld.line, f"duplicate LDevice inst {ld.inst!r}")
                continue
            self.ld_by_inst[ld.inst] = eld
            pairs.append((ld, eld))
            out.lds.append(eld)
            act_sg = ld.ln0.setting_control.act_sg if ld.ln0 and ld.ln0.setting_control else None
            for ln in ld.all_lns:
                eln = self.build_ln(eld, ln, act_sg)
                if eld.ln(eln.name) is not None:
                    self.warn(ln.line, f"duplicate LN {eln.ref}")
                eld.lns.append(eln)

        # Second pass: things that reference the model (datasets may span LDs).
        for ld, eld in pairs:
            for ln, eln in zip(ld.all_lns, eld.lns, strict=True):
                self.build_control_blocks(out, ld, eld, ln, eln)

        for eld in out.lds:
            for eln in eld.lns:
                for d in eln.dos:
                    for dd in d.walk_dos():
                        out._do_index.setdefault(dd.ref, dd)
                    for a in d.walk_attributes():
                        out.attributes.append(a)
                        out._attr_index.setdefault((a.ref, a.fc), a)
                out.datasets.extend(eln.datasets)
                out.rcbs.extend(eln.rcbs)
                out.lcbs.extend(eln.lcbs)
                out.gocbs.extend(eln.gocbs)
                out.svcbs.extend(eln.svcbs)
                if eln.sgcb is not None:
                    out.sgcbs.append(eln.sgcb)
        return out

    # -- data model --------------------------------------------------------------
    def build_ln(self, eld: ExpectedLD, ln: LN, act_sg: int | None) -> ExpectedLN:
        eln = ExpectedLN(name=ln.name, ln_class="LLN0" if ln.is_ln0 else ln.ln_class, prefix=ln.prefix,
                         inst=ln.inst, ln_type=ln.ln_type, domain=eld.domain, ld_inst=eld.inst,
                         is_ln0=ln.is_ln0, desc=ln.desc, logs=[n for n in ln.logs if n])
        lt = self.t.lnode_types.get(ln.ln_type)
        if lt is None:
            self.broken(ln.line, f"LN {eln.ref}: unknown LNodeType {ln.ln_type!r}")
            return eln
        for d in lt.dos:
            edo = self.build_do(eln, d.name, d.type, (d.name,), transient=d.transient, count=0, is_sdo=False,
                                line=d.line, desc=d.desc, parent_ref=eln.ref, stack=frozenset())
            if edo is not None:
                eln.dos.append(edo)
        for doi in ln.dois:
            edo = eln.do(doi.name)
            if edo is None:
                self.warn(doi.line, f"DOI {doi.name!r} in {eln.ref}: LNodeType {ln.ln_type!r} has no such DO")
                continue
            if doi.ix is not None:
                self.warn(doi.line, f"DOI {eln.ref}.{doi.name} ix={doi.ix}: DO array instance data not applied")
                continue
            self.apply_instance(edo, doi.sdis, doi.dais, act_sg)
        return eln

    def build_do(self, eln: ExpectedLN, name: str, type_id: str, do_path: tuple[str, ...], *, transient: bool,
                 count: int, is_sdo: bool, line: int | None, desc: str | None, parent_ref: str,
                 stack: frozenset[str]) -> ExpectedDO | None:
        ref = f"{parent_ref}.{name}"
        dt = self.t.do_types.get(type_id)
        if dt is None:
            self.broken(line, f"{ref}: unknown DOType {type_id!r}")
            return None
        if type_id in stack:
            self.broken(line, f"{ref}: DOType {type_id!r} contains itself")
            return None
        edo = ExpectedDO(name=name, ref=ref, cdc=dt.cdc, type_id=type_id, domain=eln.domain, ln_name=eln.name,
                         do_path=do_path, is_sdo=is_sdo, transient=transient, count=count, desc=desc)
        inner = stack | {type_id}
        for c in dt.children:
            if isinstance(c, SDODef):
                sub = self.build_do(eln, c.name, c.type, (*do_path, c.name), transient=transient, count=c.count,
                                    is_sdo=True, line=c.line, desc=c.desc, parent_ref=ref, stack=inner)
                if sub is not None:
                    edo.children.append(sub)
                continue
            if not c.fc:
                self.warn(c.line, f"{ref}.{c.name}: DA without fc left out")
                continue
            fcs = [c.fc]
            if c.fc == "SG" and not _declares(dt, c.name, "SE"):
                fcs.append("SE")
            trg = frozenset(n for n in ("dchg", "qchg", "dupd") if getattr(c, n))
            for fc in fcs:
                a = self.build_da(eln, c, fc, do_path, (c.name,), trg, transient, ref, frozenset())
                if a is not None:
                    edo.children.append(a)
        return edo

    def build_da(self, eln: ExpectedLN, d: DADef, fc: str, do_path: tuple[str, ...], da_path: tuple[str, ...],
                 trg: frozenset[str], transient: bool, parent_ref: str,
                 stack: frozenset[str]) -> ExpectedAttribute | None:
        ref = f"{parent_ref}.{d.name}"
        info = btype_info(d.b_type)
        if info is None:
            self.warn(d.line, f"{ref}: unknown bType {d.b_type!r}")
        a = ExpectedAttribute(name=d.name, ref=ref, fc=fc, b_type=d.b_type, mms_type=mms_type_of(d.b_type, d.count),
                              domain=eln.domain, ln_name=eln.name, do_path=do_path, da_path=da_path,
                              type_id=d.type, count=d.count, trigger_options=trg, transient=transient,
                              s_addr=d.s_addr, val_kind=d.val_kind, desc=d.desc)
        if d.b_type == "Struct":
            dat = self.t.da_types.get(d.type or "")
            if dat is None:
                self.broken(d.line, f"{ref}: unknown DAType {d.type!r}")
                return None
            if dat.id in stack:
                self.broken(d.line, f"{ref}: DAType {dat.id!r} contains itself")
                return None
            for bda in dat.bdas:
                child = self.build_da(eln, bda, fc, do_path, (*da_path, bda.name), trg, transient, ref,
                                      stack | {dat.id})
                if child is not None:
                    a.children.append(child)
            a.mms_type = MmsType("array", d.count, MmsType("structure")) if d.count else MmsType("structure")
        elif d.b_type == "Enum":
            et = self.t.enum_types.get(d.type or "")
            if et is None:
                self.broken(d.line, f"{ref}: unknown EnumType {d.type!r}")
                return None
            a.enum_values = dict(et.values)
        if d.vals:
            if a.is_constructed:
                self.warn(d.line, f"{ref}: Val on a constructed attribute ignored")
            else:
                self.set_values(a, d.vals, "template", None, d.line)
        return a

    def apply_instance(self, node: ExpectedDO | ExpectedAttribute, sdis: list[SDI], dais: list[DAI],
                       act_sg: int | None) -> None:
        for sdi in sdis:
            targets = [c for c in node.children if c.name == sdi.name]
            if not targets:
                self.warn(sdi.line, f"SDI {sdi.name!r}: {node.ref} has no such child")
                continue
            if sdi.ix is not None:
                self.warn(sdi.line, f"SDI {node.ref}.{sdi.name} ix={sdi.ix}: array element instance data not applied")
                continue
            for t in targets:
                self.apply_instance(t, sdi.sdis, sdi.dais, act_sg)
        for dai in dais:
            targets = [c for c in node.children if isinstance(c, ExpectedAttribute) and c.name == dai.name]
            if not targets:
                self.warn(dai.line, f"DAI {dai.name!r}: {node.ref} has no such attribute")
                continue
            for t in targets:
                if dai.s_addr is not None:
                    t.s_addr = dai.s_addr
                if dai.val_kind:
                    t.val_kind = dai.val_kind
                if not dai.vals:
                    continue
                if t.is_constructed:
                    self.warn(dai.line, f"DAI {t.ref}: Val on a constructed attribute ignored")
                elif dai.ix is not None:
                    t.element_values[dai.ix] = dai.vals[0].text
                else:
                    self.set_values(t, dai.vals, "instance", act_sg, dai.line)

    def set_values(self, a: ExpectedAttribute, vals: list[Val], source: str, act_sg: int | None,
                   line: int | None) -> None:
        plain = [v.text for v in vals if v.sgroup is None]
        by_sg = {v.sgroup: v.text for v in vals if v.sgroup is not None}
        if by_sg:
            a.values_by_sg = by_sg
        if plain:
            a.value = plain[0]
        elif by_sg:
            a.value = by_sg[act_sg] if act_sg in by_sg else by_sg[min(by_sg)]
        else:
            return
        a.value_source = source
        a.parsed_value = self.parse_value(a, a.value, line)

    def parse_value(self, a: ExpectedAttribute, text: str, line: int | None) -> bool | int | float | str | None:
        info = btype_info(a.b_type)
        kind = info.value_kind if info else "raw"
        try:
            if kind == "bool":
                low = text.strip().lower()
                if low in ("true", "1"):
                    return True
                if low in ("false", "0"):
                    return False
                raise ValueError
            if kind in ("int", "uint"):
                v = int(text.strip(), 0)
                if kind == "uint" and v < 0:
                    raise ValueError
                return v
            if kind == "float":
                return float(text.strip())
            if kind == "enum":
                return self.enum_ord(a, text, line)
        except ValueError:
            self.warn(line, f"{a.ref}: value {text!r} is not a valid {a.b_type}")
            return None
        return text

    def enum_ord(self, a: ExpectedAttribute, text: str, line: int | None) -> int | None:
        values = a.enum_values or {}
        for o, t in values.items():
            if t == text:
                return o
        for o, t in values.items():
            if t.lower() == text.strip().lower():
                return o
        try:
            o = int(text.strip())
        except ValueError:
            self.warn(line, f"{a.ref}: {text!r} is not a literal of EnumType {a.type_id!r}")
            return None
        if o not in values:
            self.warn(line, f"{a.ref}: numeric value {o} is not an ord of EnumType {a.type_id!r}")
        return o

    # -- datasets and control blocks -------------------------------------------------
    def build_control_blocks(self, out: ExpectedServer, ld: LDevice, eld: ExpectedLD, ln: LN,
                             eln: ExpectedLN) -> None:
        for ds in ln.datasets:
            if eln.dataset(ds.name) is not None:
                self.warn(ds.line, f"duplicate dataset {eln.ref}.{ds.name}")
            eln.datasets.append(self.build_dataset(eld, eln, ds))
        report_settings = self.ied.services.report_settings
        has_owner = bool(report_settings and report_settings.owner)
        resv_tms = report_settings.resv_tms if report_settings else None
        for rc in ln.report_controls:
            eln.rcbs.extend(self.build_rcbs(eld, eln, rc, has_owner, resv_tms))
        for lc in ln.log_controls:
            # The log lives in LN (ldInst, prefix, lnClass, lnInst) when given (Ed2), else in LLN0.
            log_ld = self.ld_by_inst.get(lc.ld_inst) if lc.ld_inst else eld
            log_ln = f"{lc.prefix}{lc.ln_class}{lc.ln_inst}" if lc.ln_class and lc.ln_class != "LLN0" else "LLN0"
            log_ref = None
            if lc.log_name:
                log_ref = f"{(log_ld.domain if log_ld else lc.ld_inst)}/{log_ln}${lc.log_name}"
            eln.lcbs.append(ExpectedControlBlock(
                kind="LCB", name=lc.name, domain=eld.domain, ld_inst=eld.inst, ln_name=eln.name, fc="LG",
                dat_set_name=lc.dat_set, line=lc.line,
                details={"logName": lc.log_name, "logRef": log_ref, "logEna": lc.log_ena,
                         "reasonCode": lc.reason_code, "intgPd": lc.intg_pd, "bufTime": lc.buf_time,
                         "trgOps": sum(TRG_OPT_BITS[n] for n in lc.trg_ops),
                         "logLD": log_ld.inst if log_ld else lc.ld_inst, "logLN": log_ln}))
            self._check_cb_dataset(eln, lc.dat_set, "LogControl", lc.name, lc.line)
        cap = out.connected_ap
        for gc in ln.gse_controls:
            addr = None
            if cap is not None:
                addr = next((g for g in cap.gse if g.ld_inst == eld.inst and g.cb_name == gc.name), None)
            eln.gocbs.append(ExpectedControlBlock(
                kind="GoCB", name=gc.name, domain=eld.domain, ld_inst=eld.inst, ln_name=eln.name, fc="GO",
                dat_set_name=gc.dat_set, line=gc.line,
                details={"appID": gc.app_id, "confRev": gc.conf_rev, "type": gc.type, "fixedOffs": gc.fixed_offs,
                         "address": dict(addr.address) if addr else {},
                         "minTime": addr.min_time if addr else None, "maxTime": addr.max_time if addr else None}))
            self._check_cb_dataset(eln, gc.dat_set, "GSEControl", gc.name, gc.line)
        for sv in ln.smv_controls:
            eln.svcbs.append(ExpectedControlBlock(
                kind="MSVCB" if sv.multicast else "USVCB", name=sv.name, domain=eld.domain, ld_inst=eld.inst,
                ln_name=eln.name, fc="MS" if sv.multicast else "US", dat_set_name=sv.dat_set, line=sv.line,
                details={"smvID": sv.smv_id, "confRev": sv.conf_rev, "smpRate": sv.smp_rate,
                         "nofASDU": sv.nof_asdu}))
            self._check_cb_dataset(eln, sv.dat_set, "SampledValueControl", sv.name, sv.line)
        sc = ln.setting_control
        if sc is not None and ln.is_ln0:
            if not 1 <= sc.act_sg <= sc.num_of_sgs:
                self.warn(sc.line, f"{eln.ref}: SettingControl actSG={sc.act_sg} outside 1..{sc.num_of_sgs}")
            eln.sgcb = ExpectedSGCB(domain=eld.domain, ld_inst=eld.inst, num_of_sgs=sc.num_of_sgs,
                                    act_sg=sc.act_sg, resv_tms=sc.resv_tms, line=sc.line)

    def _check_cb_dataset(self, eln: ExpectedLN, dat_set: str | None, what: str, name: str,
                          line: int | None) -> None:
        if dat_set and eln.dataset(dat_set) is None:
            self.warn(line, f"{what} {eln.ref}.{name} references missing dataset {dat_set!r}")

    def build_dataset(self, eld: ExpectedLD, eln: ExpectedLN, ds: DataSet) -> ExpectedDataSet:
        eds = ExpectedDataSet(name=ds.name, domain=eld.domain, ld_inst=eld.inst, ln_name=eln.name, desc=ds.desc,
                              line=ds.line)
        for f in ds.fcdas:
            m = self.resolve_fcda(eld, f)
            if not m.resolved:
                self.warn(f.line, f"dataset {eds.ref} member {m.ref} [{m.fc or '?'}] does not resolve: {m.problem}")
            eds.members.append(m)
        return eds

    def resolve_fcda(self, own_ld: ExpectedLD, f: FCDA) -> ExpectedFCDA:
        """Resolve one FCDA against the expanded model (see ``ExpectedFCDA``)."""
        ld_inst = f.ld_inst or own_ld.inst
        eld = self.ld_by_inst.get(ld_inst)
        domain = eld.domain if eld else f"{self.ied.name}{ld_inst}"
        ln_name = f.ln_name
        do_parts = [p for p in f.do_name.split(".") if p] if f.do_name else []
        da_parts = [p for p in f.da_name.split(".") if p] if f.da_name else []
        ref = f"{domain}/{ln_name}" + "".join(f".{p}" for p in (*do_parts, *da_parts))
        mms_ref = f"{domain}/{ln_name}${f.fc}" + "".join(f"${p}" for p in (*do_parts, *da_parts))
        m = ExpectedFCDA(fcda=f, ld_inst=ld_inst, domain=domain, ln_name=ln_name, fc=f.fc, ref=ref, mms_ref=mms_ref,
                         resolved=False)
        if not f.ld_inst:
            self.warn(f.line, f"FCDA {ref} has no ldInst; assumed {own_ld.inst!r}")
        if not f.fc:
            m.problem = "FCDA has no fc"
            return m
        if f.fc not in FCS:
            m.problem = f"unknown functional constraint {f.fc!r}"
            return m
        if eld is None:
            m.problem = f"no logical device with inst {ld_inst!r}"
            return m
        eln = eld.ln(ln_name)
        if eln is None:
            m.problem = f"no logical node {ln_name!r} in {domain}"
            return m
        if not do_parts:
            m.problem = "FCDA has no doName"
            return m
        node: ExpectedDO | ExpectedAttribute | None = eln.do(do_parts[0])
        path = f"{eln.ref}.{do_parts[0]}"
        if node is None:
            m.problem = f"{eln.ref} has no data object {do_parts[0]!r}"
            return m
        for p in do_parts[1:]:
            nxt = node.child(p) if isinstance(node, ExpectedDO) else None
            if not isinstance(nxt, ExpectedDO):
                m.problem = f"{path} has no sub data object {p!r}"
                return m
            node = nxt
            path += f".{p}"
        assert isinstance(node, ExpectedDO)
        if da_parts:
            first = node.child(da_parts[0], fc=f.fc)
            if first is None:
                other = node.child(da_parts[0])
                if isinstance(other, ExpectedAttribute):
                    m.problem = f"{path}.{da_parts[0]} exists with FC {other.fc}, not {f.fc}"
                else:
                    m.problem = f"{path} has no attribute {da_parts[0]!r} with FC {f.fc}"
                return m
            assert isinstance(first, ExpectedAttribute)
            attr: ExpectedAttribute = first
            path += f".{da_parts[0]}"
            for p in da_parts[1:]:
                nxt_a = attr.child(p)
                if nxt_a is None:
                    m.problem = f"{path} has no sub-attribute {p!r}"
                    return m
                attr = nxt_a
                path += f".{p}"
            node = attr
        elif not any(a.fc == f.fc for a in node.walk_attributes()):
            m.problem = f"{path} has no data with FC {f.fc}"
            return m
        if f.ix is not None:
            count = node.count
            if count <= 0:
                m.problem = f"ix={f.ix} but {path} is not an array"
                return m
            if not 0 <= f.ix < count:
                m.problem = f"ix={f.ix} outside array of {count}"
                return m
        m.resolved = True
        m.target = node
        return m

    def build_rcbs(self, eld: ExpectedLD, eln: ExpectedLN, rc: ReportControl, has_owner: bool,
                   resv_tms: bool | None) -> list[ExpectedRCB]:
        n = rc.rpt_enabled_max
        if rc.indexed and n > 1:
            names = [(f"{rc.name}{i:02d}", i) for i in range(1, n + 1)]
        else:
            names = [(rc.name, None)]
        clients = rc.client_lns
        if len(names) > 1 and len(clients) > len(names):
            self.warn(rc.line, f"ReportControl {eln.ref}.{rc.name}: {len(clients)} ClientLNs but only "
                               f"{len(names)} instances; extra ClientLNs are not assigned")
        if len(names) == 1 and len(clients) > 1:
            self.warn(rc.line, f"ReportControl {eln.ref}.{rc.name}: {len(clients)} ClientLNs share one "
                               "non-indexed instance")
        dataset = eln.dataset(rc.dat_set) if rc.dat_set else None
        if rc.dat_set and dataset is None:
            self.warn(rc.line, f"ReportControl {eln.ref}.{rc.name} references missing dataset {rc.dat_set!r}")
        opt = frozenset(_OPT_NAME[o] for o in rc.opt_fields)
        if not rc.buffered:
            opt -= _BRCB_ONLY_OPT
        opt_int = sum(OPT_FLDS_BITS[o] for o in opt if o in OPT_FLDS_BITS)
        trg_int = sum(TRG_OPT_BITS[t] for t in rc.trg_ops)
        out = []
        for idx, (name, index) in enumerate(names):
            if len(names) > 1:
                assigned = [clients[idx]] if idx < len(clients) else []
            else:
                assigned = list(clients)
            out.append(ExpectedRCB(
                name=name, base_name=rc.name, index=index, domain=eld.domain, ld_inst=eld.inst, ln_name=eln.name,
                buffered=rc.buffered, rpt_id=rc.rpt_id, dat_set_name=rc.dat_set,
                dat_set=f"{eld.domain}/{eln.name}${rc.dat_set}" if rc.dat_set else None, conf_rev=rc.conf_rev,
                trg_ops=trg_int, trg_ops_names=rc.trg_ops, opt_fields=opt, opt_fields_int=opt_int,
                buf_time=rc.buf_time, intg_pd=rc.intg_pd, indexed=rc.indexed, rpt_enabled_max=n,
                clients=assigned, dataset=dataset, has_owner=has_owner, resv_tms=resv_tms, desc=rc.desc,
                control=rc, line=rc.line))
        return out


def _declares(dt: DOType, name: str, fc: str) -> bool:
    return any(isinstance(c, DADef) and c.name == name and c.fc == fc for c in dt.children)


def expand_server(doc: SclDocument, ied_name: str | IED, ap_name: str | None = None, *,
                  strict: bool = True) -> ExpectedServer:
    """Expand one IED's server (on ``ap_name``, default: the first AP with a server).

    ``ied_name`` is the IED name (an :class:`IED` from ``doc.ieds`` is accepted too).

    Returns the :class:`ExpectedServer`: LD/LN/DO/DA tree, flat attribute list, datasets
    with resolved members, RCB instances, SGCBs and LCB/GoCB/SVCB names.

    Raises :class:`SclError` if the IED or access point does not exist or has no server,
    and (when ``strict``, the default) on broken type references. With ``strict=False``
    broken references become warnings and the affected LN/DO/DA is left out.
    """
    name = ied_name.name if isinstance(ied_name, IED) else ied_name
    ied = doc.ied(name)
    if ied is None:
        raise SclError(f"IED {name!r} not found (IEDs: {', '.join(doc.ied_names) or 'none'})",
                       source=doc.source)
    return _Expander(doc, ied, strict).run(ap_name)

