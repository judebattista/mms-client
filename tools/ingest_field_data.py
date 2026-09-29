"""Review a field bundle from an offline laptop into the repository (IED-CLIENT-SPEC.md FLD-5).

    uv run python tools/ingest_field_data.py BUNDLE              # dry run: what is in it, what would happen
    uv run python tools/ingest_field_data.py BUNDLE --apply      # review each entry, then write
    uv run python tools/ingest_field_data.py BUNDLE --apply --accept-all

A bundle is what ``ied-client local export`` writes: the laptop's local quirks and explanation files,
the incident files they cite, and where they came from. Each entry is classified against the
repository and the ledger:

    already reviewed  its fingerprint is in the ledger                          skipped
    add               a new hint id / a quirk for a new vendor-model-firmware  appended
    change            a hint whose id exists: the laptop changed it            replaces the entry in place
    duplicate         already in the repository as it is                       ledger only
    conflict          a quirk for the same match, with other values            never automatic: merge by hand

With ``--apply`` each entry is accepted, rejected (with a reason, which the laptop sees after its next
upgrade) or skipped (it comes back next time); ``--accept-all`` accepts everything but conflicts.
Accepted hints go to ``field.yaml`` in the data tree that already holds their code domain (the
client's or a protocol module's; term and concept entries to the client's glossary), accepted quirks
to the protocol module's ``quirks.yaml`` with ``source`` pointing at the incident copied to
``field-data/incidents/``. The bundle is archived in ``field-data/bundles/`` and every decision gets a
row in ``src/ied_client/data/field-ledger.yaml``, which ships with the next package so that
``ied-client local status`` / ``local prune`` on the laptops know what happened (FLD-6).

Nothing is written unless everything validates: the resulting catalogue must load, and lint problems
it adds are reported.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as _dt
import difflib
import re
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from ied_client import codes, localdata, yamledit
from ied_client.codes import Domain
from ied_client.explain.catalogue import Catalogue, CatalogueError
from ied_client.protocol import registry as protocols
from ied_client.quirks import QuirkDB, QuirksError, _load_text

REPO = Path(__file__).resolve().parents[1]
FIELD_FILE = "field.yaml"
QUIRK_IGNORED = ("source", "added")  # fields that do not make two quirks different
FIELD_HEADER = """\
# Entries reviewed into the repository from field bundles (IED-CLIENT-SPEC.md FLD-5), added by
# tools/ingest_field_data.py. Each says which bundle it came from; move an entry to a better file
# whenever convenient (keep its id). Format: see the header of ied_client/data/hints.yaml.

version: 1
entries:
"""


# ---------------------------------------------------------------------------------------- repository
@dataclasses.dataclass
class Tree:
    """One package's data directory: the client's, or a protocol module's."""

    name: str  # "ied_client" or a protocol module's package, e.g. "mms_protocol"
    data: Path
    protocol: str | None = None  # the protocol module's name, None for the client

    def hints_sources(self) -> list[Path]:
        if self.protocol is None:
            out = [self.data / "hints.yaml", *sorted((self.data / "hints").glob("*.yaml")),
                   self.data / "glossary.yaml", *sorted((self.data / "glossary").glob("*.yaml"))]
            return [p for p in out if p.is_file()]
        return sorted((self.data / "hints").glob("*.yaml"))


@dataclasses.dataclass
class Repo:
    root: Path
    client: Tree
    modules: dict[str, Tree]  # protocol name -> tree
    texts: dict[Path, str]  # current (possibly already modified) text of every file we may touch

    @property
    def ledger_path(self) -> Path:
        return self.client.data / "field-ledger.yaml"

    def trees(self) -> list[Tree]:
        return [self.client, *(self.modules[n] for n in sorted(self.modules))]

    def text(self, path: Path) -> str:
        if path not in self.texts:
            self.texts[path] = path.read_text(encoding="utf-8") if path.exists() else ""
        return self.texts[path]

    def catalogue_sources(self) -> list[tuple[str, str, bool]]:
        """The repository's catalogue in the built-in load order (client, then modules by name)."""
        return [(self.label(p), self.text(p), True) for t in self.trees() for p in self.hints_sources(t)]

    def hints_sources(self, tree: Tree) -> list[Path]:
        extra = [p for p in self.texts if p.parent in (tree.data / "hints", tree.data / "glossary") and p.suffix == ".yaml"]
        return sorted(set(tree.hints_sources()) | set(extra), key=lambda p: self._order(tree, p))

    def _order(self, tree: Tree, p: Path) -> tuple[int, str]:
        if tree.protocol is not None:
            return (0, p.name)
        rel = p.relative_to(tree.data).as_posix()
        group = 0 if rel == "hints.yaml" else 1 if rel.startswith("hints/") else 2 if rel == "glossary.yaml" else 3
        return (group, p.name)

    def quirks_path(self, protocol: str) -> Path:
        return self.modules[protocol].data / "quirks.yaml"

    def label(self, p: Path) -> str:
        try:
            return p.relative_to(self.root).as_posix()
        except ValueError:
            return str(p)


def load_repo(root: Path) -> Repo:
    client = Tree("ied_client", root / "src" / "ied_client" / "data")
    if not (client.data / "hints.yaml").is_file():
        raise SystemExit(f"{root} does not look like the ied-client repository (no src/ied_client/data/hints.yaml)")
    modules = {}
    for name in protocols.available():
        pkg = type(protocols.get(name)).__module__.split(".")[0]
        data = root / "src" / pkg / "data"
        if data.is_dir():
            modules[name] = Tree(pkg, data, name)
    return Repo(root, client, modules, {})


# ---------------------------------------------------------------------------------------- classify
@dataclasses.dataclass
class Candidate:
    kind: str  # "hint" | "quirk"
    action: str  # "add" | "change" | "duplicate" | "conflict" | "reviewed"
    ref: str
    fingerprint: str
    entry: Any
    entry_text: str  # the entry as the laptop wrote it (hints), or as it will be written (quirks)
    origin: str  # "<layer>:<path>#<n>" in the bundle
    protocol: str | None = None
    target: Path | None = None  # file to write (add / change)
    target_index: int | None = None  # item to replace (change)
    diff: str = ""
    note: str = ""
    decision: str | None = None  # "accepted" | "rejected" | None (skip)
    reason: str | None = None


def _dump(entry: Any) -> str:
    return yamledit.dump_item(entry)


def _diff(old: str, new: str, old_label: str, new_label: str) -> str:
    return "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), old_label, new_label))


def _key_domains(entry: Mapping[str, Any]) -> set[str]:
    out = set()
    for k in entry.get("keys") or []:
        d = str(k).partition(":")[0]
        try:
            out.add(codes.domain(d))
        except ValueError:
            out.add(d)
    return out


def _route_hint(repo: Repo, entry: Mapping[str, Any]) -> Path:
    """Where a new entry goes: the tree that already holds most entries of its code domains."""
    kind = entry.get("kind") or ("error" if entry.get("keys") else "term")
    if kind != "error":
        return repo.client.data / "glossary" / FIELD_FILE
    domains = _key_domains(entry)
    best: tuple[int, int, Tree] | None = None
    for rank, tree in enumerate(repo.trees()):
        count = 0
        for p in repo.hints_sources(tree):
            for e in localdata.raw_entries(repo.text(p), "hints"):
                if isinstance(e, Mapping) and _key_domains(e) & domains:
                    count += 1
        if count and (best is None or count > best[0]):
            best = (count, -rank, tree)
    if best is None:
        client_domains = {str(d) for d in Domain}
        tree = repo.client if domains <= client_domains or not repo.modules else repo.trees()[1]
    else:
        tree = best[2]
    return tree.data / "hints" / FIELD_FILE


def _find_hint(repo: Repo, entry_id: str) -> tuple[Path, yamledit.Item] | None:
    found = None
    for tree in repo.trees():
        for p in repo.hints_sources(tree):
            for item in yamledit.items(repo.text(p), "entries"):
                if isinstance(item.value, Mapping) and str(item.value.get("id", "")).casefold() == entry_id.casefold():
                    found = (p, item)  # a later file replaces an earlier one
    return found


def _quirk_core(entry: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in entry.items() if k not in QUIRK_IGNORED}


def classify(bundle: Mapping[str, Any], repo: Repo) -> tuple[list[Candidate], list[str]]:
    """Every entry of the bundle's valid files, and notes about what was not looked at."""
    ledger = localdata.parse_ledger(repo.text(repo.ledger_path), repo.label(repo.ledger_path))
    out: list[Candidate] = []
    notes: list[str] = []
    for f in bundle.get("files") or []:
        where = f"{f['layer']}:{f['path']}"
        if not f.get("valid", True):
            notes.append(f"{where} was invalid on the laptop ({f.get('error')}); not reviewed")
            continue
        kind = "hint" if f["kind"] == "hints" else "quirk"
        items = yamledit.items(f["text"], localdata.LIST_KEY[f["kind"]])
        for item in items:
            entry = item.value
            fp = localdata.fingerprint(entry)
            ref = localdata.entry_ref(f["kind"], entry)
            c = Candidate(kind, "add", ref, fp, entry, yamledit.item_text(f["text"], item), f"{where}#{item.index}",
                          protocol=f.get("protocol"))
            row = ledger.get(fp)
            if row is not None:
                c.action, c.note = "reviewed", f"{row.decision} {row.reviewed or ''}".strip()
            elif kind == "hint":
                hit = _find_hint(repo, str(entry.get("id", "")))
                if hit is None:
                    c.target = _route_hint(repo, entry)
                else:
                    path, repo_item = hit
                    if repo_item.value == entry:
                        c.action, c.note = "duplicate", f"identical to {repo.label(path)}"
                    else:
                        c.action, c.target, c.target_index = "change", path, repo_item.index
                        c.diff = _diff(yamledit.item_text(repo.text(path), repo_item), c.entry_text,
                                       f"{repo.label(path)} (repository)", f"{where} (laptop)")
            else:
                proto = f.get("protocol")
                if proto not in repo.modules:
                    notes.append(f"{where}: protocol module {proto!r} is not in this repository; not reviewed")
                    continue
                c.target = repo.quirks_path(proto)
                key = localdata.quirk_match_key(entry)
                same = [e for e in localdata.raw_entries(repo.text(c.target), "quirks")
                        if isinstance(e, Mapping) and localdata.quirk_match_key(e) == key]
                if same and any(_quirk_core(e) == _quirk_core(entry) for e in same):
                    c.action, c.note = "duplicate", f"identical to an entry of {repo.label(c.target)}"
                elif same:
                    c.action = "conflict"
                    c.diff = _diff(_dump(_quirk_core(same[-1])), _dump(_quirk_core(entry)),
                                   f"{repo.label(c.target)} (repository)", f"{where} (laptop)")
            out.append(c)
    return out, notes


# ---------------------------------------------------------------------------------------- review
def review(cands: list[Candidate], *, accept_all: bool, ask: Callable[[str], str] | None) -> None:
    """Set each candidate's decision (duplicates are accepted: their content is already in the package)."""
    for c in cands:
        if c.action == "reviewed":
            continue
        if c.action == "duplicate":
            c.decision, c.reason = "accepted", "already in the repository"
            continue
        if accept_all:
            c.decision = None if c.action == "conflict" else "accepted"
            continue
        assert ask is not None
        print()
        print(describe(c, verbose=True))
        options = "[m]erged by hand, [r]eject, [s]kip" if c.action == "conflict" else "[a]ccept, [r]eject, [s]kip"
        while True:
            ans = ask(f"{options}? ").strip().lower()[:1]
            if ans in ("a", "m") and (ans == "m") == (c.action == "conflict"):
                c.decision = "accepted"
                c.reason = "merged by hand" if ans == "m" else None
                break
            if ans == "r":
                c.decision, c.reason = "rejected", ask("Reason (shown on the laptop): ").strip() or None
                break
            if ans == "s":
                c.decision = None
                break


def _shown(p: Path) -> str:
    try:
        return p.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return str(p)


def describe(c: Candidate, verbose: bool = False) -> str:
    where = f" → {_shown(c.target)}" if c.target is not None and c.action in ("add", "change") else ""
    line = f"{c.action:<10} {c.kind:<6} {c.ref}  ({c.origin}){where}"
    if c.note:
        line += f"\n           {c.note}"
    if verbose:
        body = c.diff or c.entry_text
        line += "\n" + "\n".join("    " + ln for ln in body.rstrip("\n").splitlines())
    return line


# ---------------------------------------------------------------------------------------- apply
def _bundle_stem(bundle: Mapping[str, Any], bundle_path: Path) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", bundle_path.stem)


def apply(cands: list[Candidate], bundle: Mapping[str, Any], bundle_path: Path, repo: Repo,
          today: str) -> tuple[dict[Path, str], dict[Path, str], list[str]]:
    """The new text of every changed file, the evidence files to write, and new lint problems.

    Raises SystemExit when the result would not load (nothing is written then).
    """
    before_lint = set(Catalogue.from_texts(repo.catalogue_sources()).lint())
    stem = _bundle_stem(bundle, bundle_path)
    evidence: dict[Path, str] = {}
    incidents = {i["path"]: i for i in bundle.get("incidents") or []}
    by_name = {i["name"]: i for i in bundle.get("incidents") or []}
    inc_dir = repo.root / "field-data" / "incidents" / stem
    tag = f"# field: {bundle_path.name} ({bundle.get('host')}, {str(bundle.get('created_at', ''))[:10]})\n"
    touched: set[Path] = set()

    # changes first (by index, which appends do not disturb), then additions
    for c in sorted((c for c in cands if c.decision == "accepted" and c.action == "change"), key=lambda c: -(c.target_index or 0)):
        assert c.target is not None and c.target_index is not None
        repo.texts[c.target] = yamledit.replace(repo.text(c.target), "entries", c.target_index, tag + c.entry_text)
        touched.add(c.target)
    for c in cands:
        if c.decision != "accepted" or c.action != "add":
            continue
        assert c.target is not None
        if c.kind == "hint":
            text = repo.text(c.target) or FIELD_HEADER
            repo.texts[c.target] = yamledit.append(text, "entries", [tag + c.entry_text])
        else:
            entry = dict(c.entry)
            src = str(entry.get("source") or "")
            inc = incidents.get(src) or by_name.get(Path(src).name) if src else None
            if inc is not None:
                dest = inc_dir / re.sub(r"[^A-Za-z0-9._-]+", "-", inc["name"])
                evidence[dest] = inc["content"]
                entry["source"] = repo.label(dest)
            elif src:
                entry["source"] = f"{src} (field: {bundle_path.name}, {bundle.get('host')})"
            else:
                entry["source"] = f"field bundle {bundle_path.name} ({bundle.get('host')})"
            repo.texts[c.target] = _append_quirk(repo.text(c.target), entry, c.target)
        touched.add(c.target)

    rows = [localdata.LedgerRow(c.fingerprint, c.kind, c.ref, c.decision, c.reason, bundle_path.name, today).to_json()
            for c in cands if c.decision is not None and c.action != "reviewed"]
    if rows:
        repo.texts[repo.ledger_path] = yamledit.append(repo.text(repo.ledger_path), "reviewed",
                                                       [yamledit.dump_item(r) for r in rows], blank_line=False)
        touched.add(repo.ledger_path)
        evidence[repo.root / "field-data" / "bundles" / bundle_path.name] = bundle_path.read_text(encoding="utf-8")

    # validate everything before anything is written
    try:
        cat = Catalogue.from_texts(repo.catalogue_sources())
    except CatalogueError as exc:
        raise SystemExit(f"the result would not load, nothing written: {exc}") from None
    for tree in repo.modules.values():
        qp = tree.data / "quirks.yaml"
        if qp in touched:
            try:
                _load_text(repo.text(qp), repo.label(qp))
            except QuirksError as exc:
                raise SystemExit(f"the result would not load, nothing written: {exc}") from None
    localdata.parse_ledger(repo.text(repo.ledger_path), repo.label(repo.ledger_path))
    new_lint = sorted(set(cat.lint()) - before_lint)
    return {p: repo.texts[p] for p in touched}, evidence, new_lint


def _append_quirk(text: str, entry: Mapping[str, Any], path: Path) -> str:
    """Append through QuirkDB.append_entry's own rules (validation, ``added``), on a scratch copy."""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d) / path.name
        tmp.write_text(text, encoding="utf-8")
        QuirkDB.append_entry(tmp, entry)
        return tmp.read_text(encoding="utf-8")


def commit_message(cands: list[Candidate], bundle: Mapping[str, Any], bundle_path: Path) -> str:
    acc = [c for c in cands if c.decision == "accepted" and c.action in ("add", "change")]
    rej = [c for c in cands if c.decision == "rejected"]
    lines = [f"Field data from {bundle.get('host')} ({str(bundle.get('created_at', ''))[:10]})", "",
             f"Reviewed {bundle_path.name} with tools/ingest_field_data.py (FLD-5)."]
    if acc:
        lines += ["", "Accepted:"] + [f"- {c.action} {c.kind} {c.ref}" for c in acc]
    if rej:
        lines += ["", "Rejected:"] + [f"- {c.kind} {c.ref}: {c.reason or 'no reason given'}" for c in rej]
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="tools/ingest_field_data.py", description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("bundle", type=Path, help="a field bundle (ied-client local export)")
    p.add_argument("--apply", action="store_true", help="review the entries and write the result")
    p.add_argument("--accept-all", action="store_true", help="with --apply: accept every entry except conflicts")
    p.add_argument("--repo-root", type=Path, default=REPO, help=argparse.SUPPRESS)
    p.add_argument("--date", default=_dt.date.today().isoformat(), help=argparse.SUPPRESS)
    args = p.parse_args(argv)

    import ied_client

    here = Path(ied_client.__file__).resolve()
    if args.repo_root == REPO and REPO / "src" not in here.parents:
        raise SystemExit(f"ied_client is imported from {here}, not from this checkout: run with `uv run` in {REPO}")
    try:
        bundle = localdata.read_bundle(args.bundle)
    except localdata.LocalDataError as exc:
        raise SystemExit(str(exc)) from None
    repo = load_repo(args.repo_root)
    tool = bundle.get("tool") or {}
    print(f"Bundle {args.bundle.name}: from {bundle.get('host')} (user {bundle.get('user')}), {bundle.get('created_at')}, "
          f"{tool.get('name')} {tool.get('package_version') or tool.get('version')}"
          + (f" commit {str(tool['git_commit'])[:12]}" if tool.get("git_commit") else ""))
    cands, notes = classify(bundle, repo)
    for n in notes:
        print(f"note: {n}")
    if not cands:
        print("Nothing to review.")
        return 0
    print()
    for c in cands:
        print(describe(c, verbose=not args.apply and c.action != "reviewed"))
    todo = [c for c in cands if c.action != "reviewed"]
    if not args.apply:
        print(f"\nDry run: {len(todo)} to review. Run again with --apply to review and write.")
        return 0
    if not todo:
        print("\nEverything in this bundle has been reviewed before.")
        return 0
    if not args.accept_all and not sys.stdin.isatty():
        raise SystemExit("--apply reviews each entry interactively: run it in a terminal, or add --accept-all")
    review(cands, accept_all=args.accept_all, ask=input)
    changed, evidence, new_lint = apply(cands, bundle, args.bundle, repo, args.date)
    for path, text in {**changed, **evidence}.items():
        localdata.atomic_write(path, text)
    print()
    for path in sorted({**changed, **evidence}):
        print(f"wrote {repo.label(path)}")
    for problem in new_lint:
        print(f"lint: {problem}")
    skipped = [c for c in cands if c.decision is None and c.action != "reviewed"]
    if skipped:
        print(f"{len(skipped)} entr{'y' if len(skipped) == 1 else 'ies'} skipped (not in the ledger; they come back next time):")
        for c in skipped:
            print(f"  {c.action} {c.kind} {c.ref}")
    print("\nNext: review `git diff`, run `uv run python -m pytest tests/unit/explain`, commit, bump the version "
          "in pyproject.toml, and build the package (packaging/build-deb.sh).")
    print("\nDraft commit message:\n")
    print(commit_message(cands, bundle, args.bundle))
    return 0


if __name__ == "__main__":
    sys.exit(main())
