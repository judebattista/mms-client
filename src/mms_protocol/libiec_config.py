"""Export an expected server model as a libiec61850 model config file (used by the test simulator).

The output is the text format read by libiec61850 v1.6.1
``ConfigFileParser_createModelFromConfigFileEx()``. This is an independent implementation
written from the grammar below (not derived from libiec61850's own generator)::

    MODEL(<iedName>){
    LD(<ldInst>[ <ldName>]){
    LN(<lnName>){
    SG(<actSG> <numOfSGs>)                                  # LLN0 only, before the DOs
    DO(<name> <arrayCount>){                                 # SDOs are nested DO(...){...}
    DA(<name> <arrayCount> <type> <fc> <trgOps> <sAddr>)[=<value>];
    DA(<name> <arrayCount> 27 <fc> <trgOps> <sAddr>){ ...nested DA lines... }
    }
    DS(<dsName>){
    DE(<lnName>$<FC>$<DO>[$<DA>...]);                        # other LD: DE(<ldInst>/<ln>$...)
    }                                                        # array element: DE(<ref> <index>)
    RC(<name> <rptId|-> <buffered> <datSet|-> <confRev> <trgOps> <optFlds> <bufTm> <intgPd>);
    LC(<name> <datSet|-> <logRef|-> <trgOps> <intgPd> <logEna> <reasonCode>);
    LOG(<logName>);
    GC(<name> <appId> <datSet> <confRev> <fixedOffs> <minTime> <maxTime>);
    }
    }
    }

Array attributes are followed by one line per element: ``[i]=value;`` / ``[i];`` for basic
types, ``[i]{`` ... ``}`` for constructed ones. Lines carry no indentation (the parser matches
line prefixes).

Encoding rules:

* ``<type>`` from :data:`LIBIEC_TYPES` (bType → libiec61850 ``DataAttributeType``), ``<fc>`` from
  ``ied_client.codes.FCS``.
* DA ``<trgOps>``: dchg=1, qchg=2, dupd=4, plus 128 when the DO is transient.
* RC ``<trgOps>``: dchg=1, qchg=2, dupd=4, period=8, gi=16, plus 64 when the IED declares
  ``Services/ReportSettings@owner="true"`` (libiec61850's "RCB has Owner" flag).
* RC ``<optFlds>``: seqNum=1, timeStamp=2, reasonCode=4, dataSet=8, dataRef=16, bufOvfl=32,
  entryID=64, configRef=128.
* ``<sAddr>`` is the SCL sAddr when it is a plain unsigned number, else 0.
* Values: booleans only when true (``=1``); integers and enums as numbers (enum literals
  resolved through the EnumType); floats as decimals; visible/unicode strings and Currency as
  ``="text"`` (double quotes replaced by single quotes, line breaks by spaces, truncated to
  the type's maximum length). Values of other types (bit strings, Quality, Check, Dbpos,
  Tcmd, times, octet strings) are not supported by the parser and are omitted.
* Indexed RCBs are written as their instances (``brcbA01`` ...), as in ``ExpectedServer.rcbs``.
* ``ldName`` is written only when it differs from ``iedName + ldInst``.
* Members from another LD are written as ``<ldInst>/...``. libiec61850 1.6.1 resolves those
  as ``iedName + ldInst``, so a cross-LD member is left out when either LD has an ldName.
* Dataset members that do not resolve are left out (libiec61850 cannot build them); an RCB
  pointing at a missing dataset is kept as is, so a simulator reproduces that defect.
* GoCBs without a dataset, SV control blocks, and attributes whose bType libiec61850 cannot
  model are left out. Each omission is reported through ``warnings``.
"""

from __future__ import annotations

import math
import re

from ied_client.codes import FCS
from ied_client.scl.btypes import btype_info
from ied_client.scl.expected import (
    TRG_OPT_TRANSIENT,
    ExpectedAttribute,
    ExpectedDataSet,
    ExpectedDO,
    ExpectedServer,
)

from .names import mms_item

#: libiec61850 ``DataAttributeType`` number of each bType it can model (INT24 has none of its own and
#: is written as INT32; Octet16 as OCTET_STRING_64, the closest available; SvOptFlds / LogOptFlds
#: only exist inside SV and log control blocks and have none).
LIBIEC_TYPES: dict[str, int] = {
    "BOOLEAN": 0,
    "INT8": 1,
    "INT16": 2,
    "INT24": 3,
    "INT32": 3,
    "INT64": 4,
    "INT128": 5,
    "INT8U": 6,
    "INT16U": 7,
    "INT24U": 8,
    "INT32U": 9,
    "FLOAT32": 10,
    "FLOAT64": 11,
    "Enum": 12,
    "Octet64": 13,
    "Octet6": 14,
    "Octet16": 13,
    "EntryID": 15,
    "VisString32": 16,
    "VisString64": 17,
    "VisString65": 18,
    "VisString129": 19,
    "ObjRef": 19,
    "VisString255": 20,
    "Unicode255": 21,
    "Timestamp": 22,
    "Quality": 23,
    "Check": 24,
    "Dbpos": 25,
    "Tcmd": 25,
    "Struct": 27,
    "EntryTime": 28,
    "PhyComAddr": 29,
    "Currency": 30,
    "OptFlds": 31,
    "TrgOps": 32,
}

#: libiec61850 DataAttributeType number for constructed attributes.
LIBIEC_CONSTRUCTED = 27


def libiec_type_of(b_type: str) -> int | None:
    """libiec61850 ``DataAttributeType`` number, or None if libiec61850 cannot model it."""
    return LIBIEC_TYPES.get(b_type)


_INT32 = (-(2**31), 2**31 - 1)
_UINT32 = (0, 2**32 - 1)
_RC_OWNER_FLAG = 64
_NAME_MAX = 129  # the parser reads names with %129s


def _token(text: str) -> str:
    """A whitespace-free token for the sscanf-based parser."""
    return re.sub(r"\s+", "_", text.strip())


class _Writer:
    def __init__(self, srv: ExpectedServer, warnings: list[str] | None) -> None:
        self.srv = srv
        self.lines: list[str] = []
        self.warnings = warnings if warnings is not None else []

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def out(self, line: str) -> None:
        self.lines.append(line)

    # -- structure --------------------------------------------------------------
    def run(self) -> str:
        srv = self.srv
        implied_logs = self._implied_logs()
        self.out(f"MODEL({srv.ied_name}){{")
        for ld in srv.lds:
            if self._has_ld_name(ld.inst):
                self.out(f"LD({ld.inst} {ld.ld_name}){{")
            else:
                self.out(f"LD({ld.inst}){{")
            for ln in ld.lns:
                self.out(f"LN({ln.name}){{")
                if ln.sgcb is not None:
                    if ln.is_ln0:
                        self.out(f"SG({ln.sgcb.act_sg} {ln.sgcb.num_of_sgs})")
                    else:
                        self.warn(f"{ln.ref}: setting group control outside LLN0 left out")
                for d in ln.dos:
                    self.do(d)
                for ds in ln.datasets:
                    self.dataset(ds)
                for rcb in ln.rcbs:
                    rpt_id = _token(rcb.rpt_id) if rcb.rpt_id else "-"
                    if len(rpt_id) > _NAME_MAX:
                        self.warn(f"{rcb.ref}: rptID longer than {_NAME_MAX} characters truncated")
                        rpt_id = rpt_id[:_NAME_MAX]
                    trg = rcb.trg_ops | (_RC_OWNER_FLAG if rcb.has_owner else 0)
                    self.out(f"RC({rcb.name} {rpt_id} {int(rcb.buffered)} {rcb.dat_set_name or '-'} "
                             f"{rcb.conf_rev or 0} {trg} {rcb.opt_fields_int} {rcb.buf_time} {rcb.intg_pd});")
                for lcb in ln.lcbs:
                    d = lcb.details
                    log_ref = d.get("logRef") or "-"
                    self.out(f"LC({lcb.name} {lcb.dat_set_name or '-'} {log_ref} {d.get('trgOps', 0)} "
                             f"{d.get('intgPd', 0)} {int(bool(d.get('logEna')))} {int(bool(d.get('reasonCode')))});")
                for log in [*ln.logs, *implied_logs.get((ld.inst, ln.name), [])]:
                    self.out(f"LOG({log});")
                for gc in ln.gocbs:
                    if not gc.dat_set_name:
                        self.warn(f"{gc.ref}: GoCB without dataset left out")
                        continue
                    d = gc.details
                    app_id = _token(str(d.get("appID") or gc.name))
                    min_t = d.get("minTime")
                    max_t = d.get("maxTime")
                    self.out(f"GC({gc.name} {app_id} {gc.dat_set_name} {d.get('confRev') or 0} "
                             f"{int(bool(d.get('fixedOffs')))} {min_t if min_t is not None else -1} "
                             f"{max_t if max_t is not None else -1});")
                for sv in ln.svcbs:
                    self.warn(f"{sv.ref}: sampled value control blocks are not exported")
                self.out("}")
            self.out("}")
        self.out("}")
        return "\n".join(self.lines) + "\n"

    def _implied_logs(self) -> dict[tuple[str, str], list[str]]:
        """Logs named by LCBs but not declared with a ``Log`` element (normal in Ed1 files)."""
        declared = {(ld.inst, ln.name, name) for ld in self.srv.lds for ln in ld.lns for name in ln.logs}
        implied: dict[tuple[str, str], list[str]] = {}
        for lcb in self.srv.lcbs:
            name = lcb.details.get("logName")
            ld_inst = lcb.details.get("logLD")
            ln_name = lcb.details.get("logLN")
            if not name or not isinstance(ld_inst, str) or not isinstance(ln_name, str):
                continue
            if self.srv.ld(ld_inst) is None or self.srv.ld(ld_inst).ln(ln_name) is None:  # type: ignore[union-attr]
                continue
            if (ld_inst, ln_name, name) in declared:
                continue
            lst = implied.setdefault((ld_inst, ln_name), [])
            if name not in lst:
                lst.append(str(name))
        return implied

    def do(self, d: ExpectedDO) -> None:
        if d.count:
            self.warn(f"{d.ref}: data object array exported as declared (count={d.count}); not verified")
        self.out(f"DO({d.name} {d.count}){{")
        for c in d.children:
            if isinstance(c, ExpectedDO):
                self.do(c)
            else:
                self.da(c)
        self.out("}")

    def da(self, a: ExpectedAttribute) -> None:
        fc = FCS.get(a.fc)
        if fc is None:
            self.warn(f"{a.ref}: unknown FC {a.fc!r}; attribute left out")
            return
        trg = a.trigger_options_int | (TRG_OPT_TRANSIENT if a.transient else 0)
        s_addr = int(a.s_addr) if a.s_addr and a.s_addr.strip().isdigit() and int(a.s_addr) < 2**32 else 0
        if a.is_constructed:
            if not a.children:
                self.warn(f"{a.ref}: constructed attribute without components left out")
                return
            head = f"DA({a.name} {a.count} {LIBIEC_CONSTRUCTED} {fc} {trg} {s_addr}){{"
            self.out(head)
            if a.count:
                for i in range(a.count):
                    self.out(f"[{i}]{{")
                    for c in a.children:
                        self.da(c)
                    self.out("}")
            else:
                for c in a.children:
                    self.da(c)
            self.out("}")
            return
        libiec_type = libiec_type_of(a.b_type)
        if libiec_type is None:
            self.warn(f"{a.ref}: bType {a.b_type!r} cannot be modelled in libiec61850; attribute left out")
            return
        head = f"DA({a.name} {a.count} {libiec_type} {fc} {trg} {s_addr})"
        if a.count:
            self.out(head + "{")
            for i in range(a.count):
                text = a.element_values.get(i)
                v = self.value(a, text) if text is not None else None
                self.out(f"[{i}]={v};" if v is not None else f"[{i}];")
            self.out("}")
            return
        v = self.value(a, a.value) if a.value is not None else None
        self.out(f"{head}={v};" if v is not None else f"{head};")

    # -- values -------------------------------------------------------------------
    def value(self, a: ExpectedAttribute, text: str) -> str | None:
        info = btype_info(a.b_type)
        if info is None:
            return None
        kind = info.value_kind
        if kind == "bool":
            low = text.strip().lower()
            if low not in ("true", "1", "false", "0"):
                self.warn(f"{a.ref}: boolean value {text!r} not exported")
                return None
            return "1" if low in ("true", "1") else None
        if kind in ("int", "uint", "enum"):
            if kind == "enum":
                n = self._enum_ord(a, text)
            else:
                try:
                    n = int(text.strip(), 0)
                except ValueError:
                    n = None
            if n is None:
                self.warn(f"{a.ref}: value {text!r} not exported")
                return None
            lo, hi = _UINT32 if kind == "uint" else _INT32
            if not lo <= n <= hi:
                self.warn(f"{a.ref}: value {n} outside the parser's range; not exported")
                return None
            return str(n)
        if kind == "float":
            try:
                f = float(text.strip())
            except ValueError:
                f = math.nan
            if not math.isfinite(f):
                self.warn(f"{a.ref}: float value {text!r} not exported")
                return None
            return repr(f)
        if kind == "string":
            s = re.sub(r"[\r\n\t]+", " ", text).replace('"', "'")
            size = info.value_type.size
            if size is not None and len(s) > size:
                self.warn(f"{a.ref}: string value truncated to {size} characters")
                s = s[:size]
            return f'"{s}"'
        return None

    @staticmethod
    def _enum_ord(a: ExpectedAttribute, text: str) -> int | None:
        values = a.enum_values or {}
        for o, t in values.items():
            if t == text:
                return o
        for o, t in values.items():
            if t.lower() == text.strip().lower():
                return o
        try:
            return int(text.strip())
        except ValueError:
            return None

    def _has_ld_name(self, ld_inst: str) -> bool:
        """True if the LD's domain is an ldName (i.e. not simply iedName + ldInst)."""
        ld = self.srv.ld(ld_inst)
        return ld is not None and ld.domain != f"{self.srv.ied_name}{ld.inst}"

    def dataset(self, ds: ExpectedDataSet) -> None:
        self.out(f"DS({ds.name}){{")
        for m in ds.members:
            if not m.resolved:
                self.warn(f"{ds.ref}: member {m.ref} [{m.fc}] does not resolve; left out")
                continue
            var = mms_item(m.object_ref)
            if m.ld_inst != ds.ld_inst:
                if self._has_ld_name(m.ld_inst) or self._has_ld_name(ds.ld_inst):
                    # libiec61850 1.6.1 resolves cross-LD members as iedName + ldInst: it crashes
                    # or reports the wrong domain when either LD has an ldName.
                    self.warn(f"{ds.ref}: cross-LD member {m.ref} [{m.fc}] involves an LD with an "
                              "ldName, which libiec61850 cannot model; left out")
                    continue
                var = f"{m.ld_inst}/{var}"
            if m.fcda.ix is not None:
                var = f"{var} {m.fcda.ix}"
            self.out(f"DE({var});")
        self.out("}")


def to_libiec61850_config(expected: ExpectedServer, *, warnings: list[str] | None = None) -> str:
    """The libiec61850 v1.6.1 model config text for ``expected`` (see module docstring).

    Anything that had to be left out or changed is appended to ``warnings`` if a list is
    given. The result can be loaded with ``ConfigFileParser_createModelFromConfigFileEx``.
    """
    return _Writer(expected, warnings).run()
