# Field data: local quirks and explanations, and the way back (§13.1, FLD-1 … FLD-6)

A rack laptop may never be able to push to the repository. What its operators learn — a model
that only accepts four associations, a hint that is wrong for this rack — still has to be usable
on that laptop at once, and has to reach the repository, and from there every other machine,
eventually. It travels as one file carried by hand. The repository stays the source of truth,
and nothing gets into it without review.

```
laptop                                              repository
──────                                              ──────────
local add-quirk / local edit hints
   │  (used at once, marked [local])
local export ──── field bundle (USB) ────────────▶  tools/ingest_field_data.py BUNDLE --apply
                                                       │  accept / reject (reason) / skip
                                                       ▼
                                                    data files + field-data/ + ledger → commit, tag
                                                       │
apt install ./ied-client_<new>.deb ◀──── .deb (USB) ───┘  (CI builds it)
local status   (reviewed: accepted / rejected + reason)
local prune    (the package's version takes over)
```

## On the laptop

### Where local data lives

| Layer | Directory | Who writes it |
|---|---|---|
| machine | `/etc/ied-client/` | an administrator: `sudo ied-client local … --system` |
| user | `${XDG_CONFIG_HOME:-~/.config}/ied-client/` | the operator |

Both have the same layout:

```
hints/*.yaml               explanation catalogue entries — the format of ied_client/data/hints.yaml
quirks/<protocol>/*.yaml   quirks for one protocol module (quirks/mms/) — the format of its quirks.yaml
incidents/                 incident files that quirk `source` fields cite (incidents/<name>.json)
```

Load order is: the package's own data, then the machine layer, then the user layer. A later
layer wins. A catalogue entry with the **same id** as an earlier one replaces it completely, and
at equal specificity the quirk loaded last wins. `IED_CLIENT_LOCAL_DIRS=/a:/b` replaces both
layers: the first directory is the machine layer and the last is the user layer.

Package upgrades never change these files. A local file that does not load is left out, with a
warning, and the rest keeps working (FLD-3).

### Commands

```sh
ied-client local status                      # layers, files, validity, what became of each entry
ied-client local add-quirk relay-F12         # a quirk, matched on the identity the device reports
ied-client local edit hints                  # explanation entries in $EDITOR, validated before saving
ied-client local edit hints --from tool.refused-standard-mode   # start from a built-in entry to change it
ied-client local edit quirks                 # the quirks file itself (for things add-quirk cannot say)
ied-client local export --out /media/usb/    # the field bundle
ied-client local import other-laptop.json    # another laptop's bundle, into this machine's layer
ied-client local prune                       # after an upgrade: drop entries the package has reviewed
```

`local add-quirk` fills `match` with the **exact** vendor, model and firmware strings the device
reports, because a quirk is only used when these match. Use `--no-connect --vendor … --model …`
when the device cannot be reached. It then asks what was observed; non-interactively, give
`--set FIELD=VALUE`, which can be repeated. Every quirk also names its evidence, in one of three
ways:
- `--save-incident` saves an incident file from this session into the layer's `incidents/`
  directory.
- `--incident FILE` copies an existing incident file there.
- `--source TEXT` states the evidence in words.

Incident files kept in `incidents/` travel with the bundle.

Anything from a local file says so wherever it is used (FLD-2):
- One-line hints are prefixed `[local]`.
- `explain` prints `Source: local file …`.
- In JSON, hints and explanations carry `source`.
- Diagnosis findings say "a local quirks file (not yet reviewed into the package) records …".

## The field bundle (FLD-4)

`local export` writes one JSON file, named `ied-client-field-<host>-<date>.json` by default.
Its top-level fields:

```
kind: ied-client-field-data      schema_version: 1
created_at, host, user           where and when it was made
tool                             version, package_version, git_commit: which built-in data the laptop ran
layers[]                         name, path
files[]                          layer, path (e.g. hints/local.yaml), kind (hints | quirks), protocol,
                                 sha256, valid, error, text (verbatim, comments included)
incidents[]                      layer, path, name, sha256, content
entries[]                        kind, file, index, ref, fingerprint, state (local-only / overrides-built-in /
                                 ingested / rejected), detail
```

- **Checksums:** `read_bundle` checks every file and incident against its `sha256`, so a damaged
  or hand-edited bundle is refused.
- **Invalid files:** these are still exported, so the maintainer can see them, but they are not
  reviewed.
- **Fingerprints:** a fingerprint is the first 16 hex digits of the sha256 of the entry's
  canonical JSON (`ied_client.localdata.fingerprint`). It depends on the entry's content only, not
  on comments or formatting.

## In the repository: review (FLD-5)

```sh
uv run python tools/ingest_field_data.py ied-client-field-laptop3-20261002.json           # dry run
uv run python tools/ingest_field_data.py ied-client-field-laptop3-20261002.json --apply   # review, write
```

The dry run lists every entry with its classification and the text or diff involved:

| Classification | Meaning | On `--apply` |
|---|---|---|
| already reviewed | the fingerprint is in the ledger | skipped |
| add | a new hint id, or a quirk for a vendor/model/firmware the file does not have | accept → appended |
| change | a hint whose id exists (the laptop overrode a built-in entry); the diff is shown | accept → the entry is replaced in place |
| duplicate | already in the repository as it is (for quirks: ignoring `source` and `added`) | recorded in the ledger as accepted |
| conflict | a quirk for the same match with other values | never automatic: merge by hand, then answer "merged by hand"; or reject; or skip |

`--apply` asks for each entry whether to accept it, reject it with a reason (the laptop sees the
reason after its next upgrade), or skip it. A skipped entry is not recorded, so it comes back the
next time the bundle is reviewed. `--accept-all` accepts everything except conflicts.

Where accepted entries go:

* **New hints** are appended to `field.yaml` in the data tree that already holds most entries of
  their code domains:
  - `data-access:`, `mms:`, `ied:` and `acse-diag:` keys go to
    `src/mms_protocol/data/hints/field.yaml`.
  - `add-cause:`, `tool:` and `check:` keys go to `src/ied_client/data/hints/field.yaml`.
  - Term and concept entries go to `src/ied_client/data/glossary/field.yaml`.

  Move them to a better file whenever convenient, but keep the id.
* **Changed hints** are replaced where they are defined.
* **New quirks** are appended to the protocol module's `quirks.yaml`, through
  `QuirkDB.append_entry`. Their `source` is rewritten to the repository copy of the cited
  incident.

Every written entry is preceded by `# field: <bundle> (<host>, <date>)`.

Everything else in the data files is left byte for byte as it was (`ied_client.yamledit`), so
comments and layout survive. The bundle is archived in `field-data/bundles/`, and cited incidents
are copied to `field-data/incidents/<bundle>/`. Nothing is written unless the whole resulting
catalogue and the quirks files load. New catalogue lint problems (for example, two entries that
can match the same context with equal rank) are printed so they can be fixed before committing.

The tool ends by printing a draft commit message. The next steps are:
1. Review `git diff`.
2. Run `uv run python -m pytest tests/unit/explain`.
3. Commit, bump the version, and tag. CI builds the package.

## The ledger (FLD-6)

`src/ied_client/data/field-ledger.yaml` has one row per reviewed entry:

```yaml
- fingerprint: 50bf70901a38804f
  kind: quirk
  ref: SimVendor / SimIED / firmware 1.0
  decision: accepted          # or rejected
  reason: …                   # optional: why rejected, or how it was changed
  bundle: ied-client-field-laptop3-20261002.json
  reviewed: '2026-10-05'
```

The ledger ships in the package. After the laptop installs that package:
- `local status` shows each matching local entry as "reviewed into this package" or "reviewed,
  not accepted: <reason>".
- `local prune` removes those entries, so the package's version, possibly reworded during review,
  takes over.

An entry the operator edits after exporting it gets a new fingerprint and stays local.
