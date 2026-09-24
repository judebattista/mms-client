"""Device identification and edition (IDN-1 … IDN-8).

Every identified attribute carries its source and a confidence (IDN-3):
``confirmed`` (the device or a reference file states it), ``inferred`` (deduced),
``operator-supplied`` (inventory override or an answer to a question) or ``unknown``.
An inferred edition is never presented as confirmed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from mms_client.adapter import AccessError, ServiceError
from mms_client.codes import ErrorInfo

from .model import DeviceModel
from .refs import ObjectRef


class Confidence(StrEnum):
    CONFIRMED = "confirmed"
    INFERRED = "inferred"
    OPERATOR = "operator-supplied"
    UNKNOWN = "unknown"


class Edition(StrEnum):
    ED1 = "Ed1"
    ED2 = "Ed2"
    ED21 = "Ed2.1"
    UNKNOWN = "unknown"

    @property
    def at_least_ed2(self) -> bool:
        return self in (Edition.ED2, Edition.ED21)


@dataclass(frozen=True, slots=True)
class Attr:
    value: Any
    source: str
    confidence: Confidence

    def to_json(self) -> dict:
        v = self.value.value if isinstance(self.value, StrEnum) else self.value
        return {"value": v, "source": self.source, "confidence": self.confidence.value}

    def __str__(self) -> str:
        v = self.value.value if isinstance(self.value, StrEnum) else self.value
        if self.confidence is Confidence.UNKNOWN:
            return "unknown"
        return f"{v} ({self.confidence.value}, from {self.source})"


UNKNOWN_ATTR = Attr(None, "none", Confidence.UNKNOWN)


@dataclass(slots=True)
class IdentitySource:
    """What one source said (IDN-1)."""

    source: str  # "mms-identify" | "LPHD.PhyNam" | "LLN0.NamPlt"
    ld: str | None
    reference: str | None
    fields: dict[str, Any] = field(default_factory=dict)
    error: ErrorInfo | None = None

    def to_json(self) -> dict:
        return {
            "source": self.source,
            "ld": self.ld,
            "reference": self.reference,
            "fields": self.fields,
            "error": self.error.to_json() if self.error else None,
        }


@dataclass(slots=True)
class IdentityReport:
    sources: list[IdentitySource] = field(default_factory=list)
    vendor: Attr = UNKNOWN_ATTR
    model: Attr = UNKNOWN_ATTR
    firmware: Attr = UNKNOWN_ATTR
    edition: Attr = UNKNOWN_ATTR
    disagreements: list[str] = field(default_factory=list)
    edition_evidence: list[str] = field(default_factory=list)
    ld_namespaces: dict[str, str] = field(default_factory=dict)
    config_revs: dict[str, str] = field(default_factory=dict)

    @property
    def edition_value(self) -> Edition:
        v = self.edition.value
        return v if isinstance(v, Edition) else Edition.UNKNOWN

    def to_json(self) -> dict:
        return {
            "vendor": self.vendor.to_json(),
            "model": self.model.to_json(),
            "firmware": self.firmware.to_json(),
            "edition": self.edition.to_json(),
            "edition_evidence": self.edition_evidence,
            "disagreements": self.disagreements,
            "ld_namespaces": self.ld_namespaces,
            "config_revs": self.config_revs,
            "sources": [s.to_json() for s in self.sources],
        }


# ldNs → edition (IDN-2 step 3). The namespace revision letters are an assumption to verify
# against the standard/devices: "2007"/"2007A" are treated as Ed2, "2007B" and later as Ed2.1.
LDNS_RULES: list[tuple[re.Pattern[str], Edition]] = [
    (re.compile(r"7-4:2003\b"), Edition.ED1),
    (re.compile(r"7-4:2007A?$"), Edition.ED2),
    (re.compile(r"7-4:2007[B-Z]\d*$"), Edition.ED21),
]


def edition_from_ldns(ld_ns: str) -> Edition | None:
    s = ld_ns.strip()
    for pat, ed in LDNS_RULES:
        if pat.search(s):
            return ed
    return None


def _norm(s: Any) -> str:
    return re.sub(r"\s+", " ", str(s)).strip().lower() if s is not None else ""


def collect_identity(
    client: Any,
    model: DeviceModel,
    *,
    override: Any = None,  # inventory.IdentityOverride
    scl_edition: tuple[str, str] | None = None,  # (edition, reason) from the SCL reference, IDN-2 step 2
    log: Any = None,
) -> IdentityReport:
    """Read every identity source (IDN-1) and determine vendor/model/firmware/edition (IDN-2/3)."""
    rep = IdentityReport()
    # -- MMS Identify
    src = IdentitySource("mms-identify", None, None)
    try:
        ident = client.identify()
        src.fields = {"vendor": ident.vendor, "model": ident.model, "revision": ident.revision}
    except ServiceError as e:
        src.error = e.error
    rep.sources.append(src)
    # -- per LD: LPHD*.PhyNam and LLN0.NamPlt
    for ld_name, ldi in model.lds.items():
        for ln_name, lni in ldi.lns.items():
            if lni.ln_class == "LPHD" and "PhyNam" in lni.data:
                rep.sources.append(_read_struct(client, model, ObjectRef(ld_name, ln_name, ("PhyNam",)), "LPHD.PhyNam", ("DC",)))
        if "LLN0" in ldi.lns and "NamPlt" in ldi.lns["LLN0"].data:
            s = _read_struct(client, model, ObjectRef(ld_name, "LLN0", ("NamPlt",)), "LLN0.NamPlt", ("DC", "EX"))
            rep.sources.append(s)
            if s.fields.get("ldNs"):
                rep.ld_namespaces[ld_name] = str(s.fields["ldNs"])
            if s.fields.get("configRev"):
                rep.config_revs[ld_name] = str(s.fields["configRev"])
    if log is not None:
        log.write("identity", sources=[s.to_json() for s in rep.sources])
    _decide(rep, override)
    determine_edition(rep, model, override=override, scl_edition=scl_edition)
    return rep


def _read_struct(client: Any, model: DeviceModel, ref: ObjectRef, source: str, fcs: tuple[str, ...]) -> IdentitySource:
    out = IdentitySource(source, ref.ld, ref.iec())
    node = model.resolve(ref).node
    assert node is not None
    for fc in fcs:
        if fc not in node.specs:
            continue
        spec = node.specs[fc]
        try:
            val = client.read(ref.ld, ref.mms_item(fc), spec)
        except ServiceError as e:
            out.error = e.error
            continue
        if isinstance(val, AccessError):
            out.error = val.error
            continue
        if isinstance(val, dict):
            for k, v in val.items():
                if isinstance(v, str | int | float | bool):
                    out.fields[k] = v
    return out


def _decide(rep: IdentityReport, override: Any) -> None:
    ident = next((s for s in rep.sources if s.source == "mms-identify"), None)
    phy = [s for s in rep.sources if s.source == "LPHD.PhyNam" and s.fields]
    nam = [s for s in rep.sources if s.source == "LLN0.NamPlt" and s.fields]

    def first(field_name: str, srcs: list[IdentitySource]) -> tuple[str, str] | None:
        for s in srcs:
            v = s.fields.get(field_name)
            if v not in (None, ""):
                return str(v), f"{s.source} ({s.reference})"
        return None

    candidates = {
        "vendor": [
            (ident.fields.get("vendor"), "MMS Identify") if ident and ident.fields.get("vendor") else None,
            first("vendor", phy),
            first("vendor", nam),
        ],
        "model": [
            (ident.fields.get("model"), "MMS Identify") if ident and ident.fields.get("model") else None,
            first("model", phy),
        ],
        "firmware": [
            (ident.fields.get("revision"), "MMS Identify") if ident and ident.fields.get("revision") else None,
            first("swRev", phy),
            first("swRev", nam),
        ],
    }
    for attr, cands in candidates.items():
        present = [c for c in cands if c]
        ov = getattr(override, attr, None) if override is not None else None
        if ov:
            setattr(rep, attr, Attr(ov, "inventory", Confidence.OPERATOR))
        elif present:
            setattr(rep, attr, Attr(present[0][0], present[0][1], Confidence.CONFIRMED))
        distinct = {_norm(v): (v, src) for v, src in present}
        if len(distinct) > 1:
            rep.disagreements.append(
                f"{attr}: " + "; ".join(f"{src} says {v!r}" for v, src in present)
            )
    # NamPlt vendor may legitimately differ per LD; report differences among LDs too.
    vendors = {_norm(s.fields.get("vendor")): s for s in nam if s.fields.get("vendor")}
    if len(vendors) > 1:
        rep.disagreements.append(
            "LLN0.NamPlt.vendor differs between logical devices: "
            + "; ".join(f"{s.ld}: {s.fields.get('vendor')!r}" for s in vendors.values())
        )


def determine_edition(
    rep: IdentityReport, model: DeviceModel | None, *, override: Any = None, scl_edition: tuple[str, str] | None = None
) -> None:
    """IDN-2 precedence: inventory → SCL → ldNs → model heuristics → (ask, done elsewhere)."""
    ev = rep.edition_evidence
    ov = getattr(override, "edition", None) if override is not None else None
    ns_eds = {ld: edition_from_ldns(ns) for ld, ns in rep.ld_namespaces.items()}
    for ld, ns in rep.ld_namespaces.items():
        ed = ns_eds[ld]
        ev.append(f"{ld}: LLN0.NamPlt.ldNs = {ns!r} → {ed.value if ed else 'unrecognised namespace'}")
    heuristics = _ed2_markers(model) if model is not None else []
    ev.extend(heuristics)
    if ov:
        rep.edition = Attr(Edition(ov), "inventory", Confidence.OPERATOR)
        return
    if scl_edition:
        rep.edition = Attr(Edition(scl_edition[0]), f"SCL ({scl_edition[1]})", Confidence.CONFIRMED)
        return
    found = {e for e in ns_eds.values() if e is not None}
    if len(found) == 1:
        ed = found.pop()
        if ed is Edition.ED1 and heuristics:
            ev.append("ldNs says Ed1 but the model has Ed2-only attributes; treating as inferred Ed2")
            rep.edition = Attr(Edition.ED2, "model heuristics (conflicts with ldNs)", Confidence.INFERRED)
            return
        rep.edition = Attr(ed, "LLN0.NamPlt.ldNs", Confidence.INFERRED)
        return
    if len(found) > 1:
        ev.append("logical devices report namespaces of different editions")
        best = max(found, key=lambda e: [Edition.ED1, Edition.ED2, Edition.ED21].index(e))
        rep.edition = Attr(best, "LLN0.NamPlt.ldNs (mixed across LDs)", Confidence.INFERRED)
        return
    if heuristics:
        rep.edition = Attr(Edition.ED2, "model heuristics (Ed2-only attributes present)", Confidence.INFERRED)
        return
    rep.edition = Attr(Edition.UNKNOWN, "none", Confidence.UNKNOWN)


def _ed2_markers(model: DeviceModel) -> list[str]:
    """Attributes that only exist from Ed2 on (IDN-2 step 4)."""
    out: list[str] = []
    for cb in model.rcbs():
        names = {c.name for c in cb.spec.children}
        if cb.kind == "BRCB" and "ResvTms" in names:
            out.append(f"{cb.reference} has ResvTms (Ed2+)")
            break
    for cb in model.rcbs():
        if "Owner" in {c.name for c in cb.spec.children}:
            out.append(f"{cb.reference} has Owner (Ed2+)")
            break
    for ref, _node in model.iter_data_objects():
        if ref.path[0] == "LocSta":
            out.append(f"{ref.iec()} exists (LocSta is Ed2+)")
            break
    return out


EDITION_QUESTION = (
    "Which IEC 61850 edition does {device} implement?\n"
    "Where to look: the device's manual or data sheet (\"IEC 61850 Edition 1/2/2.1\"), or the project\n"
    "settings of its configuration tool (often labelled \"IEC 61850 edition\" or \"SCL version\").\n"
    "This is needed for: {needed_for}."
)


def ask_edition(ui: Any, device: str, needed_for: str) -> Attr:
    """IDN-4: ask only when needed; always offer "don't know". Non-interactive → unknown (IDN-5)."""
    if not getattr(ui, "interactive", False):
        return Attr(Edition.UNKNOWN, "not asked (non-interactive)", Confidence.UNKNOWN)
    ans = ui.choose(
        EDITION_QUESTION.format(device=device, needed_for=needed_for),
        [("Ed1", "Edition 1 (2003)"), ("Ed2", "Edition 2"), ("Ed2.1", "Edition 2.1"), ("unknown", "Don't know")],
        default="unknown",
    )
    if ans in ("Ed1", "Ed2", "Ed2.1"):
        return Attr(Edition(ans), "operator", Confidence.OPERATOR)
    return Attr(Edition.UNKNOWN, "operator answered don't know", Confidence.UNKNOWN)
