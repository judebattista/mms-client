"""The check engine (§12): self-consistency checks (always), reference checks (SCD / CID /
snapshot / device-supplied SCL) and communication checks (acting as a real neighbour).

Every check yields a :class:`CheckResult` (ARC-2) in one of two categories that are never
combined (VER-7): Configuration (does the device match its reference?) and Communication (can
a client, acting as the neighbour, do what the neighbour needs?).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

from ied_client import codes
from ied_client.core.identity import Confidence, Edition, ask_edition, identify
from ied_client.core.model import DeviceModel
from ied_client.core.refs import RefError
from ied_client.core.reports import list_rcbs
from ied_client.core.results import Category, CheckReport, CheckResult, RawExchange, Status
from ied_client.core.session import Session
from ied_client.inventory_ops import offer_to_save_edition
from ied_client.protocol.errors import ServiceError
from ied_client.protocol.types import AccessError

from .diff import IgnoreRules, diff_snapshots
from .snapshot import Snapshot, capture

CONF = Category.CONFIGURATION
COMM = Category.COMMUNICATION


@dataclass
class CheckOptions:
    test_writes: bool = False  # §12.3 "when writes are tested": write the current value back
    write_filter: str | None = None  # glob on IEC refs to limit write tests
    try_reports: bool = False  # communication: briefly enable the client's RCBs and wait for GI
    ignore: IgnoreRules = field(default_factory=IgnoreRules)
    progress: Callable[[str, int, int], None] | None = None


def _r(check_id: str, title: str, status: Status, category: Category, **kw: Any) -> CheckResult:
    return CheckResult(check_id, title, status, category, **kw)


# ------------------------------------------------------------------------------------------ edition
def edition_for_check(session: Session, needed_for: str) -> Edition:
    """The device's edition for an edition-dependent check; asks the operator only if needed
    and only interactively (IDN-4, IDN-5)."""
    ident = session.identity or identify(session, log=False)
    ed = ident.edition_value
    if ed is Edition.UNKNOWN:
        attr = ask_edition(session.ui, session.device_name, needed_for)
        if attr.confidence is Confidence.OPERATOR:
            session.operator_edition = attr  # kept when the identity is read again (IDN-6)
            ident.edition = attr
            ident.edition_evidence.append(f"operator answered {attr.value.value}")
            offer_to_save_edition(session, attr)
        ed = ident.edition_value
    return ed


# ------------------------------------------------------------------------------------------ self-consistency
def self_consistency(session: Session, opts: CheckOptions) -> Iterator[CheckResult]:
    """§12.3 — always run, with or without a reference."""
    client = session.require_client()
    model = session.model()
    names = session.protocol.names
    rcb_states = list_rcbs(session)
    datasets = [ds for ldi in model.lds.values() for ds in ldi.datasets]
    # 1. RCB → dataset exists
    for st in rcb_states:
        if st.values is None:
            yield _r("rcb-readable", "RCB attributes can be read", Status.FAIL, CONF, subject=st.cb.reference, error=st.error,
                     message=f"reading {st.cb.reference} failed: {st.error}")
            continue
        ds = st.values.dataset
        raw = [RawExchange("get-rcb", st.cb.reference, response=st.values.to_json())]
        if not ds:
            yield _r("rcb-dataset-missing", "RCB references an existing dataset", Status.WARN, CONF, subject=st.cb.reference,
                     message="DatSet is empty: this RCB cannot report anything", raw=raw, error=codes.check("rcb-dataset-missing"))
            continue
        try:
            exists = names.parse_dataset(ds) in datasets
        except RefError:
            exists = False
        yield _r(
            "rcb-dataset-missing",
            "RCB references an existing dataset",
            Status.PASS if exists else Status.FAIL,
            CONF,
            subject=st.cb.reference,
            message=f"DatSet {ds} " + ("exists" if exists else "does not exist on the device"),
            evidence={"datset": ds},
            raw=raw,
            error=None if exists else codes.check("rcb-dataset-missing"),
            certainty="fact",
        )
    # 2. dataset members resolve
    for full, ds in sorted((names.dataset(ds), ds) for ds in datasets):
        try:
            members, _ = client.dataset_directory(ds)
        except ServiceError as e:
            yield _r("dataset-readable", "Dataset directory can be read", Status.FAIL, CONF, subject=full, error=e.error,
                     message=str(e))
            continue
        bad = []
        for m in members:
            ref = m.ref
            if ref is None or not model.has(ref) or (ref.path and ref.fc and not _has_fc(model, ref)):
                bad.append(m.native)
        yield _r(
            "dataset-member-unresolved",
            "Every dataset member resolves in the device model",
            Status.FAIL if bad else Status.PASS,
            CONF,
            subject=full,
            message=(f"{len(bad)} of {len(members)} members do not resolve: " + ", ".join(bad)) if bad else f"{len(members)} members resolve",
            evidence={"members": [m.native for m in members], "unresolved": bad},
            raw=[RawExchange("get-dataset-directory", full, response=[m.native for m in members])],
            error=codes.check("dataset-member-unresolved") if bad else None,
            certainty="fact",
        )
    # 3. RCBs enabled/reserved by clients not in the inventory
    for st in rcb_states:
        if st.values is None:
            continue
        busy = st.values.rpt_ena or st.values.resv or (st.values.resv_tms not in (None, 0))
        if not busy:
            continue
        cid = "rcb-enabled-by-unknown-client" if st.values.rpt_ena else "rcb-reserved-by-unknown-client"
        who = st.owner_name or st.owner_ip
        if session.inventory is None:
            yield _r(cid, "RCB in use by a known client", Status.NOT_RUN, COMM, subject=st.cb.reference,
                     reason="no inventory to compare the owner with", evidence={"owner_ip": st.owner_ip})
        elif st.owner_name:
            yield _r(cid, "RCB in use by a known client", Status.PASS, COMM, subject=st.cb.reference,
                     message=f"used by {st.owner_name}", evidence={"owner_ip": st.owner_ip, "owner": st.owner_name})
        else:
            yield _r(
                cid,
                "RCB in use by a known client",
                Status.WARN,
                COMM,
                subject=st.cb.reference,
                message=f"{'enabled' if st.values.rpt_ena else 'reserved'} by {who or 'a client that the device does not identify (no Owner)'}, "
                "which is not in the inventory",
                evidence={"owner_ip": st.owner_ip},
                error=codes.check(cid),
            )
    # 4. BRCB buffer overflow (only visible in reports)
    for st in (s for s in rcb_states if s.cb.kind == "BRCB"):
        seen = session.buf_ovfl_seen.get(st.cb.reference)
        if seen is None:
            yield _r("brcb-buffer-overflow", "BRCB buffer has not overflowed", Status.NOT_RUN, COMM, subject=st.cb.reference,
                     reason="BufOvfl is only reported inside reports; subscribe to this BRCB to observe it")
        else:
            ovfl, when = seen
            yield _r("brcb-buffer-overflow", "BRCB buffer has not overflowed", Status.FAIL if ovfl else Status.PASS, COMM,
                     subject=st.cb.reference, message=("a report carried BufOvfl=TRUE: buffered events were lost"
                                                       if ovfl else "last report carried BufOvfl=FALSE"),
                     evidence={"seen_at": when}, error=codes.check("brcb-buffer-overflow") if ovfl else None,
                     certainty="fact")
    # 5. writable attributes accept writes (only when requested)
    if opts.test_writes:
        yield from _write_tests(session, opts)
    else:
        yield _r("writable-attribute-refused", "Writable attributes accept writes", Status.NOT_RUN, CONF,
                 reason="writes are not tested unless --test-writes is given")
    # 6. edition-dependent: LocSta (IDN-7)
    yield from _locsta_check(session, model)


def _has_fc(model: DeviceModel, ref) -> bool:
    try:
        model.resolve_fc(ref)
        return True
    except RefError:
        return False


def _locsta_check(session: Session, model: DeviceModel) -> Iterator[CheckResult]:
    lds_with_controls = sorted({r.ld for r in model.controllable_objects()})
    if not lds_with_controls:
        return
    missing = [ld for ld in lds_with_controls if not any(
        "LocSta" in lni.data for lni in model.lds[ld].lns.values()
    )]
    ed = edition_for_check(session, "deciding whether a missing LocSta is an issue")
    if not missing:
        yield _r("locsta-missing", "Station-level switching authority (LocSta) present", Status.PASS, CONF,
                 message="LocSta present in every LD with controllable objects")
        return
    if ed is Edition.UNKNOWN:
        yield _r("locsta-missing", "Station-level switching authority (LocSta) present", Status.NOT_RUN, CONF,
                 subject=", ".join(missing), reason="edition unknown: LocSta is expected from Ed2 on, not in Ed1",
                 error=codes.tool("edition-unknown"))
    elif ed is Edition.ED1:
        yield _r("locsta-missing", "Station-level switching authority (LocSta) present", Status.INFO, CONF,
                 subject=", ".join(missing), message="no LocSta; not an issue for an Ed1 device",
                 evidence={"edition": ed.value})
    else:
        yield _r("locsta-missing", "Station-level switching authority (LocSta) present", Status.WARN, CONF,
                 subject=", ".join(missing), message=f"no LocSta in {', '.join(missing)} although the device is {ed.value}",
                 evidence={"edition": ed.value}, error=codes.check("locsta-missing"))


def _write_tests(session: Session, opts: CheckOptions) -> Iterator[CheckResult]:
    import fnmatch

    from ied_client.core.readwrite import values_equal

    client = session.require_client()
    model = session.model()
    targets = [
        (ref, spec)
        for ref, spec in model.iter_leaves(fcs=codes.NORMALLY_WRITABLE_FCS)
        if spec.is_basic and (opts.write_filter is None or fnmatch.fnmatchcase(ref.iec(), opts.write_filter))
    ]
    if not targets:
        return
    session.policy.confirm_write(
        session.ui,
        f"Write test: write the current value back to {len(targets)} attribute(s) on {session.device_name} "
        f"(filter {opts.write_filter or 'none'}). Values do not change, but these are real writes.",
    )
    for ref, spec in targets:
        try:
            cur = client.read(ref, spec)
        except ServiceError as e:
            yield _r("writable-attribute-refused", "Writable attribute accepts a write", Status.FAIL, CONF, subject=str(ref), error=e.error)
            continue
        if isinstance(cur, AccessError):
            yield _r("writable-attribute-refused", "Writable attribute accepts a write", Status.NOT_RUN, CONF, subject=str(ref),
                     reason=f"cannot read the current value ({cur.error})")
            continue
        try:
            client.write(ref, spec, cur)
            after = client.read(ref, spec)
            ok = values_equal(after, cur)
            session.log.write("write", ref=ref.iec(), fc=ref.fc, before=cur, requested=cur, after=after, ok=True,
                              write_kind="write-test")
            yield _r("writable-attribute-refused", "Writable attribute accepts a write", Status.PASS if ok else Status.WARN,
                     CONF, subject=str(ref), message="write of the current value accepted" if ok else "value changed after writing it back")
        except ServiceError as e:
            yield _r("writable-attribute-refused", "Writable attribute accepts a write", Status.FAIL, CONF, subject=str(ref),
                     error=e.error, message=f"the device refused a write to a normally writable attribute: {e.error}",
                     raw=[RawExchange("write", str(ref), request=repr(cur), error=e.error)])


# ------------------------------------------------------------------------------------------ snapshot reference
def against_snapshot(session: Session, ref_snap: Snapshot, opts: CheckOptions, label: str) -> Iterator[CheckResult]:
    live = capture(session, progress=opts.progress)
    d = diff_snapshots(ref_snap, live, ignore=opts.ignore, left_label=label, right_label="live")
    by_layer: dict[str, list] = {}
    for e in d.entries:
        by_layer.setdefault(e.layer, []).append(e)
    titles = {
        "structure": ("snapshot-structure", "Model, datasets and control blocks match the snapshot", CONF),
        "configuration": ("snapshot-configuration", "Configuration values match the snapshot", CONF),
        "operational": ("snapshot-operational", "Operational state compared with the snapshot", COMM),
    }
    for layer, (cid, title, cat) in titles.items():
        entries = by_layer.get(layer, [])
        if not entries:
            yield _r(cid, title, Status.PASS, cat, message="no differences")
            continue
        for e in entries:
            yield _r(
                cid,
                title,
                e.severity,
                cat,
                subject=e.key,
                message=f"{e.change}: {e.old!r} -> {e.new!r}",
                evidence=e.to_json(),
                error=codes.check("model-object-missing" if layer == "structure" and e.change == "removed" else
                                  "model-object-unexpected" if layer == "structure" and e.change == "added" else
                                  "config-value-mismatch" if layer == "configuration" else "snapshot-difference"),
            )
    for note in d.notes:
        yield _r("snapshot-notes", "Snapshot comparison notes", Status.INFO, CONF, message=note)


# ------------------------------------------------------------------------------------------ entry point
def run_checks(session: Session, reference: Any = None, opts: CheckOptions | None = None) -> CheckReport:
    """Run all applicable checks. ``reference`` is a :class:`verify.reference.Reference` or None."""
    opts = opts or CheckOptions()
    rep = CheckReport(device=session.device_name, reference=reference.describe() if reference is not None else None)
    t0 = time.time()
    for r in self_consistency(session, opts):
        rep.add(r)
    if reference is not None:
        for r in reference.checks(session, opts):
            rep.add(r)
    else:
        reason = ("no reference (SCD, CID, snapshot or device-supplied SCL) given: only self-consistency checks ran; "
                  "review the raw values yourself")
        if not session.ui.interactive:
            reason += " (a non-interactive run does not look for SCL files on the device: use --device-scl)"
        rep.add(_r("reference", "Reference comparison", Status.NOT_RUN, CONF, reason=reason))
    rep.finished_at = time.time()
    session.log.write("check", reference=rep.reference, summary=rep.summary(), duration_s=rep.finished_at - t0,
                      results=[r.to_json() for r in rep.results])
    return rep
