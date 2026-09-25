"""EXP-7 coverage and catalogue content rules (EXP-1, EXP-2, EXP-4, EXP-6)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from ied_client import codes
from ied_client.explain import (
    REQUIRED_CHECK_IDS,
    REQUIRED_TOOL_CODES,
    Catalogue,
    Certainty,
    HintContext,
    default_catalogue,
)
from ied_client.explain.model import RESERVED_PREFIXES
from mms_protocol import codes as mms_codes


@pytest.fixture(scope="module")
def cat() -> Catalogue:
    return default_catalogue()


# ---------------------------------------------------------------------------------------------------
# EXP-7: minimum coverage (acceptance criterion)
# ---------------------------------------------------------------------------------------------------
def test_coverage_reports_nothing_missing(cat: Catalogue) -> None:
    missing = {domain: keys for domain, keys in cat.coverage().items() if keys}
    assert missing == {}


def _all_required_keys() -> list[str]:
    keys = [mms_codes.ied_error(n).key for n in mms_codes.IED_ERRORS]
    keys += [mms_codes.data_access_error(n).key for n in mms_codes.DATA_ACCESS_ERRORS]
    keys += [codes.add_cause(n).key for n in codes.ADD_CAUSES]
    keys += [codes.ctl_error(n).key for n in codes.CTL_ERRORS]
    keys += [codes.association(name).key for name in codes.ASSOCIATION_OUTCOMES]
    keys += [codes.info(mms_codes.MmsDomain.ACSE_DIAG, n).key for n in mms_codes.ACSE_USER_DIAGNOSTICS]
    keys += [mms_codes.mms_error(n).key for n in mms_codes.MMS_ERRORS]
    keys += [codes.tool(name).key for name in REQUIRED_TOOL_CODES]
    keys += [codes.check(name).key for name in REQUIRED_CHECK_IDS]
    return keys


@pytest.mark.parametrize("key", _all_required_keys())
def test_every_required_code_has_its_own_hint_and_explanation(cat: Catalogue, key: str) -> None:
    hint = cat.hint(codes.parse_key(key))
    assert hint is not None, key
    assert hint.key == key
    # an exact, context-free entry, not a "<domain>:*" fallback
    entry = cat.entry(hint.entry_id)
    assert entry is not None and key in entry.keys and not entry.when, (key, hint.entry_id)
    exp = cat.explain(key)
    assert exp is not None and exp.kind == "error" and key in exp.keys


def test_coverage_counts_all_domains(cat: Catalogue) -> None:
    """The client's domains plus those the MMS module registers."""
    assert set(cat.coverage()) == {d.value for d in codes.Domain} | {d.value for d in mms_codes.MmsDomain}


@pytest.mark.parametrize(
    "key", ["ied:ok", "mms:none", "ctl-error:no-error", "association:accepted", "acse-diag:null"]
)
def test_success_codes_have_a_trivial_fact(cat: Catalogue, key: str) -> None:
    hint = cat.hint(key)
    assert hint is not None and hint.certainty is Certainty.FACT


def test_the_catalogue_covers_tool_and_check_codes_used_in_the_source_tree(cat: Catalogue) -> None:
    """Guard: every literal codes.tool("…") / codes.check("…") in src/ has its own catalogue entry."""
    src = Path(__file__).resolve().parents[3] / "src" / "ied_client"
    pattern = re.compile(r'codes\.(tool|check)\(\s*"([a-z0-9-]+)"\s*\)')
    used: set[str] = set()
    for path in src.rglob("*.py"):
        for domain, name in pattern.findall(path.read_text(encoding="utf-8")):
            used.add(f"{domain}:{name}")
    assert used, "expected the core to raise some tool/check codes"
    missing = sorted(k for k in used if not any(not e.when for e in cat.entries if k in e.keys))
    assert missing == [], f"add catalogue entries (and REQUIRED_* names) for: {missing}"


# ---------------------------------------------------------------------------------------------------
# EXP-2: glossary terms
# ---------------------------------------------------------------------------------------------------
LN_CLASSES = [
    "XCBR",
    "XSWI",
    "CSWI",
    "CILO",
    "CSYN",
    "CALH",
    "PTOC",
    "PDIS",
    "PDIF",
    "PTRC",
    "PIOC",
    "PTOV",
    "PTUV",
    "PTOF",
    "PTUF",
    "PTTR",
    "RREC",
    "RBRF",
    "RSYN",
    "RDIR",
    "RDRE",
    "RFLO",
    "MMXU",
    "MMXN",
    "MSQI",
    "MMTR",
    "MHAI",
    "GGIO",
    "GAPC",
    "LLN0",
    "LPHD",
    "TCTR",
    "TVTR",
    "YPTR",
    "YLTC",
    "ATCC",
    "LGOS",
    "LSVS",
    "LTRK",
    "LCCH",
    "LTIM",
    "LTMS",
    "GSAL",
    "SIMG",
]
CDCS = [
    "SPS",
    "DPS",
    "INS",
    "ENS",
    "ACT",
    "ACD",
    "SEC",
    "BCR",
    "MV",
    "CMV",
    "SAV",
    "WYE",
    "DEL",
    "SEQ",
    "SPC",
    "DPC",
    "INC",
    "ENC",
    "BSC",
    "ISC",
    "APC",
    "BAC",
    "SPG",
    "ING",
    "ENG",
    "ASG",
    "CURVE",
    "CSG",
    "ORG",
    "TSG",
    "VSG",
    "VSS",
    "LPL",
    "DPL",
    "CSD",
    "HST",
]
RCB_ATTRIBUTES = [
    "RptID",
    "RptEna",
    "Resv",
    "ResvTms",
    "Owner",
    "DatSet",
    "ConfRev",
    "OptFlds",
    "BufTm",
    "SqNum",
    "TrgOps",
    "IntgPd",
    "GI",
    "PurgeBuf",
    "EntryID",
    "TimeOfEntry",
    "BufOvfl",
]
SGCB_ATTRIBUTES = ["NumOfSG", "ActSG", "EditSG", "CnfEdit", "LActTm"]
CONTROL_TERMS = [
    "SBO",
    "SBOw",
    "Oper",
    "Cancel",
    "CommandTermination",
    "origin",
    "orIdent",
    "orCat",
    "ctlNum",
    "Test",
    "Check",
    "interlocking",
    "interlock",
    "synchrocheck",
    "Loc",
    "LocSta",
    "LocKey",
    "Mod",
    "Beh",
    "Health",
    "switching hierarchy",
    "ctlModel",
    "ctlVal",
    "stVal",
    "sboTimeout",
    "LastApplError",
    "AddCause",
]
TRGOPS_BITS = ["dchg", "qchg", "dupd", "period", "gi"]
OPTFLDS_BITS = [
    "seqNum",
    "OptFlds.timeStamp",
    "reasonCode",
    "OptFlds.dataSet",
    "dataRef",
    "OptFlds.bufOvfl",
    "OptFlds.entryID",
    "OptFlds.confRev",
    "segmentation",
]
QUALITY_TERMS = [
    "quality",
    "validity",
    "detailQual",
    "oldData",
    "quality.source",
    "quality.test",
    "operatorBlocked",
]
EDITION_TERMS = ["Ed1", "Ed2", "Ed2.1", "edition"]
MMS_ACSE_TERMS = [
    "association",
    "association slots",
    "MMS initiate",
    "PDU size",
    "domain",
    "named variable",
    "named variable list",
    "dataset",
    "GetNameList",
    "GetVariableAccessAttributes",
    "Identify",
    "file services",
    "ACSE",
    "COTP",
    "MMS",
]
OUT_OF_SCOPE = ["GOOSE", "Sampled Values", "IEC 62351", "TLS", "ACSE authentication", "Wireshark"]
MODELLING = [
    "LD",
    "LN",
    "DO",
    "DA",
    "FC",
    "CDC",
    "SCL",
    "SCD",
    "CID",
    "object reference",
    "ldNs",
    "NamPlt",
    "PhyNam",
]


@pytest.mark.parametrize(
    "term",
    LN_CLASSES
    + CDCS
    + list(codes.FCS)
    + list(codes.CTL_MODELS.values())
    + [f"orCat={n}" for n in codes.OR_CATS]
    + [name for name in codes.OR_CATS.values() if name != "not-supported"]
    + RCB_ATTRIBUTES
    + SGCB_ATTRIBUTES
    + CONTROL_TERMS
    + TRGOPS_BITS
    + OPTFLDS_BITS
    + QUALITY_TERMS
    + EDITION_TERMS
    + MMS_ACSE_TERMS
    + OUT_OF_SCOPE
    + MODELLING,
)
def test_glossary_term_is_explained(cat: Catalogue, term: str) -> None:
    exp = cat.explain(term)
    assert exp is not None, term
    assert exp.kind in ("term", "concept"), (term, exp.id)
    assert exp.summary and exp.title


def test_ln_classes_cdcs_and_fcs_resolve_to_their_own_entry(cat: Catalogue) -> None:
    for name in LN_CLASSES:
        assert cat.explain(name).id == f"ln.{name.lower()}"  # type: ignore[union-attr]
    for name in CDCS:
        assert cat.explain(name).id == f"cdc.{name.lower()}"  # type: ignore[union-attr]
    for name in codes.FCS:
        assert cat.explain(name).id == f"fc.{name.lower()}"  # type: ignore[union-attr]


def test_out_of_scope_notes_say_so(cat: Catalogue) -> None:
    for term in ("GOOSE", "Sampled Values"):
        exp = cat.explain(term)
        assert exp is not None and "out of scope" in exp.title.lower()
        assert "cannot observe" in exp.summary
    assert "Wireshark" in cat.explain("GOOSE").details  # type: ignore[union-attr]
    security = cat.explain("TLS")
    assert security is not None and "not support" in security.summary


def test_fc_sv_is_not_sampled_values(cat: Catalogue) -> None:
    exp = cat.explain("SV")
    assert exp is not None and exp.id == "fc.sv" and "Not to be confused with Sampled Values" in exp.summary


# ---------------------------------------------------------------------------------------------------
# Content hygiene
# ---------------------------------------------------------------------------------------------------
def test_no_entry_text_is_empty_and_hints_are_single_lines(cat: Catalogue) -> None:
    for entry in cat.entries:
        exp = cat.explain(entry.id)
        assert exp is not None, entry.id
        assert exp.title.strip() and exp.summary.strip(), entry.id
        assert "\n" not in exp.title, entry.id
        for text in (*exp.next_checks, *exp.related):
            assert text.strip(), entry.id
        if entry.keys:
            assert entry.hint and entry.certainty, entry.id
            hint = cat.hint(
                entry.keys[0].replace("*", "unknown-12345") if entry.keys[0].endswith(":*") else entry.keys[0]
            )
            assert hint is not None
        if entry.hint is not None:
            assert entry.hint.strip() == entry.hint and "\n" not in entry.hint, entry.id
            assert not entry.hint.endswith("."), entry.id
            assert not entry.hint.casefold().startswith(RESERVED_PREFIXES), entry.id


def test_rendered_hints_are_single_lines_in_every_context_variant(cat: Catalogue) -> None:
    for entry in cat.entries:
        for key in entry.keys:
            if key.endswith(":*"):
                continue
            ctx = HintContext(**_context_for(entry))
            hint = cat.hint(key, ctx)
            assert hint is not None
            assert "\n" not in hint.render()


def _context_for(entry) -> dict:
    """A context that satisfies all of an entry's conditions (first listed value of each)."""
    ctx: dict = {"tags": {}}
    for cond in entry.when:
        value = cond.display[0]
        if cond.field.startswith("tags."):
            ctx["tags"][cond.field[5:]] = value
        else:
            ctx[cond.field] = value
    return ctx


def test_each_context_variant_wins_in_its_own_context(cat: Catalogue) -> None:
    for entry in cat.entries:
        if not entry.when:
            continue
        for key in entry.keys:
            hint = cat.hint(key, HintContext(**_context_for(entry)))
            assert hint is not None and hint.entry_id == entry.id, (key, entry.id, hint and hint.entry_id)


# Protocol-level entries may only claim a fact when the context proves it, or when the code itself
# is the proof (success codes, a local library state, an explicit authentication diagnostic).
FACT_WITHOUT_CONTEXT_ALLOWED = {
    "ied.ok",
    "ctl-error.no-error",
    "ied.not-connected",
    "ied.already-connected",
    "ied.service-not-implemented",
    "association.accepted",
    "acse-diag.null",
    "acse-diag.authentication",
}
PROTOCOL_DOMAINS = {"ied", "mms", "data-access", "add-cause", "ctl-error", "association", "acse-diag"}


def test_facts_are_only_stated_when_proved(cat: Catalogue) -> None:
    offenders = []
    for entry in cat.entries:
        if entry.certainty is not Certainty.FACT or entry.when:
            continue
        domains = {k.partition(":")[0] for k in entry.keys}
        if domains & PROTOCOL_DOMAINS and entry.id not in FACT_WITHOUT_CONTEXT_ALLOWED:
            offenders.append(entry.id)
    assert offenders == []


def test_slots_probably_exhausted_is_only_likely(cat: Catalogue) -> None:
    hint = cat.hint("tool:slots-probably-exhausted")
    assert hint is not None and hint.certainty is Certainty.LIKELY
    assert hint.render().startswith("Likely cause: ")


def test_local_address_missing_gives_the_ip_addr_add_remedy(cat: Catalogue) -> None:
    hint = cat.hint("tool:local-address-missing")
    assert hint is not None
    assert "ip addr add <local_ip>/<prefix> dev <interface>" in hint.text


def test_catalogue_lint_is_clean(cat: Catalogue) -> None:
    assert cat.lint() == []


def test_terms_are_unique_across_builtin_files(cat: Catalogue) -> None:
    # loading would raise otherwise; also make sure lookups are not shadowed by entry ids
    seen: dict[str, str] = {}
    for entry in cat.entries:
        for term in entry.terms:
            norm = re.sub(r"[^a-z0-9]", "", term.casefold())
            assert seen.setdefault(norm, entry.id) == entry.id, (term, entry.id, seen[norm])
