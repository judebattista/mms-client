"""Report control blocks: inspect (level A), subscribe to a free RCB (level B), take over (level C,
expert) — RPT-1 … RPT-8.

Cleanup is a tested requirement (RPT-7): every change the tool makes to an RCB is registered in
the session's cleanup registry *before* it is made, so that exit, Ctrl-C and lost connections
all undo it (or report what lingers).
"""

from __future__ import annotations

import queue
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from mms_client import codes
from mms_client.adapter import RcbValues, Report, ServiceError, VarSpec, format_value, value_to_json
from mms_client.codes import ErrorInfo

from .model import ControlBlockInfo, dataset_ref_to_mms
from .refs import ObjectRef, RefError, parse_mms, parse_ref
from .safety import PolicyError
from .session import CleanupAction, Session, owner_ip


def trg_ops_names(v: int | None) -> list[str]:
    return [] if v is None else [n for b, n in codes.TRG_OPS.items() if v & b]


def opt_flds_names(v: int | None) -> list[str]:
    return [] if v is None else [n for b, n in codes.OPT_FLDS.items() if v & b]


def parse_trg_ops(text: str) -> int:
    """``dchg,qchg,gi`` → bits."""
    names = {n: b for b, n in codes.TRG_OPS.items()}
    names["integrity"] = names["period"]
    out = 0
    for part in text.replace("+", ",").split(","):
        p = part.strip()
        if not p:
            continue
        if p not in names:
            raise PolicyError(codes.tool("invalid-trgops"), f"unknown trigger option {p!r} (dchg, qchg, dupd, period, gi)")
        out |= names[p]
    return out


# ------------------------------------------------------------------------------ inspection (RPT-1)
@dataclass(slots=True)
class RcbStatus:
    cb: ControlBlockInfo
    values: RcbValues | None = None
    error: ErrorInfo | None = None
    owner_ip: str | None = None
    owner_name: str | None = None
    assigned_to: list[str] = field(default_factory=list)  # clients assigned in the SCD / inventory
    expected_conf_rev: int | None = None  # from the reference (RPT-6)
    last_buf_ovfl: bool | None = None  # BufOvfl of the last report received by this tool (BRCB)

    @property
    def state(self) -> str:
        v = self.values
        if v is None:
            return "unreadable"
        if v.rpt_ena:
            return "enabled"
        if v.resv:
            return "reserved"
        if v.resv_tms not in (None, 0):
            return "reserved"
        if self.owner_ip:
            return "owned"
        return "free"

    @property
    def conf_rev_mismatch(self) -> bool:
        return (
            self.expected_conf_rev is not None
            and self.values is not None
            and self.values.conf_rev is not None
            and self.values.conf_rev != self.expected_conf_rev
        )

    def to_json(self) -> dict:
        v = self.values
        return {
            "reference": self.cb.reference,
            "type": "BR" if self.cb.kind == "BRCB" else "RP",
            "state": self.state,
            "values": v.to_json() if v else None,
            "trg_ops": trg_ops_names(v.trg_ops) if v else None,
            "opt_flds": opt_flds_names(v.opt_flds) if v else None,
            "owner_ip": self.owner_ip,
            "owner_name": self.owner_name,
            "assigned_to": self.assigned_to,
            "expected_conf_rev": self.expected_conf_rev,
            "conf_rev_mismatch": self.conf_rev_mismatch,
            "last_buf_ovfl": self.last_buf_ovfl,
            "error": self.error.to_json() if self.error else None,
        }


def assignments(session: Session, cb: ControlBlockInfo) -> list[str]:
    """Clients the reference (SCD ClientLN) or the inventory assign to this RCB instance."""
    out: list[str] = []
    ref_fn = getattr(session.reference, "rcb_clients", None)
    if callable(ref_fn):
        out.extend(ref_fn(cb))
    if session.inventory is not None:
        for rel in session.inventory.clients_of(session.target.name or ""):
            for r in rel.rcbs:
                if same_rcb(r, cb):
                    out.append(rel.client)
    return sorted(set(out))


def same_rcb(text: str, cb: ControlBlockInfo) -> bool:
    """Does an RCB name from the inventory or the command line (``LD/LLN0.BR.brcbA01``, ``LD/LLN0$BR$brcbA01``
    or the IEC form without FC, ``LD/LLN0.brcbA01``) name this control block?"""
    t = text.replace("$", ".")
    return t in (cb.reference, f"{cb.ld}/{cb.ln}.{cb.name}") or t.endswith(f"/{cb.ln}.{cb.fc}.{cb.name}")


def list_rcbs(session: Session, ln_filter: str | None = None) -> list[RcbStatus]:
    client = session.require_client()
    model = session.model()
    out = []
    for cb in model.rcbs():
        if ln_filter and not _matches_ln(cb, ln_filter, session):
            continue
        st = RcbStatus(cb)
        try:
            st.values = client.get_rcb(cb.reference)
            st.owner_ip = owner_ip(st.values.owner)
            st.owner_name = session.owner_name(st.values.owner)
        except ServiceError as e:
            st.error = e.error
        st.assigned_to = assignments(session, cb)
        seen = session.buf_ovfl_seen.get(cb.reference)
        st.last_buf_ovfl = seen[0] if seen else None
        exp = getattr(session.reference, "rcb_conf_rev", None)
        if callable(exp):
            st.expected_conf_rev = exp(cb)
        out.append(st)
    session.log.write("request", service="get-rcb", target=ln_filter or "all", count=len(out))
    return out


def _matches_ln(cb: ControlBlockInfo, text: str, session: Session) -> bool:
    try:
        ref = parse_ref(text, session.cwd, None, session.model().ld_names)
    except RefError:
        return text in (cb.ln, cb.name)
    return cb.ld == ref.ld and (ref.ln is None or cb.ln == ref.ln)


def find_instances(session: Session, text: str) -> list[ControlBlockInfo]:
    """An RCB reference (``LD/LLN0.BR.brcb01``, ``LD/LLN0$BR$brcb01``), a name, or a base name of
    indexed instances (``brcb`` → brcb01, brcb02 …)."""
    model = session.model()
    t = text.strip()
    cands = model.rcbs()
    exact = [cb for cb in cands if t in (cb.reference, cb.mms_reference, cb.name, f"{cb.ln}.{cb.name}")]
    if exact:
        return exact
    base = t.split("/")[-1].split(".")[-1].split("$")[-1]
    indexed = [cb for cb in cands if cb.name.startswith(base) and cb.name[len(base) :].isdigit()]
    if "/" in t:
        ld = t.split("/")[0]
        indexed = [cb for cb in indexed if cb.ld == ld]
    return indexed


def free_reason(st: RcbStatus, *, me: str | None) -> str | None:
    """None if the instance is free for level B; otherwise why not."""
    if st.values is None:
        return f"unreadable ({st.error})"
    if st.values.rpt_ena:
        return f"enabled by {st.owner_name or st.owner_ip or 'another client'}"
    if st.values.resv:
        return f"reserved by {st.owner_name or st.owner_ip or 'another client'}"
    if st.values.resv_tms not in (None, 0):
        return f"reserved (ResvTms={st.values.resv_tms}) by {st.owner_name or st.owner_ip or 'a client'}"
    if st.owner_ip and st.owner_name != "this tool":
        return f"owned by {st.owner_name or st.owner_ip}"
    others = [c for c in st.assigned_to if c != me]
    if others:
        return f"assigned to {', '.join(others)} in the reference/inventory"
    return None


# ------------------------------------------------------------------------------ subscription (RPT-2 …)
@dataclass(slots=True)
class ReportView:
    report: Report
    members: list[tuple[str, str | None]]  # (IEC ref, FC) per dataset index
    gap: int = 0  # number of missing SqNums before this report (RPT-2)

    def to_json(self) -> dict:
        d = self.report.to_json()
        for e in d["entries"]:
            idx = e["index"]
            if idx < len(self.members):
                e["member"], e["fc"] = self.members[idx]
        d["sqnum_gap"] = self.gap
        return d

    def lines(self) -> list[str]:
        r = self.report
        head = time.strftime("%H:%M:%S", time.localtime(r.received_at)) + f".{int(r.received_at * 1000) % 1000:03d}"
        parts = [head, f"SqNum={r.seq_num}" if r.seq_num is not None else "SqNum=-"]
        if r.sub_seq_num is not None:
            parts.append(f"SubSqNum={r.sub_seq_num}{'+' if r.more_segments_follow else ''}")
        if r.entry_id is not None:
            parts.append(f"EntryID={r.entry_id.hex()}")
        if r.buf_ovfl:
            parts.append("BufOvfl=TRUE")
        if self.gap:
            parts.append(f"GAP: {self.gap} report(s) missing before this one")
        out = ["  ".join(parts)]
        for e in r.entries:
            name = self.members[e.index][0] if e.index < len(self.members) else f"member {e.index}"
            out.append(f"    {name}  {format_value(e.value, 120)}  [{', '.join(e.reasons) or 'reason n/a'}]")
        return out


class Subscription:
    """An enabled RCB whose reports we receive. Created by :func:`subscribe`."""

    def __init__(self, session: Session, cb: ControlBlockInfo, values: RcbValues, members: list[tuple[str, str | None]]):
        self.session = session
        self.cb = cb
        self.initial = values
        self.members = members
        self.queue: queue.Queue[Report] = queue.Queue()
        self.received = 0
        self.gaps = 0
        self.last_seq: int | None = None
        self.active = False
        self.started_at = time.time()
        self.changed_params: dict[str, Any] = {}
        self.takeover_from: str | None = None

    @property
    def reference(self) -> str:
        return self.cb.reference

    @property
    def seq_modulus(self) -> int:
        return 65536 if self.cb.kind == "BRCB" else 256

    def _on_report(self, r: Report) -> None:  # connection thread
        self.queue.put(r)

    def next(self, timeout: float | None = None) -> ReportView | None:
        try:
            r = self.queue.get(timeout=timeout)
        except queue.Empty:
            return None
        gap = 0
        if r.seq_num is not None and (r.sub_seq_num in (None, 0)):
            if self.last_seq is not None:
                expected = (self.last_seq + 1) % self.seq_modulus
                if r.seq_num != expected and r.seq_num != self.last_seq:
                    gap = (r.seq_num - expected) % self.seq_modulus
                    self.gaps += gap
            self.last_seq = r.seq_num
        self.received += 1
        if r.buf_ovfl is not None:
            self.session.buf_ovfl_seen[self.reference] = (bool(r.buf_ovfl), r.received_at)
        view = ReportView(r, self.members, gap)
        if self.received <= 50 or gap:
            self.session.log.write("report", rcb=self.reference, report=view.to_json())
        return view

    def reports(self, *, duration_s: float | None = None, count: int | None = None, poll_s: float = 0.2) -> Iterator[ReportView]:
        start = time.monotonic()
        n = 0
        while self.active:
            if duration_s is not None and time.monotonic() - start >= duration_s:
                return
            if count is not None and n >= count:
                return
            if self.session.connection_lost.is_set():
                return
            v = self.next(timeout=poll_s)
            if v is not None:
                n += 1
                yield v

    def gi(self) -> None:
        """RPT-5: general interrogation."""
        client = self.session.require_client()
        client.set_rcb(self.reference, {"gi": True})
        self.session.log.write("rcb", action="gi", rcb=self.reference, ok=True)

    def stop(self) -> list[str]:
        """Undo everything done for this subscription (RPT-7)."""
        notes: list[str] = []
        # Order matters: most devices refuse parameter changes while RptEna is true.
        for key in (f"rcb:{self.reference}:enable", f"rcb:{self.reference}:params", f"rcb:{self.reference}:resv"):
            action = next((a for a in self.session.cleanups if a.key == key), None)
            if action is None:
                continue
            self.session.drop_cleanup(key)
            if not self.session.connected:
                notes.append(f"could not undo: {action.description} — {action.if_lost}")
                continue
            try:
                notes.append(action.undo() or f"undone: {action.description}")
                lingering = action.check_after_undo()
                if lingering:
                    notes.append(f"still in effect: {lingering}")
            except ServiceError as e:
                notes.append(f"could not undo: {action.description} ({e.error}) — {action.if_lost}")
        if self.session.client is not None:
            self.session.client.uninstall_report_handler(self.reference)
        self.active = False
        self.session.subscriptions.pop(self.reference, None)
        self.session.log.write("rcb", action="unsubscribe", rcb=self.reference, notes=notes, received=self.received, gaps=self.gaps)
        return notes

    def to_json(self) -> dict:
        return {
            "rcb": self.reference,
            "type": self.cb.kind,
            "rpt_id": self.initial.rpt_id,
            "dataset": self.initial.dataset,
            "members": [m for m, _ in self.members],
            "changed_params": self.changed_params,
            "received": self.received,
            "sqnum_gaps": self.gaps,
            "takeover_from": self.takeover_from,
        }


class NoFreeRcbError(PolicyError):
    """RPT-3: no free instance; ``instances`` lists every instance with its state."""

    def __init__(self, text: str, instances: list[RcbStatus]) -> None:
        self.instances = instances
        lines = [f"no free instance for {text!r}:"]
        for st in instances:
            lines.append(f"  {st.cb.reference}: {free_reason(st, me=None) or 'free'}")
        lines.append("Use a free instance, or (expert mode) --takeover to take one over.")
        super().__init__(codes.tool("rcb-no-free-instance"), "\n".join(lines))


def _members(session: Session, dataset_ref: str | None) -> tuple[list[tuple[str, str | None]], list[VarSpec | None]]:
    if not dataset_ref:
        return [], []
    client = session.require_client()
    model = session.model()
    domain, name = dataset_ref_to_mms(dataset_ref)
    try:
        members, _ = client.get_dataset_directory(domain, name)
    except ServiceError:
        return [], []
    names: list[tuple[str, str | None]] = []
    specs: list[VarSpec | None] = []
    for m in members:
        try:
            ref = parse_mms(f"{m.domain}/{m.item}")
            names.append((ref.iec(), ref.fc))
            try:
                specs.append(model.resolve_fc(ref)[1] if ref.path else None)
            except RefError:
                specs.append(None)
        except RefError:
            names.append((m.mms_ref(), None))
            specs.append(None)
    return names, specs


def subscribe(
    session: Session,
    text: str,
    *,
    takeover: bool = False,
    trg_ops: int | None = None,
    intg_pd: int | None = None,
    opt_flds: int | None = None,
    buf_tm: int | None = None,
    gi: bool = True,
    purge_buf: bool = False,
    me: str | None = None,
) -> Subscription:
    """Enable an RCB and start receiving its reports.

    Level B (standard mode): only a disabled, unreserved instance not assigned to another client.
    Level C (``takeover``, expert mode): any instance, after typed confirmation naming the
    client that will lose it (RPT-4).
    """
    client = session.require_client()
    instances = find_instances(session, text)
    if not instances:
        raise PolicyError(codes.tool("rcb-not-found"), f"no report control block matches {text!r} (see `rcb`)")
    me = me or (session.target.as_client.client if session.target.as_client else None)
    statuses = []
    for cb in instances:
        st = RcbStatus(cb)
        try:
            st.values = client.get_rcb(cb.reference)
            st.owner_ip = owner_ip(st.values.owner)
            st.owner_name = session.owner_name(st.values.owner)
        except ServiceError as e:
            st.error = e.error
        st.assigned_to = assignments(session, cb)
        statuses.append(st)
    chosen = next((s for s in statuses if free_reason(s, me=me) is None), None)
    victim: str | None = None
    if chosen is None:
        if not takeover:
            raise NoFreeRcbError(text, statuses)
        session.policy.require_expert("RCB takeover (level C)")
        chosen = statuses[0] if len(statuses) == 1 else _pick_for_takeover(statuses)
        victim = chosen.owner_name or chosen.owner_ip or ", ".join(chosen.assigned_to) or "an unknown client"
        session.policy.confirm_dangerous(
            session.ui,
            f"TAKEOVER of {chosen.cb.reference} on {session.device_name}\n"
            f"  current state: {free_reason(chosen, me=me)}\n"
            f"  {victim} will lose this RCB (its reports stop until it re-enables it).",
            chosen.cb.name,
            "takeover",
        )
    elif takeover:
        session.policy.require_expert("RCB takeover (level C)")
    cb, values = chosen.cb, chosen.values
    assert values is not None
    names, specs = _members(session, values.dataset)
    sub = Subscription(session, cb, values, names)
    sub.takeover_from = victim
    ref = cb.reference
    session.log.write("rcb", action="subscribe", rcb=ref, takeover=bool(victim), victim=victim, before=values.to_json())

    # 1. reservation (URCB)
    if cb.kind == "URCB" and not values.resv:
        session.register_cleanup(
            CleanupAction(
                f"rcb:{ref}:resv",
                f"reservation of {ref}",
                lambda: client.set_rcb(ref, {"resv": False}),
                "the device releases URCB reservations when the association ends",
            )
        )
        _set(session, ref, {"resv": True})
    # 1b. reservation (BRCB with ResvTms, Ed2): enabling a BRCB reserves it for this client's address, and the
    # reservation outlives the association by ResvTms seconds. Registered before anything is written so that it
    # is undone last, after RptEna (RPT-7).
    resv_action = None
    if cb.kind == "BRCB" and values.resv_tms is not None:
        resv_action = _brcb_reservation_cleanup(session, cb, values, chosen.assigned_to)
        session.register_cleanup(resv_action)
    # 2. parameters (RPT-8: logged like any write)
    changes: dict[str, Any] = {}
    originals: dict[str, Any] = {}
    for key, new, old in (
        ("trg_ops", trg_ops, values.trg_ops),
        ("intg_pd", intg_pd, values.intg_pd),
        ("opt_flds", opt_flds, values.opt_flds),
        ("buf_tm", buf_tm, values.buf_tm),
    ):
        if new is not None and new != old:
            changes[key] = new
            originals[key] = old
    if changes:
        session.register_cleanup(
            CleanupAction(
                f"rcb:{ref}:params",
                f"parameters {sorted(changes)} of {ref} (restore {originals})",
                lambda: client.set_rcb(ref, originals, single_request=False),
                "changed RCB parameters stay changed until someone rewrites them; reconnect and restore them "
                f"to {originals}",
            )
        )
        _set(session, ref, changes, before=originals)
        sub.changed_params = {"new": changes, "original": originals}
    if purge_buf and cb.kind == "BRCB":
        _set(session, ref, {"purge_buf": True})
    # 3. handler, then enable
    client.install_report_handler(ref, values.rpt_id, sub._on_report, specs)
    if_lost = (
        "the device disables a URCB when the association ends"
        if cb.kind == "URCB"
        else "the device stops sending reports when the association ends but keeps buffering events for this BRCB"
    )
    session.register_cleanup(
        CleanupAction(f"rcb:{ref}:enable", f"RptEna of {ref}", lambda: client.set_rcb(ref, {"rpt_ena": False}), if_lost)
    )
    try:
        _set(session, ref, {"rpt_ena": True})
    except ServiceError:
        client.uninstall_report_handler(ref)
        session.drop_cleanup(f"rcb:{ref}:enable")
        sub.stop()
        raise
    sub.active = True
    session.subscriptions[ref] = sub
    after = client.get_rcb(ref) if (gi or resv_action is not None) else None
    if resv_action is not None and after is not None and after.resv_tms not in (None, 0):
        # the device's ResvTms is only known now that it has reserved the BRCB for us
        resv_action.if_lost = _lingering_text(ref, after.resv_tms, chosen.assigned_to)
    if gi and after is not None and after.trg_ops is not None and after.trg_ops & 16:
        try:
            sub.gi()
        except ServiceError as e:
            session.remember_error(e.error, {"service": "gi"}, str(e))
    return sub


def _lingering_text(ref: str, resv_tms: int | None, assigned: list[str]) -> str:
    who = ", ".join(assigned) if assigned else "other clients"
    secs = f"{resv_tms} s" if resv_tms not in (None, 0) else "ResvTms seconds"
    return (
        f"{ref} stays reserved for this tool's address for up to {secs} after the association ends; "
        f"{who} cannot enable it until then (reconnect from the same address to release it sooner)"
    )


def _brcb_reservation_cleanup(session: Session, cb: ControlBlockInfo, values: RcbValues, assigned: list[str]) -> CleanupAction:
    """RPT-7 for a BRCB with ResvTms: release the reservation the tool caused, and report what lingers.

    The tool releases (ResvTms := 0) only a reservation that is held for its own address when the cleanup runs.
    After a takeover that is the tool's reservation too: the previous holder's cannot be given back, and
    releasing it lets that client enable the RCB again at once. A reservation made by configuration (ResvTms -1)
    is never touched. Afterwards the BRCB is read again: a reservation that is still held for this tool's address
    is reported with its effect and duration.
    """
    ref = cb.reference
    by_configuration = values.resv_tms == -1

    def undo() -> str | None:
        if by_configuration:
            return f"left the reservation of {ref} as the tool found it (ResvTms=-1: reserved by configuration)"
        client = session.require_client()
        v = client.get_rcb(ref)
        if v.resv_tms not in (None, 0) and not session.is_own_owner(v.owner):
            holder = session.owner_name(v.owner) or owner_ip(v.owner) or "another client"
            return f"left the reservation of {ref} alone: it is held by {holder}, not by this tool"
        if v.resv_tms not in (None, 0):
            client.set_rcb(ref, {"resv_tms": 0})
        return None

    def verify() -> str | None:
        v = session.require_client().get_rcb(ref)
        if v.resv_tms in (None, 0) or not session.is_own_owner(v.owner):
            return None
        return _lingering_text(ref, v.resv_tms, assigned)

    return CleanupAction(
        f"rcb:{ref}:resv",
        f"reservation of {ref}" if by_configuration else f"reservation of {ref} (ResvTms back to 0)",
        undo,
        _lingering_text(ref, None, assigned),
        verify=verify,
    )


def _pick_for_takeover(statuses: list[RcbStatus]) -> RcbStatus:
    """Prefer an instance that is only assigned (not in use), then reserved, then enabled."""
    order = {"free": 0, "owned": 1, "reserved": 2, "enabled": 3, "unreadable": 4}
    return sorted(statuses, key=lambda s: order.get(s.state, 9))[0]


def _set(session: Session, ref: str, changes: dict[str, Any], before: dict[str, Any] | None = None) -> None:
    client = session.require_client()
    try:
        client.set_rcb(ref, changes, single_request=False)
        session.log.write("rcb", action="set", rcb=ref, changes=changes, before=before, ok=True)
    except ServiceError as e:
        session.log.write("rcb", action="set", rcb=ref, changes=changes, before=before, ok=False, error=e.error)
        session.remember_error(e.error, {"service": "set-rcb"}, str(e))
        raise


def describe_values(v: RcbValues) -> dict[str, str]:
    """Human-readable RCB attributes for display."""
    d = {
        "RptID": v.rpt_id or "",
        "RptEna": format_value(v.rpt_ena),
        "DatSet": v.dataset or "",
        "ConfRev": str(v.conf_rev),
        "TrgOps": ",".join(trg_ops_names(v.trg_ops)),
        "OptFlds": ",".join(opt_flds_names(v.opt_flds)),
        "BufTm": str(v.buf_tm),
        "IntgPd": str(v.intg_pd),
        "SqNum": str(v.sq_num),
    }
    if v.buffered:
        d["ResvTms"] = "n/a" if v.resv_tms is None else str(v.resv_tms)
        d["EntryID"] = v.entry_id.hex() if v.entry_id else ""
        d["PurgeBuf"] = format_value(v.purge_buf)
    else:
        d["Resv"] = format_value(v.resv)
    d["Owner"] = v.owner.hex() if v.owner else ""
    return d


def rcb_values_json(v: RcbValues) -> dict:
    return {k: value_to_json(x) for k, x in v.to_json().items()}


def rcb_ref_for(session: Session, text: str) -> ObjectRef:
    return parse_ref(text, session.cwd, None, session.model().ld_names)
