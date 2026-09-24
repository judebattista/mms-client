"""SCL parser: ``load_scl()`` turns an SCL file into an :class:`~mms_client.scl.model.SclDocument`.

Handles the 2003 (Ed1), 2007A/2007B (Ed2) and 2007B4 (Ed2.1) schemas, including mixed-edition
SCDs (per-IED ``originalSclVersion`` / ``originalSclRevision`` / ``originalSclRelease``).

Robustness rules:

* Parsing uses expat directly (stdlib), so every element keeps its line number for messages.
* All editions use the namespace ``http://www.iec.ch/61850/2003/SCL``; un-namespaced SCL
  (seen in some hand-written files) is accepted with a warning. Elements in any other
  namespace, and ``Private`` / ``Text`` elements, are skipped with their content.
* Byte-order marks, comments, processing instructions and a DOCTYPE are ignored.
* A file that claims UTF-8 but contains Windows-1252 bytes is decoded as Windows-1252,
  with a warning.
* Missing optional attributes get their schema defaults. Anything merely odd (unparseable
  number, duplicate type id, dangling type reference) becomes a warning on
  ``SclDocument.warnings``; only unusable input (not XML, not SCL) raises
  :class:`~mms_client.scl.errors.SclError`. Dangling type references raise later, in
  :func:`~mms_client.scl.expand.expand_server`, when the model actually needs them.

Schema defaults applied: ``ReportControl`` ``buffered=false``, ``bufTime=0``,
``indexed=true``, ``intgPd=0``; ``RptEnabled max=1``; ``TrgOps gi=true`` (other trigger
options false); ``OptFields bufOvfl=true`` in 2007 documents and false in 2003 documents
(other option fields false); ``LogControl logEna=true``, ``reasonCode=true``;
``SettingControl actSG=1``; ``GSEControl type=GOOSE``; ``SampledValueControl multicast=true``.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from pathlib import Path
from xml.parsers import expat

from mms_client.scl.errors import SclError, location
from mms_client.scl.model import (
    DAI,
    DOI,
    FCDA,
    IED,
    LN,
    SDI,
    AccessPoint,
    ClientLN,
    ConfReportControl,
    ConnectedAP,
    ControlBlockAddress,
    DADef,
    DataSet,
    DataTypeTemplates,
    DAType,
    DODef,
    DOType,
    EnumType,
    GSEControl,
    Header,
    LDevice,
    LNodeType,
    LogControl,
    ReportControl,
    ReportSettings,
    SampledValueControl,
    SclDocument,
    SDODef,
    Server,
    Services,
    SettingControl,
    SettingGroupsService,
    SubNetwork,
    Val,
)

SCL_NS = "http://www.iec.ch/61850/2003/SCL"

_SKIPPED_SCL_ELEMENTS = frozenset({"Private", "Text"})


# ---------------------------------------------------------------------------
# Minimal element tree with line numbers
# ---------------------------------------------------------------------------


class _Node:
    __slots__ = ("tag", "attrib", "children", "_text", "line")

    def __init__(self, tag: str, attrib: dict[str, str], line: int) -> None:
        self.tag = tag
        self.attrib = attrib
        self.children: list[_Node] = []
        self._text: list[str] = []
        self.line = line

    def get(self, name: str, default: str | None = None) -> str | None:
        return self.attrib.get(name, default)

    def find(self, tag: str) -> _Node | None:
        for c in self.children:
            if c.tag == tag:
                return c
        return None

    def findall(self, tag: str) -> list[_Node]:
        return [c for c in self.children if c.tag == tag]

    @property
    def text(self) -> str:
        return "".join(self._text).strip()


def _read_input(src: str | os.PathLike[str] | bytes | bytearray) -> tuple[bytes, str]:
    if isinstance(src, bytes | bytearray):
        return bytes(src), "<bytes>"
    if isinstance(src, str) and src.lstrip("﻿ \t\r\n").startswith("<"):
        return src.encode("utf-8"), "<string>"
    path = Path(src)
    try:
        return path.read_bytes(), str(path)
    except OSError as exc:
        raise SclError(f"cannot read SCL file: {exc.strerror or exc}", source=str(path)) from exc


def _xml_tree(data: bytes, source: str, warnings: list[str]) -> _Node:
    try:
        return _expat_parse(data, source, warnings)
    except expat.ExpatError as exc:
        # A common vendor defect: Windows-1252 text in a file declared (or defaulting to) UTF-8.
        if exc.code == expat.errors.codes[expat.errors.XML_ERROR_INVALID_TOKEN] and _declares_utf8(data):
            try:
                data.decode("utf-8")
            except UnicodeDecodeError:
                text = data.decode("cp1252", errors="replace")
                warnings.append(
                    f"{location(source, None)}file is not valid UTF-8; decoded as Windows-1252"
                )
                try:
                    return _expat_parse(text.encode("utf-8"), source, warnings)
                except expat.ExpatError as exc2:
                    exc = exc2
        raise SclError(f"XML error: {expat.errors.messages.get(exc.code, exc)}", source=source,
                       line=exc.lineno) from exc


_DECL_RE = re.compile(rb"^(?:\xef\xbb\xbf)?\s*<\?xml[^>]*encoding\s*=\s*[\"']([A-Za-z0-9._-]+)[\"']")


def _declares_utf8(data: bytes) -> bool:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return False
    m = _DECL_RE.match(data[:200])
    return m is None or m.group(1).decode("ascii").lower().replace("_", "-") in ("utf-8", "utf8")


def _expat_parse(data: bytes, source: str, warnings: list[str]) -> _Node:
    parser = expat.ParserCreate(namespace_separator="}")
    parser.buffer_text = True
    stack: list[_Node] = []
    root: list[_Node] = []
    skip_depth = 0
    no_ns_warned = False

    def start(name: str, attrs: dict[str, str]) -> None:
        nonlocal skip_depth, no_ns_warned
        if skip_depth:
            skip_depth += 1
            return
        ns, sep, local = name.rpartition("}")
        if not sep:
            ns, local = "", name
        if ns not in ("", SCL_NS) or local in _SKIPPED_SCL_ELEMENTS:
            if not stack:
                # Foreign root element: not SCL at all.
                raise SclError(f"root element is {{{ns}}}{local}, not SCL", source=source,
                               line=parser.CurrentLineNumber)
            skip_depth = 1
            return
        if ns == "" and not no_ns_warned:
            no_ns_warned = True
            warnings.append(
                f"{location(source, parser.CurrentLineNumber)}element <{local}> has no namespace "
                f"(expected {SCL_NS}); accepted"
            )
        node = _Node(local, {k: v for k, v in attrs.items() if "}" not in k}, parser.CurrentLineNumber)
        if stack:
            stack[-1].children.append(node)
        else:
            root.append(node)
        stack.append(node)

    def end(_name: str) -> None:
        nonlocal skip_depth
        if skip_depth:
            skip_depth -= 1
            return
        stack.pop()

    def chars(text: str) -> None:
        if not skip_depth and stack:
            stack[-1]._text.append(text)

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = chars
    parser.Parse(data, True)
    if not root:
        raise SclError("empty document", source=source)
    return root[0]


# ---------------------------------------------------------------------------
# Model builder
# ---------------------------------------------------------------------------

_TRUE = frozenset({"true", "1"})
_FALSE = frozenset({"false", "0"})


class _Builder:
    def __init__(self, source: str, warnings: list[str]) -> None:
        self.source = source
        self.warnings = warnings
        self.is_2003 = False

    # -- helpers -----------------------------------------------------------
    def warn(self, node: _Node | None, msg: str) -> None:
        self.warnings.append(f"{location(self.source, node.line if node else None)}{msg}")

    def s(self, node: _Node, name: str, default: str | None = None) -> str | None:
        v = node.get(name)
        return default if v is None else v

    def req(self, node: _Node, name: str) -> str:
        v = node.get(name)
        if v is None or v == "":
            self.warn(node, f"<{node.tag}> has no '{name}' attribute")
            return ""
        return v

    def b(self, node: _Node, name: str, default: bool) -> bool:
        v = node.get(name)
        if v is None or v.strip() == "":
            return default
        low = v.strip().lower()
        if low in _TRUE:
            return True
        if low in _FALSE:
            return False
        self.warn(node, f"<{node.tag}> {name}={v!r} is not a boolean; using {str(default).lower()}")
        return default

    def ob(self, node: _Node, name: str) -> bool | None:
        if node.get(name) is None:
            return None
        return self.b(node, name, False)

    def i(self, node: _Node, name: str, default: int | None = None) -> int | None:
        v = node.get(name)
        if v is None or v.strip() == "":
            return default
        try:
            return int(v.strip())
        except ValueError:
            self.warn(node, f"<{node.tag}> {name}={v!r} is not an integer; ignored")
            return default

    def text_int(self, node: _Node | None) -> int | None:
        if node is None or not node.text:
            return None
        try:
            return int(float(node.text))
        except ValueError:
            self.warn(node, f"<{node.tag}> {node.text!r} is not a number; ignored")
            return None

    def flags(self, node: _Node | None, names: tuple[str, ...], defaults: dict[str, bool]) -> frozenset[str]:
        out = set()
        for n in names:
            default = defaults.get(n, False)
            if (self.b(node, n, default) if node is not None else default):
                out.add(n)
        return frozenset(out)

    def vals(self, node: _Node) -> list[Val]:
        return [Val(v.text, self.i(v, "sGroup")) for v in node.findall("Val")]

    # -- document ----------------------------------------------------------
    def document(self, root: _Node) -> SclDocument:
        if root.tag != "SCL":
            raise SclError(f"root element is <{root.tag}>, not <SCL>", source=self.source, line=root.line)
        version = root.get("version")
        revision = root.get("revision")
        release = root.get("release")
        if version is None:
            scl_version = "2003"
        else:
            scl_version = f"{version}{revision or ''}{release or ''}"
            if version != "2007":
                self.warn(root, f"unknown SCL schema version {scl_version!r}; treated as 2007")
        self.is_2003 = scl_version == "2003"
        doc = SclDocument(source=self.source, scl_version=scl_version, version=version,
                          revision=revision, release=release, warnings=self.warnings)
        hdr = root.find("Header")
        if hdr is not None:
            doc.header = Header(id=hdr.get("id", "") or "", version=hdr.get("version"),
                                revision=hdr.get("revision"), tool_id=hdr.get("toolID"),
                                name_structure=hdr.get("nameStructure"))
        else:
            self.warn(root, "no <Header>")
        for comm in root.findall("Communication"):
            for sn in comm.findall("SubNetwork"):
                doc.subnetworks.append(self.subnetwork(sn))
        seen: set[str] = set()
        for ied_node in root.findall("IED"):
            ied = self.ied(ied_node)
            if ied.name in seen:
                self.warn(ied_node, f"duplicate IED name {ied.name!r}")
            seen.add(ied.name)
            doc.ieds.append(ied)
        dtt = root.find("DataTypeTemplates")
        if dtt is not None:
            doc.templates = self.templates(dtt)
        self.check_references(doc)
        return doc

    # -- Communication -----------------------------------------------------
    def address(self, node: _Node | None) -> dict[str, str]:
        out: dict[str, str] = {}
        if node is None:
            return out
        for p in node.findall("P"):
            t = p.get("type")
            if t:
                out[t] = p.text
        return out

    def subnetwork(self, node: _Node) -> SubNetwork:
        sn = SubNetwork(name=self.req(node, "name"), type=node.get("type"), desc=node.get("desc"),
                        line=node.line)
        for cap in node.findall("ConnectedAP"):
            c = ConnectedAP(ied_name=self.req(cap, "iedName"), ap_name=self.req(cap, "apName"),
                            subnetwork=sn.name, subnetwork_type=sn.type, desc=cap.get("desc"),
                            address=self.address(cap.find("Address")), line=cap.line)
            for kind in ("GSE", "SMV"):
                for g in cap.findall(kind):
                    cba = ControlBlockAddress(kind=kind, ld_inst=self.req(g, "ldInst"),
                                              cb_name=self.req(g, "cbName"),
                                              address=self.address(g.find("Address")), line=g.line)
                    if kind == "GSE":
                        cba.min_time = self.text_int(g.find("MinTime"))
                        cba.max_time = self.text_int(g.find("MaxTime"))
                        c.gse.append(cba)
                    else:
                        c.smv.append(cba)
            sn.connected_aps.append(c)
        return sn

    # -- IED ---------------------------------------------------------------
    def ied(self, node: _Node) -> IED:
        ied = IED(name=self.req(node, "name"), type=node.get("type"),
                  manufacturer=node.get("manufacturer"), config_version=node.get("configVersion"),
                  desc=node.get("desc"), original_scl_version=node.get("originalSclVersion"),
                  original_scl_revision=node.get("originalSclRevision"),
                  original_scl_release=node.get("originalSclRelease"), line=node.line)
        svc = node.find("Services")
        if svc is not None:
            ied.services = self.services(svc)
        for ap in node.findall("AccessPoint"):
            ied.access_points.append(self.access_point(ap))
        return ied

    def services(self, node: _Node) -> Services:
        svc = Services()

        def walk(n: _Node, prefix: str) -> None:
            for c in n.children:
                key = f"{prefix}{c.tag}"
                svc.raw.setdefault(key, dict(c.attrib))
                walk(c, key + "/")

        walk(node, "")
        rs = node.find("ReportSettings")
        if rs is not None:
            svc.report_settings = ReportSettings(
                cb_name=rs.get("cbName"), dat_set=rs.get("datSet"), rpt_id=rs.get("rptID"),
                opt_fields=rs.get("optFields"), buf_time=rs.get("bufTime"), trg_ops=rs.get("trgOps"),
                intg_pd=rs.get("intgPd"), resv_tms=self.ob(rs, "resvTms"), owner=self.ob(rs, "owner"))
        crc = node.find("ConfReportControl")
        if crc is not None:
            svc.conf_report_control = ConfReportControl(
                max=self.i(crc, "max"), buf_mode=crc.get("bufMode"), buf_conf=self.ob(crc, "bufConf"),
                max_buf=self.i(crc, "maxBuf"))
        sg = node.find("SettingGroups")
        if sg is not None:
            sge = sg.find("SGEdit")
            csg = sg.find("ConfSG")
            svc.setting_groups = SettingGroupsService(
                sg_edit=sge is not None, sg_edit_resv_tms=self.ob(sge, "resvTms") if sge is not None else None,
                conf_sg=csg is not None, conf_sg_resv_tms=self.ob(csg, "resvTms") if csg is not None else None)
        return svc

    def access_point(self, node: _Node) -> AccessPoint:
        ap = AccessPoint(name=self.req(node, "name"), desc=node.get("desc"), line=node.line)
        srv = node.find("Server")
        if srv is not None:
            ap.server = Server(timeout=self.i(srv, "timeout"), desc=srv.get("desc"), line=srv.line)
            for ld in srv.findall("LDevice"):
                ap.server.ldevices.append(self.ldevice(ld))
        sat = node.find("ServerAt")
        if sat is not None:
            ap.server_at = sat.get("apName")
        for ln in node.findall("LN"):
            ap.lns.append(self.ln(ln, is_ln0=False))
        return ap

    def ldevice(self, node: _Node) -> LDevice:
        ld = LDevice(inst=self.req(node, "inst"), ld_name=node.get("ldName") or None, desc=node.get("desc"),
                     line=node.line)
        ln0 = node.find("LN0")
        if ln0 is not None:
            ld.ln0 = self.ln(ln0, is_ln0=True)
        else:
            self.warn(node, f"LDevice {ld.inst!r} has no LN0")
        for ln in node.findall("LN"):
            ld.lns.append(self.ln(ln, is_ln0=False))
        return ld

    def ln(self, node: _Node, *, is_ln0: bool) -> LN:
        ln = LN(ln_class=self.req(node, "lnClass") if not is_ln0 else (node.get("lnClass") or "LLN0"),
                ln_type=self.req(node, "lnType"), inst=node.get("inst", "") or "",
                prefix=node.get("prefix", "") or "", desc=node.get("desc"), is_ln0=is_ln0, line=node.line)
        for c in node.children:
            tag = c.tag
            if tag == "DOI":
                ln.dois.append(self.doi(c))
            elif tag == "DataSet":
                ln.datasets.append(self.dataset(c))
            elif tag == "ReportControl":
                ln.report_controls.append(self.report_control(c))
            elif tag == "LogControl":
                ln.log_controls.append(self.log_control(c))
            elif tag == "GSEControl":
                ln.gse_controls.append(GSEControl(
                    name=self.req(c, "name"), dat_set=c.get("datSet") or None, conf_rev=self.i(c, "confRev"),
                    app_id=c.get("appID"), type=c.get("type", "GOOSE") or "GOOSE",
                    fixed_offs=self.b(c, "fixedOffs", False), desc=c.get("desc"), line=c.line))
            elif tag == "SampledValueControl":
                ln.smv_controls.append(SampledValueControl(
                    name=self.req(c, "name"), dat_set=c.get("datSet") or None, conf_rev=self.i(c, "confRev"),
                    smv_id=c.get("smvID"), multicast=self.b(c, "multicast", True),
                    smp_rate=self.i(c, "smpRate"), nof_asdu=self.i(c, "nofASDU"), desc=c.get("desc"),
                    line=c.line))
            elif tag == "SettingControl":
                if not is_ln0:
                    self.warn(c, "SettingControl outside LN0")
                if ln.setting_control is not None:
                    self.warn(c, "second SettingControl in the same LN ignored")
                    continue
                num = self.i(c, "numOfSGs")
                if num is None:
                    self.warn(c, "SettingControl has no numOfSGs; assuming 1")
                    num = 1
                ln.setting_control = SettingControl(num_of_sgs=num, act_sg=self.i(c, "actSG", 1) or 1,
                                                    resv_tms=self.i(c, "resvTms"), desc=c.get("desc"),
                                                    line=c.line)
            elif tag == "Log":
                ln.logs.append(c.get("name", "") or "")
        return ln

    def doi(self, node: _Node) -> DOI:
        d = DOI(name=self.req(node, "name"), ix=self.i(node, "ix"), desc=node.get("desc"), line=node.line)
        self._instance_children(node, d.sdis, d.dais)
        return d

    def _instance_children(self, node: _Node, sdis: list[SDI], dais: list[DAI]) -> None:
        for c in node.children:
            if c.tag == "SDI":
                sdi = SDI(name=c.get("name", "") or "", ix=self.i(c, "ix"), desc=c.get("desc"), line=c.line)
                self._instance_children(c, sdi.sdis, sdi.dais)
                sdis.append(sdi)
            elif c.tag == "DAI":
                dais.append(DAI(name=self.req(c, "name"), vals=self.vals(c), s_addr=c.get("sAddr"),
                                val_kind=c.get("valKind"), val_import=self.ob(c, "valImport"),
                                ix=self.i(c, "ix"), desc=c.get("desc"), line=c.line))

    def dataset(self, node: _Node) -> DataSet:
        ds = DataSet(name=self.req(node, "name"), desc=node.get("desc"), line=node.line)
        for f in node.findall("FCDA"):
            ds.fcdas.append(FCDA(ld_inst=f.get("ldInst", "") or "", prefix=f.get("prefix", "") or "",
                                 ln_class=f.get("lnClass", "") or "", ln_inst=f.get("lnInst", "") or "",
                                 do_name=f.get("doName", "") or "", da_name=f.get("daName", "") or "",
                                 fc=f.get("fc", "") or "", ix=self.i(f, "ix"), line=f.line))
        return ds

    def report_control(self, node: _Node) -> ReportControl:
        rc = ReportControl(name=self.req(node, "name"), rpt_id=node.get("rptID"),
                           dat_set=node.get("datSet") or None, conf_rev=self.i(node, "confRev"),
                           buffered=self.b(node, "buffered", False), buf_time=self.i(node, "bufTime", 0) or 0,
                           indexed=self.b(node, "indexed", True), intg_pd=self.i(node, "intgPd", 0) or 0,
                           desc=node.get("desc"), line=node.line)
        if rc.conf_rev is None:
            self.warn(node, f"ReportControl {rc.name!r} has no confRev")
        rc.trg_ops = self.flags(node.find("TrgOps"), ("dchg", "qchg", "dupd", "period", "gi"), {"gi": True})
        opt_node = node.find("OptFields")
        rc.opt_fields = self.flags(opt_node, ("seqNum", "timeStamp", "dataSet", "reasonCode", "dataRef",
                                              "entryID", "configRef", "bufOvfl", "segmentation"),
                                   {"bufOvfl": not self.is_2003})
        re_node = node.find("RptEnabled")
        if re_node is not None:
            rc.has_rpt_enabled = True
            mx = self.i(re_node, "max", 1)
            if mx is None or mx < 1:
                self.warn(re_node, f"RptEnabled max={re_node.get('max')!r} is invalid; using 1")
                mx = 1
            rc.rpt_enabled_max = mx
            for cl in re_node.findall("ClientLN"):
                rc.client_lns.append(ClientLN(
                    ied_name=self.req(cl, "iedName"), ln_class=self.req(cl, "lnClass"),
                    ld_inst=cl.get("ldInst", "") or "", prefix=cl.get("prefix", "") or "",
                    ln_inst=cl.get("lnInst", "") or "", ap_ref=cl.get("apRef") or None,
                    desc=cl.get("desc"), line=cl.line))
        return rc

    def log_control(self, node: _Node) -> LogControl:
        lc = LogControl(name=self.req(node, "name"), dat_set=node.get("datSet") or None,
                        log_name=node.get("logName") or None, log_ena=self.b(node, "logEna", True),
                        reason_code=self.b(node, "reasonCode", True), intg_pd=self.i(node, "intgPd", 0) or 0,
                        buf_time=self.i(node, "bufTime", 0) or 0, ld_inst=node.get("ldInst") or None,
                        prefix=node.get("prefix", "") or "", ln_class=node.get("lnClass") or None,
                        ln_inst=node.get("lnInst", "") or "", desc=node.get("desc"), line=node.line)
        lc.trg_ops = self.flags(node.find("TrgOps"), ("dchg", "qchg", "dupd", "period", "gi"), {"gi": True})
        return lc

    # -- DataTypeTemplates -------------------------------------------------
    def _count(self, node: _Node) -> tuple[int, str | None]:
        v = node.get("count")
        if v is None or v.strip() == "":
            return 0, None
        try:
            n = int(v)
            if n < 0:
                raise ValueError
            return n, None
        except ValueError:
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", v.strip()):
                return 0, v.strip()
            self.warn(node, f"<{node.tag}> count={v!r} is invalid; treated as not an array")
            return 0, None

    def _dadef(self, node: _Node, *, is_bda: bool) -> DADef:
        count, count_ref = self._count(node)
        b_type = self.req(node, "bType")
        da = DADef(name=self.req(node, "name"), b_type=b_type, type=node.get("type") or None,
                   fc=None if is_bda else (node.get("fc") or None), count=count, count_ref=count_ref,
                   dchg=self.b(node, "dchg", False), qchg=self.b(node, "qchg", False),
                   dupd=self.b(node, "dupd", False), val_kind=node.get("valKind"),
                   val_import=self.ob(node, "valImport"), s_addr=node.get("sAddr"), vals=self.vals(node),
                   desc=node.get("desc"), is_bda=is_bda, line=node.line)
        if not is_bda and da.fc is None:
            self.warn(node, f"DA {da.name!r} has no fc")
        if count_ref is not None:
            self.warn(node, f"<{node.tag}> {da.name!r} count refers to attribute {count_ref!r}; "
                            "variable-size arrays are treated as not an array")
        return da

    def templates(self, node: _Node) -> DataTypeTemplates:
        t = DataTypeTemplates()

        def put(table: dict, key: str, value: object, n: _Node) -> None:
            if key in table:
                self.warn(n, f"duplicate <{n.tag}> id {key!r}; first definition kept")
                return
            table[key] = value

        for n in node.children:
            if n.tag == "LNodeType":
                lt = LNodeType(id=self.req(n, "id"), ln_class=self.req(n, "lnClass"), desc=n.get("desc"),
                               ied_type=n.get("iedType"), line=n.line)
                for d in n.findall("DO"):
                    lt.dos.append(DODef(name=self.req(d, "name"), type=self.req(d, "type"),
                                        transient=self.b(d, "transient", False), desc=d.get("desc"),
                                        line=d.line))
                put(t.lnode_types, lt.id, lt, n)
            elif n.tag == "DOType":
                dt = DOType(id=self.req(n, "id"), cdc=self.req(n, "cdc"), desc=n.get("desc"),
                            ied_type=n.get("iedType"), line=n.line)
                for c in n.children:
                    if c.tag == "DA":
                        dt.children.append(self._dadef(c, is_bda=False))
                    elif c.tag == "SDO":
                        cnt, _ = self._count(c)
                        dt.children.append(SDODef(name=self.req(c, "name"), type=self.req(c, "type"),
                                                  count=cnt, desc=c.get("desc"), line=c.line))
                put(t.do_types, dt.id, dt, n)
            elif n.tag == "DAType":
                at = DAType(id=self.req(n, "id"), desc=n.get("desc"), ied_type=n.get("iedType"), line=n.line)
                for c in n.findall("BDA"):
                    at.bdas.append(self._dadef(c, is_bda=True))
                put(t.da_types, at.id, at, n)
            elif n.tag == "EnumType":
                et = EnumType(id=self.req(n, "id"), desc=n.get("desc"), line=n.line)
                for ev in n.findall("EnumVal"):
                    o = self.i(ev, "ord")
                    if o is None:
                        self.warn(ev, f"EnumVal in {et.id!r} has no valid ord; ignored")
                        continue
                    if o in et.values:
                        self.warn(ev, f"EnumType {et.id!r} repeats ord {o}")
                        continue
                    et.values[o] = ev.text
                put(t.enum_types, et.id, et, n)
        return t

    # -- reference checks (warnings only) -----------------------------------
    def check_references(self, doc: SclDocument) -> None:
        t = doc.templates

        def w(line: int | None, msg: str) -> None:
            self.warnings.append(f"{location(self.source, line)}{msg}")

        for lt in t.lnode_types.values():
            for d in lt.dos:
                if d.type and d.type not in t.do_types:
                    w(d.line, f"LNodeType {lt.id!r} DO {d.name!r}: unknown DOType {d.type!r}")
        for dt in t.do_types.values():
            for c in dt.children:
                if isinstance(c, SDODef):
                    if c.type and c.type not in t.do_types:
                        w(c.line, f"DOType {dt.id!r} SDO {c.name!r}: unknown DOType {c.type!r}")
                else:
                    self._check_da_type(t, f"DOType {dt.id!r} DA {c.name!r}", c, w)
        for at in t.da_types.values():
            for c in at.bdas:
                self._check_da_type(t, f"DAType {at.id!r} BDA {c.name!r}", c, w)
        for ied in doc.ieds:
            for ap in ied.access_points:
                lns = list(ap.lns)
                if ap.server is not None:
                    for ld in ap.server.ldevices:
                        lns.extend(ld.all_lns)
                for ln in lns:
                    if ln.ln_type and ln.ln_type not in t.lnode_types:
                        w(ln.line, f"IED {ied.name!r} LN {ln.name!r}: unknown LNodeType {ln.ln_type!r}")
                    elif ln.ln_type:
                        lt = t.lnode_types[ln.ln_type]
                        cls = "LLN0" if ln.is_ln0 else ln.ln_class
                        if lt.ln_class and lt.ln_class != cls:
                            w(ln.line, f"IED {ied.name!r} LN {ln.name!r} has lnClass {cls!r} but LNodeType "
                                       f"{lt.id!r} is for {lt.ln_class!r}")
            names = {ap.name for ap in ied.access_points}
            for ap in ied.access_points:
                if ap.server_at and ap.server_at not in names:
                    w(ap.line, f"IED {ied.name!r} AccessPoint {ap.name!r}: ServerAt unknown AP {ap.server_at!r}")

    @staticmethod
    def _check_da_type(t: DataTypeTemplates, what: str, c: DADef, w: Callable[[int | None, str], None]) -> None:
        if c.b_type == "Struct":
            if not c.type:
                w(c.line, f"{what}: bType Struct without type")
            elif c.type not in t.da_types:
                w(c.line, f"{what}: unknown DAType {c.type!r}")
        elif c.b_type == "Enum":
            if not c.type:
                w(c.line, f"{what}: bType Enum without type")
            elif c.type not in t.enum_types:
                w(c.line, f"{what}: unknown EnumType {c.type!r}")


def load_scl(src: str | os.PathLike[str] | bytes | bytearray) -> SclDocument:
    """Parse an SCL file (SCD, CID, ICD, IID, ...) of any edition.

    ``src`` is a path, raw file content as bytes, or XML text as a str (a str whose first
    non-blank character is ``<``). Raises :class:`SclError` (with file and line where
    known) if the input is not readable XML or not SCL; everything else that is odd is
    recorded in ``SclDocument.warnings``.
    """
    data, source = _read_input(src)
    warnings: list[str] = []
    root = _xml_tree(data, source, warnings)
    return _Builder(source, warnings).document(root)
