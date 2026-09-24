"""References for verification (VER-1 … VER-3, VER-8, RPT-6): SCD, CID, snapshot, or an SCL
file found on the device itself ("device-supplied": the device's claim about its own
configuration, not an independent reference).
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mms_client import codes
from mms_client.adapter import AccessError, MmsKind, ServiceError, VarSpec, value_to_json
from mms_client.core.model import ControlBlockInfo, DeviceModel, dataset_ref_to_mms
from mms_client.core.readwrite import values_equal
from mms_client.core.refs import ObjectRef, RefError
from mms_client.core.reports import RcbStatus, free_reason, list_rcbs, subscribe
from mms_client.core.results import Category, CheckResult, RawExchange, Status
from mms_client.core.session import Session
from mms_client.scl import (
    ExpectedServer,
    SclDocument,
    client_relationships,
    edition_of,
    expand_server,
    load_scl,
)

from .snapshot import Snapshot

CONF = Category.CONFIGURATION
COMM = Category.COMMUNICATION
VALUE_FCS = ("CF", "SP", "DC", "EX", "SG")
RP_IGNORED_OPT = 32 | 64  # bufOvfl / entryID are meaningless for unbuffered RCBs


def _r(check_id: str, title: str, status: Status, category: Category, **kw: Any) -> CheckResult:
    return CheckResult(check_id, title, status, category, **kw)


class ReferenceError(ValueError):
    pass


@dataclass
class Reference:
    kind: str  # "scd" | "cid" | "snapshot" | "device-supplied"
    path: Path
    ied_name: str | None = None
    doc: SclDocument | None = None
    expected: ExpectedServer | None = None
    snapshot: Snapshot | None = None
    notes: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------ construction
    @classmethod
    def load(cls, kind: str, path: str | Path, ied: str | None = None, *, live_model: DeviceModel | None = None, host: str | None = None) -> Reference:
        p = Path(path)
        if kind == "snapshot":
            return cls("snapshot", p, snapshot=Snapshot.load(p))
        if kind not in ("scd", "cid", "device-supplied"):
            raise ReferenceError(f"unknown reference kind {kind!r}")
        doc = load_scl(p)
        name = ied or _pick_ied(doc, live_model, host)
        if name is None:
            raise ReferenceError(f"{p}: several IEDs ({', '.join(doc.ied_names)}); say which one with --ied")
        ref = cls(kind, p, name, doc, expand_server(doc, name, strict=False))
        ref.notes.extend(doc.warnings[:20])
        return ref

    @classmethod
    def guess_kind(cls, path: str | Path) -> str:
        suffix = Path(path).suffix.lower()
        if suffix == ".json":
            return "snapshot"
        if suffix == ".scd":
            return "scd"
        return "cid"

    @classmethod
    def from_device(cls, session: Session, choose: str | None = None) -> Reference | None:
        """VER-8: look for SCL files on the device and use one as a (labelled) reference."""
        from mms_client.core.files import find_scl_files, get_file

        found = find_scl_files(session)
        if not found:
            return None
        names = [f.name for f in found]
        pick = choose
        if pick is None:
            if session.ui.interactive:
                pick = session.ui.choose(
                    "No reference was given. The device offers these SCL files. Use one as a "
                    "device-supplied reference (the device's own claim, not an independent check)?",
                    [(n, n) for n in names] + [("none", "Don't use any")],
                    default="none",
                )
            else:
                return None
        if pick in (None, "none") or pick not in names:
            return None
        tmp = Path(tempfile.mkdtemp(prefix="mms-client-scl-"))
        dl = get_file(session, pick, tmp / Path(pick).name, overwrite=True)
        ref = cls.load("device-supplied", dl.local, live_model=session.model(), host=session.target.host)
        ref.notes.append(f"downloaded from the device as {pick} (sha256 {dl.sha256[:16]}…)")
        return ref

    # ------------------------------------------------------------------ description / hooks
    @property
    def label(self) -> str:
        base = f"{self.kind} {self.path.name}"
        if self.ied_name:
            base += f" (IED {self.ied_name})"
        if self.kind == "device-supplied":
            base += " — device-supplied: the device's own claim, not an independent reference"
        return base

    def describe(self) -> dict[str, Any]:
        return {"kind": self.kind, "path": str(self.path), "ied": self.ied_name, "label": self.label, "notes": self.notes}

    def scl_edition(self) -> tuple[str, str] | None:
        if self.doc is None or self.ied_name is None:
            return None
        info = edition_of(self.doc, self.ied_name)
        return info.edition, info.reason

    def cdc_of(self, ref: ObjectRef) -> str | None:
        if self.expected is None:
            return None
        d = self.expected.do(ref.iec())
        return d.cdc if d is not None else None

    def _rcb(self, cb: ControlBlockInfo):
        if self.expected is None:
            return None
        return next((r for r in self.expected.rcbs if r.domain == cb.ld and r.ln_name == cb.ln and r.name == cb.name), None)

    def rcb_clients(self, cb: ControlBlockInfo) -> list[str]:
        r = self._rcb(cb)
        return [c.ied_name for c in r.clients] if r is not None else []

    def rcb_conf_rev(self, cb: ControlBlockInfo) -> int | None:
        r = self._rcb(cb)
        return r.conf_rev if r is not None else None

    # ------------------------------------------------------------------ checks
    def checks(self, session: Session, opts: Any) -> Iterator[CheckResult]:
        if self.kind == "device-supplied":
            yield _r("device-supplied-reference", "Reference origin", Status.INFO, CONF, message=self.label,
                     error=codes.check("device-supplied-reference"))
        if self.snapshot is not None:
            from .checks import against_snapshot

            yield from against_snapshot(session, self.snapshot, opts, self.label)
            return
        assert self.expected is not None
        yield from _scl_model_checks(session, self.expected, opts)
        rcbs = list_rcbs(session)
        yield from _scl_dataset_checks(session, self.expected)
        yield from _scl_rcb_checks(self.expected, rcbs)
        yield from _scl_value_checks(session, self.expected, opts)
        yield from _scl_identity_checks(session, self)
        yield from _communication_checks(session, self, rcbs, opts)


def _pick_ied(doc: SclDocument, live: DeviceModel | None, host: str | None) -> str | None:
    if len(doc.ieds) == 1:
        return doc.ieds[0].name
    if host:
        for name in doc.ied_names:
            cap = doc.connected_ap(name)
            if cap is not None and cap.ip == host:
                return name
    if live is not None:
        best, score = None, 0
        for name in doc.ied_names:
            try:
                exp = expand_server(doc, name, strict=False)
            except Exception:
                continue
            s = len(set(exp.domains) & set(live.lds))
            if s > score:
                best, score = name, s
        return best
    return None


# ------------------------------------------------------------------------------------ model
def _live_spec(model: DeviceModel, domain: str, ln: str, path: tuple[str, ...], fc: str) -> VarSpec | None:
    try:
        r = model.resolve(ObjectRef(domain, ln, path))
    except RefError:
        return None
    return r.node.specs.get(fc) if r.node is not None else None


def _type_matches(expected, live: VarSpec) -> tuple[bool, bool]:
    """(kind matches, size matches)."""
    mt = expected.mms_type
    if mt.kind in ("unknown", None):
        return True, True
    if mt.kind == "array":
        return live.kind is MmsKind.ARRAY, True
    try:
        kind_ok = MmsKind(mt.kind) is live.kind
    except ValueError:
        return True, True
    if not kind_ok or mt.size is None or live.size is None or expected.b_type == "Enum":
        return kind_ok, True
    return kind_ok, abs(live.size) == abs(mt.size)


def _scl_model_checks(session: Session, exp: ExpectedServer, opts: Any) -> Iterator[CheckResult]:
    model = session.model()
    title = "Device model matches the reference"
    missing = type_bad = size_bad = 0
    for ld in exp.lds:
        live_ld = model.lds.get(ld.domain)
        if live_ld is None:
            missing += 1
            yield _r("model-object-missing", title, Status.FAIL, CONF, subject=ld.domain, message=f"logical device {ld.domain} is missing",
                     error=codes.check("model-object-missing"), certainty="fact")
            continue
        for ln in ld.lns:
            live_ln = live_ld.lns.get(ln.name)
            if live_ln is None:
                missing += 1
                yield _r("model-object-missing", title, Status.FAIL, CONF, subject=f"{ld.domain}/{ln.name}",
                         message=f"logical node {ln.name} ({ln.ln_class}) is missing", error=codes.check("model-object-missing"))
                continue
            for do in ln.dos:
                if do.name not in live_ln.data:
                    missing += 1
                    yield _r("model-object-missing", title, Status.FAIL, CONF, subject=do.ref,
                             message=f"data object {do.name} ({do.cdc}) is missing", error=codes.check("model-object-missing"))
                    continue
                for a in do.walk_attributes():
                    if not a.is_leaf:
                        continue
                    path = (*a.do_path, *a.da_path)
                    live = _live_spec(model, a.domain, a.ln_name, path, a.fc)
                    if live is None:
                        other = _live_spec_any(model, a.domain, a.ln_name, path)
                        missing += 1
                        if other:
                            yield _r("model-fc-mismatch", title, Status.FAIL, CONF, subject=f"{a.ref} [{a.fc}]",
                                     message=f"exists under {', '.join(other)} instead of {a.fc}", error=codes.check("model-fc-mismatch"))
                        else:
                            yield _r("model-object-missing", title, Status.FAIL, CONF, subject=f"{a.ref} [{a.fc}]",
                                     message="attribute is missing", error=codes.check("model-object-missing"))
                        continue
                    kind_ok, size_ok = _type_matches(a, live)
                    if not kind_ok:
                        type_bad += 1
                        yield _r("model-type-mismatch", title, Status.FAIL, CONF, subject=f"{a.ref} [{a.fc}]",
                                 message=f"type {live.type_name()} on the device, {a.mms_type} ({a.b_type}) in the reference",
                                 error=codes.check("model-type-mismatch"))
                    elif not size_ok:
                        size_bad += 1
                        yield _r("model-type-mismatch", title, Status.WARN, CONF, subject=f"{a.ref} [{a.fc}]",
                                 message=f"size differs: {live.type_name()} on the device, {a.mms_type} in the reference",
                                 error=codes.check("model-type-mismatch"))
            # unexpected DOs
            expected_dos = {d.name for d in ln.dos}
            extra = [n for n in live_ln.data if n not in expected_dos]
            if extra:
                yield _r("model-object-unexpected", title, Status.WARN, CONF, subject=f"{ld.domain}/{ln.name}",
                         message=f"data objects not in the reference: {', '.join(extra)}", evidence={"extra": extra},
                         error=codes.check("model-object-unexpected"))
        extra_lns = [n for n in live_ld.lns if ld.ln(n) is None]
        if extra_lns:
            yield _r("model-object-unexpected", title, Status.WARN, CONF, subject=ld.domain,
                     message=f"logical nodes not in the reference: {', '.join(extra_lns)}", error=codes.check("model-object-unexpected"))
    extra_lds = [n for n in model.lds if exp.ld(n) is None]
    if extra_lds:
        yield _r("model-object-unexpected", title, Status.WARN, CONF, subject=", ".join(extra_lds),
                 message=f"logical devices not in the reference: {', '.join(extra_lds)}", error=codes.check("model-object-unexpected"))
    if not (missing or type_bad or size_bad):
        yield _r("model-structure", title, Status.PASS, CONF, message=f"{len(exp.attributes)} reference attributes found with matching FC and type")


def _live_spec_any(model: DeviceModel, domain: str, ln: str, path: tuple[str, ...]) -> list[str]:
    try:
        r = model.resolve(ObjectRef(domain, ln, path))
    except RefError:
        return []
    return r.node.fcs if r.node is not None else []


# ------------------------------------------------------------------------------------ datasets
def _norm_ds(ref: str | None) -> str | None:
    if not ref:
        return None
    ld, _, rest = ref.partition("/")
    return f"{ld}/{rest.replace('.', '$')}"


def _scl_dataset_checks(session: Session, exp: ExpectedServer) -> Iterator[CheckResult]:
    client = session.require_client()
    model = session.model()
    title = "Datasets match the reference"
    for ds in exp.datasets:
        domain, name = dataset_ref_to_mms(ds.mms_ref)
        if domain not in model.lds or name not in model.lds[domain].datasets:
            yield _r("dataset-mismatch", title, Status.FAIL, CONF, subject=ds.mms_ref, message="dataset is missing on the device",
                     error=codes.check("dataset-mismatch"), certainty="fact")
            continue
        try:
            members, _ = client.get_dataset_directory(domain, name)
        except ServiceError as e:
            yield _r("dataset-mismatch", title, Status.FAIL, CONF, subject=ds.mms_ref, error=e.error, message=str(e))
            continue
        live = [m.mms_ref() for m in members]
        want = [m.mms_ref for m in ds.members]
        raw = [RawExchange("get-dataset-directory", ds.mms_ref, response=live)]
        if live == want:
            yield _r("dataset-mismatch", title, Status.PASS, CONF, subject=ds.mms_ref, message=f"{len(live)} members match", raw=raw)
        else:
            missing = [m for m in want if m not in live]
            extra = [m for m in live if m not in want]
            msg = "member order differs" if not missing and not extra else ""
            if missing:
                msg += f"missing: {', '.join(missing)} "
            if extra:
                msg += f"not in reference: {', '.join(extra)}"
            yield _r("dataset-mismatch", title, Status.FAIL, CONF, subject=ds.mms_ref, message=msg.strip(),
                     evidence={"device": live, "reference": want}, raw=raw, error=codes.check("dataset-mismatch"))


# ------------------------------------------------------------------------------------ RCBs (RPT-6)
def _scl_rcb_checks(exp: ExpectedServer, rcbs: list[RcbStatus]) -> Iterator[CheckResult]:
    live = {(s.cb.ld, s.cb.ln, s.cb.name): s for s in rcbs}
    title = "Report control blocks match the reference"
    for r in exp.rcbs:
        st = live.get((r.domain, r.ln_name, r.name))
        if st is None or st.cb.kind != ("BRCB" if r.buffered else "URCB"):
            yield _r("rcb-attribute-mismatch", title, Status.FAIL, CONF, subject=r.ref,
                     message="RCB instance is missing" if st is None else "buffered/unbuffered type differs",
                     error=codes.check("rcb-attribute-mismatch"))
            continue
        v = st.values
        if v is None:
            yield _r("rcb-attribute-mismatch", title, Status.FAIL, CONF, subject=r.ref, error=st.error, message="RCB unreadable")
            continue
        raw = [RawExchange("get-rcb", r.ref, response=v.to_json())]
        # ConfRev (RPT-6)
        if r.conf_rev is not None:
            ok = v.conf_rev == r.conf_rev
            yield _r("confrev-mismatch", "ConfRev matches the reference", Status.PASS if ok else Status.FAIL, CONF, subject=r.ref,
                     message=f"ConfRev {v.conf_rev} on the device, {r.conf_rev} in the reference", raw=raw,
                     error=None if ok else codes.check("confrev-mismatch"), certainty="fact",
                     evidence={"device": v.conf_rev, "reference": r.conf_rev})
        in_use = bool(v.rpt_ena or v.resv or (v.resv_tms not in (None, 0)))
        diffs: list[tuple[str, Any, Any, bool]] = []  # (attr, device, reference, may be a runtime change)
        if _norm_ds(v.dataset) != _norm_ds(r.dat_set):
            diffs.append(("DatSet", v.dataset, r.dat_set, False))
        if r.rpt_id and v.rpt_id != r.rpt_id:
            diffs.append(("RptID", v.rpt_id, r.rpt_id, False))
        if (v.trg_ops or 0) & 31 != r.trg_ops & 31:
            diffs.append(("TrgOps", v.trg_ops, r.trg_ops, True))
        mask = 0xFF & ~(RP_IGNORED_OPT if not r.buffered else 0)
        if (v.opt_flds or 0) & mask != r.opt_fields_int & mask:
            diffs.append(("OptFlds", v.opt_flds, r.opt_fields_int, True))
        if v.buf_tm != r.buf_time:
            diffs.append(("BufTm", v.buf_tm, r.buf_time, True))
        if v.intg_pd != r.intg_pd:
            diffs.append(("IntgPd", v.intg_pd, r.intg_pd, True))
        if not diffs:
            yield _r("rcb-attribute-mismatch", title, Status.PASS, CONF, subject=r.ref, message="attributes match", raw=raw)
        for attr, dev, want, runtime in diffs:
            status = Status.WARN if (runtime and in_use) else Status.FAIL
            note = " (the RCB is in use: a client may have changed it at run time)" if runtime and in_use else ""
            yield _r("rcb-attribute-mismatch", title, status, CONF, subject=f"{r.ref}.{attr}",
                     message=f"{attr}: {dev!r} on the device, {want!r} in the reference{note}", raw=raw,
                     error=codes.check("rcb-attribute-mismatch"))


# ------------------------------------------------------------------------------------ configured values
def _scl_value_checks(session: Session, exp: ExpectedServer, opts: Any) -> Iterator[CheckResult]:
    client = session.require_client()
    model = session.model()
    title = "Configured values match the reference"
    # SGCB
    active: dict[str, int] = {}
    for sg in exp.sgcbs:
        from mms_client.core import setgroup

        states = {s.ld: s for s in setgroup.show(session)}
        st = states.get(sg.domain)
        if st is None:
            yield _r("config-value-mismatch", "Setting group control block matches", Status.FAIL, CONF, subject=sg.ref,
                     message="SGCB missing on the device", error=codes.check("config-value-mismatch"))
            continue
        num = st.values.get("NumOfSG")
        act = st.values.get("ActSG")
        if isinstance(act, int):
            active[sg.domain] = act
        yield _r("config-value-mismatch", "Setting group control block matches", Status.PASS if num == sg.num_of_sgs else Status.FAIL,
                 CONF, subject=f"{sg.ref}.NumOfSG", message=f"NumOfSG {num} on the device, {sg.num_of_sgs} in the reference")
        if act != sg.act_sg:
            yield _r("config-value-mismatch", "Setting group control block matches", Status.INFO, CONF, subject=f"{sg.ref}.ActSG",
                     message=f"active group {act} on the device, {sg.act_sg} in the reference")
    jobs: dict[str, list[tuple[Any, VarSpec, Any]]] = {}
    for a in exp.attributes:
        if not a.is_leaf or a.fc not in VALUE_FCS:
            continue
        want = a.parsed_value
        if a.fc == "SG" and a.values_by_sg:
            grp = active.get(a.domain)
            if grp in a.values_by_sg:
                want = _parse_like(a.parsed_value, a.values_by_sg[grp])
        if want is None:
            continue
        spec = _live_spec(model, a.domain, a.ln_name, (*a.do_path, *a.da_path), a.fc)
        if spec is None or not spec.is_basic:
            continue
        jobs.setdefault(a.domain, []).append((a, spec, want))
    mismatches = checked = 0
    for domain, items in jobs.items():
        for i in range(0, len(items), 16):
            chunk = items[i : i + 16]
            try:
                vals = client.read_multiple(domain, [a.mms_path for a, _, _ in chunk], [s for _, s, _ in chunk])
            except ServiceError as e:
                vals = [AccessError(e.error)] * len(chunk)
            for (a, _spec, want), got in zip(chunk, vals, strict=True):
                checked += 1
                if isinstance(got, AccessError):
                    yield _r("config-value-mismatch", title, Status.WARN, CONF, subject=f"{a.ref} [{a.fc}]",
                             message=f"cannot read ({got.error})", error=got.error)
                    continue
                if not values_equal(got, want):
                    mismatches += 1
                    shown = f"{got!r}" + (f" ({a.enum_values.get(got)})" if a.enum_values and isinstance(got, int) else "")
                    yield _r("config-value-mismatch", title, Status.WARN, CONF, subject=f"{a.ref} [{a.fc}]",
                             message=f"{shown} on the device, {want!r} in the reference", error=codes.check("config-value-mismatch"),
                             evidence={"device": value_to_json(got), "reference": want, "source": a.value_source})
    if checked and not mismatches:
        yield _r("config-value-mismatch", title, Status.PASS, CONF, message=f"{checked} configured values match")


def _parse_like(template: Any, text: str) -> Any:
    if isinstance(template, bool):
        return text.strip().lower() in ("true", "1")
    if isinstance(template, int):
        try:
            return int(text)
        except ValueError:
            return template
    if isinstance(template, float):
        try:
            return float(text)
        except ValueError:
            return template
    return text


# ------------------------------------------------------------------------------------ identity / addresses
def _scl_identity_checks(session: Session, ref: Reference) -> Iterator[CheckResult]:
    exp = ref.expected
    assert exp is not None
    ident = session.identity
    if exp.ip:
        ok = exp.ip == session.target.host
        yield _r("address-mismatch", "Device address matches the reference", Status.PASS if ok else Status.FAIL, CONF,
                 subject=exp.ied_name, message=f"reference ConnectedAP IP {exp.ip}, connected to {session.target.host}",
                 error=None if ok else codes.check("address-mismatch"))
    if ident is not None and exp.manufacturer and ident.vendor.value:
        same = str(exp.manufacturer).strip().lower() in str(ident.vendor.value).strip().lower() or str(ident.vendor.value).strip().lower() in str(exp.manufacturer).strip().lower()
        yield _r("identity-disagreement", "Vendor matches the reference", Status.PASS if same else Status.WARN, CONF,
                 message=f"device vendor {ident.vendor.value!r} ({ident.vendor.source}); reference manufacturer {exp.manufacturer!r}",
                 error=None if same else codes.check("identity-disagreement"))
    if ident is not None:
        inferred = [e for e in ident.edition_evidence if "ldNs" in e]
        scl_ed = ref.scl_edition()
        from mms_client.core.identity import edition_from_ldns

        ns_eds = {edition_from_ldns(ns) for ns in ident.ld_namespaces.values()} - {None}
        if scl_ed and ns_eds and {e.value for e in ns_eds} != {scl_ed[0]}:
            yield _r("edition-mismatch", "Edition consistent between device and reference", Status.WARN, CONF,
                     message=f"reference says {scl_ed[0]} ({scl_ed[1]}); device namespaces suggest {', '.join(sorted(e.value for e in ns_eds))}",
                     evidence={"ldNs": inferred}, error=codes.check("edition-mismatch"))


# ------------------------------------------------------------------------------------ communication (VER-3)
def _communication_checks(session: Session, ref: Reference, rcbs: list[RcbStatus], opts: Any) -> Iterator[CheckResult]:
    exp = ref.expected
    assert exp is not None
    live = {(s.cb.ld, s.cb.ln, s.cb.name): s for s in rcbs}
    rels = []
    if ref.doc is not None:
        try:
            rels = [r for r in client_relationships(ref.doc, [exp]) if r.server_ied == exp.ied_name]
        except Exception as e:  # pragma: no cover - defensive: malformed SCD
            yield _r("client-relationships", "Client relationships from the reference", Status.NOT_RUN, COMM, reason=str(e))
    inventory_ips: dict[str, str | None] = {}
    if session.inventory is not None:
        for c in session.inventory.clients_of(session.target.name or ""):
            inventory_ips[c.client] = c.source_ip
    if not rels:
        yield _r("client-relationships", "Clients the reference expects", Status.INFO, COMM,
                 message="the reference assigns no RCBs to clients (no ClientLN)" if ref.kind != "cid" else
                 "a CID describes one device only; use the SCD for cross-device checks (VER-3)")
    as_client = session.target.as_client.client if session.target.as_client else None
    for rel in rels:
        client_ip = rel.client_ip
        inv_ip = inventory_ips.get(rel.client_ied)
        if inv_ip and client_ip and inv_ip != client_ip:
            yield _r("address-mismatch", "Client address consistent", Status.WARN, COMM, subject=rel.client_ied,
                     message=f"SCD gives {client_ip}, inventory gives {inv_ip}", error=codes.check("address-mismatch"))
        for r in rel.rcbs:
            st = live.get((r.domain, r.ln_name, r.name))
            subject = f"{r.ref} for {rel.client_ied}"
            if st is None or st.values is None:
                yield _r("client-rcb-missing", "The client's RCBs exist", Status.FAIL, COMM, subject=subject,
                         message="the RCB this client expects is missing or unreadable", error=codes.check("client-rcb-missing"))
                continue
            ips = {x for x in (client_ip, inv_ip) if x}
            if st.owner_ip and st.owner_ip in ips:
                yield _r("client-rcb-not-available", "The client's RCBs are available to it", Status.PASS, COMM, subject=subject,
                         message=f"{st.state} by the client itself ({st.owner_ip})")
                continue
            reason = free_reason(st, me=rel.client_ied)
            if reason is None:
                yield _r("client-rcb-not-available", "The client's RCBs are available to it", Status.PASS, COMM, subject=subject,
                         message="free: the client can enable it")
            else:
                yield _r("client-rcb-not-available", "The client's RCBs are available to it", Status.FAIL, COMM, subject=subject,
                         message=f"not available to {rel.client_ied}: {reason}", error=codes.check("client-rcb-not-available"),
                         certainty="fact")
            if opts is not None and getattr(opts, "try_reports", False) and as_client == rel.client_ied and reason is None:
                yield from _try_report(session, st)
    if as_client:
        yield _r("client-association", "Association as the client", Status.PASS, COMM, subject=as_client,
                 message=f"associated from {session.target.local_ip or 'the default address'} as {as_client}")


def _try_report(session: Session, st: RcbStatus) -> Iterator[CheckResult]:
    title = "The client's RCB delivers reports"
    try:
        sub = subscribe(session, st.cb.reference, gi=True)
    except Exception as e:
        yield _r("client-report-delivery", title, Status.FAIL, COMM, subject=st.cb.reference, message=str(e),
                 error=getattr(e, "error", None))
        return
    try:
        view = sub.next(timeout=5.0)
        if view is None:
            yield _r("client-report-delivery", title, Status.FAIL, COMM, subject=st.cb.reference,
                     message="enabled, but no report arrived within 5 s after GI")
        else:
            yield _r("client-report-delivery", title, Status.PASS, COMM, subject=st.cb.reference,
                     message=f"report received (SqNum {view.report.seq_num}, {len(view.report.entries)} entries)")
    finally:
        sub.stop()



def reference_for(
    session: Session,
    path: str | Path | None = None,
    *,
    kind: str | None = None,
    ied: str | None = None,
    device_supplied: bool = False,
    device_file: str | None = None,
) -> Reference | None:
    """Pick the reference (VER-1 order): an explicit file, else the inventory's reference for the
    device, else (when ``device_supplied``) an SCL file offered by the device (VER-8)."""
    if path is not None:
        k = kind or Reference.guess_kind(path)
        return Reference.load(k, path, ied, live_model=session.model(), host=session.target.host)
    dev = session.target.device
    if dev is not None and dev.reference is not None:
        r = dev.reference
        return Reference.load(r.kind, r.path, ied or r.ied, live_model=session.model(), host=session.target.host)
    if device_supplied or device_file:
        return Reference.from_device(session, device_file)
    return None
