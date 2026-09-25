# mms-client — Advanced Instructions

This is the reference for someone already comfortable with IEC 61850, MMS, and the CLI basics
(if you need those first, see [basic-instructions.md](basic-instructions.md)). It covers what
the basic guide leaves out: the JSON contract, scripting against the core library directly,
`--as`/`--bind`, the diagnosis engine's raw probes, the explanation/quirks data files as an
editable knowledge base, the adapter's ctypes design and its consequences, and the dev/test
workflow.

Authoritative source of truth throughout: [SPEC.md](SPEC.md) (requirement IDs like `CTL-2`
are cited below and are grep-able in the spec), [docs/architecture.md](docs/architecture.md),
and [docs/spikes.md](docs/spikes.md) (Phase 0 findings — read this before you trust anything
about concurrency or memory behaviour).

---

## 1. Architecture, in one paragraph

```
CLI (shell + one-shot)      ied_client.cli          argparse registry, prompt_toolkit, rich
      │
Check engine                ied_client.verify       checks → CheckResult (status, category, evidence, raw)
      │                     ied_client.diagnosis    layered diagnosis engine, discover
Core client                 ied_client.core         session, model, read/write, controls, reports,
      │                                             setting groups, files, identity, log, restore
      │                     ied_client.scl          SCL parser (Ed1/Ed2/Ed2.1), expected model
      │                     ied_client.explain      hint catalogue (YAML data)
Protocol contract           ied_client.protocol     ProtocolModule / Association / Naming, shared types
      ▼  entry point ied_client.protocols: mms = "mms_protocol:protocol"
MMS protocol module         mms_protocol            association, MMS naming, MMS codes, hints, quirks
      │                     mms_protocol.diagnosis  raw COTP/ISO/ACSE/MMS probes, classification
Adapter                     mms_protocol.adapter    the only code touching pyiec61850-ng (PLT-4)
      │
libiec61850 1.6.1, as bundled in the pyiec61850-ng 1.6.1.10 wheel
```

The client (`ied_client`) is protocol-independent: it works in ACSI terms (`ObjectRef`,
`DatasetRef`, FCs, CDCs) and reaches the device only through the `Association` a protocol module
opens. The MMS module is the one plugged in by default; `--protocol NAME` or `protocol:` on an
inventory device picks another. [docs/architecture.md](docs/architecture.md) describes the
contract and how to add a module.

The structural rules are enforced by `tests/unit/test_architecture.py`, not just documented:

- **PLT-4** — nothing outside `mms_protocol.adapter` imports `pyiec61850` (checked by AST walk
  over every source file, including detecting `importlib`-based indirection).
- **ARC-5** — nothing in `ied_client` imports `mms_protocol`; protocol modules are found only
  through the entry points. The contract package `ied_client.protocol` depends on nothing but
  `ied_client.codes` and `ied_client.core.refs`.
- **ARC-1** — the CLI doesn't reach into a protocol module; anything the CLI can do is callable
  from plain Python, because the future pytest phase (§18 of the spec) will be built directly
  on `ied_client.core`, not on the CLI.

If you're extending the tool, put protocol-touching code in the protocol module (library calls in
its adapter), business logic in `core`/`verify`/`diagnosis`, and keep the CLI a thin renderer.
The architecture test will fail your PR if you don't.

---

## 2. Why the adapter uses ctypes instead of the SWIG bindings (read this before touching `adapter/`)

This is documented in full in `docs/spikes.md` RSK-1, but the short version matters for anyone
extending the adapter:

- The pyiec61850-ng SWIG module isn't built with `-threads`. Only four wrapped functions
  (`pyWrap_IedConnection_readObject/writeObject/getRCBValues/setRCBValues`) release the GIL.
  Everything else — `operate`/`select`/`cancel`, directory services, dataset reads, file
  services, `MmsConnection_identify` — holds the GIL for the duration of the call.
- Reports and CommandTermination arrive as C→Python callbacks on libiec61850's own receive
  thread, which needs the GIL to run them. If the main thread is blocked in one of those
  GIL-holding SWIG calls while a callback needs to fire, you get a **permanent deadlock**
  (reproduced: 500 concurrent `operate` calls against ~50 reports/s hung until killed).
- **Workaround:** `adapter/_native.py` resolves libiec61850 symbols through the SWIG
  extension's own loaded handle (so it's guaranteed to be exactly the wheel's build) and
  declares ctypes prototypes directly — bypassing SWIG's Python wrapper layer entirely. Every
  ctypes foreign call releases the GIL automatically. Callbacks are `ctypes.CFUNCTYPE`s that
  **only copy data into Python objects and never issue new requests** (a request issued from
  the connection thread would block waiting for a response that the very same thread is
  supposed to deliver).
- Verified: 3000 reads and 3000 direct-enhanced operates concurrent with reports and
  CommandTerminations, no deadlock.

Practical consequences if you're extending `adapter/client.py`:

1. **Never call back into a blocking request from inside a callback.** Callbacks put decoded
   objects on a `queue.Queue` (reports) or set a condition variable (CommandTermination); the
   consuming loop runs on the normal calling thread.
2. **CommandTermination polarity is decided after arrival**, not from the callback's own
   signal — libiec61850 can invoke CommandTermination *before* storing the LastApplError that
   goes with a negative termination. The adapter compares LastApplError against its
   pre-operate value with a 150 ms grace period. If you touch this logic, re-read RSK-1's
   "CommandTermination detail" paragraph first.
3. Other SWIG bugs found and avoided entirely by going through ctypes: `EventSubscriber`
   double-free on GC (segfault, reproduced), NULL-check typemaps rejecting NULLs the C API
   accepts, and wrong `RCB_ELEMENT_*` bitmasks in `pyiec61850.mms.reporting` (0x04 is `Resv`,
   not `RptEna` — the high-level wrapper silently writes the wrong attribute). Don't use that
   high-level wrapper for anything; it isn't used anywhere in this codebase.
4. Every native handle (`MmsValue`, `LinkedList`, `MmsVariableSpecification`,
   `ClientReportControlBlock`, `FileDirectoryEntry`, `MmsServerIdentity`,
   `ControlObjectClient`) must be owned and explicitly freed by the adapter before plain
   Python data crosses back out (`adapter/types.py` dataclasses only — nothing native crosses
   the boundary). This is what keeps RSK-5's soak test clean (10k read+write cycles, RSS
   growth 284 kB over the run). If you add a new native call, free what you allocate in the
   same function, including on the exception path.
5. Reading a whole logical node without an FC fails with `mms:parsing-response` on this stack
   — always read per DO and per FC (a spike finding, not a design choice you can revisit
   without re-testing against the stack).
6. A dataset member that lives in another LD, on an LD that has an `ldName`, crashes the
   libiec61850 1.6.1 **server** on GetDataSetDirectory. This is a server-side bug you'll hit if
   you write SCL for the simulator; the shipped fixtures avoid it deliberately.
7. `authority-probe` cannot test orCat on `sbo-with-normal-security` objects — libiec61850's
   SBO-normal select carries no origin at the protocol level. The tool says so rather than
   silently skipping it; don't try to "fix" this without changing the wire protocol.

---

## 3. The `--as` / `--bind` distinction and why both exist

`--as <client>` (NBR-1) makes the tool assume the *identity* of a client from the inventory:
its `source_ip` and its configured RCBs. This is how you reproduce "does the Station Manager's
association actually work", using the tool as a stand-in for the Station Manager, without
needing the Station Manager itself available.

`--bind <ip>` (NBR-2) is the lower-level mechanism `--as` uses under the hood
(`IedConnection_setLocalAddress`, confirmed working in RSK-2 — a bound association shows up at
the server with that peer address, and RCB `Owner` reflects the bound IP in the
`…7f000002`-style byte encoding). Use `--bind` directly when you want a specific source IP
without pulling in a whole named client's RCB list.

Before binding, the tool checks the address is actually configured on a local interface and,
if not, **prints the exact `ip addr add` command** to run — it won't silently fall back to the
default source address. If you're regularly using several source IPs against the same rack,
add them to the interface once (`sudo ip addr add 10.0.0.5/24 dev eth0`) rather than doing it
per session.

```sh
mms-client diagnose relay-F12 --as station-manager-1     # both source IP and RCBs come from the inventory
mms-client read relay-F12 CTRL/CSWI1.Pos.stVal --bind 10.0.0.9   # just a specific source IP
```

`--as` and `--bind` are session-scoped options — they must be given at `connect` time (or as a
global option on a one-shot command); you can't change the bound IP of an association that's
already open.

---

## 4. The JSON contract (`--json`, ARC-3)

Every command accepts `--json` and, when given, emits **exactly one** JSON document to stdout
and prompts for nothing (this is also how non-interactive/scripted use is detected — see §6 on
confirmation behaviour). The envelope is stable across commands:

```json
{
  "schema_version": "1.0",
  "tool": {
    "name": "mms-client",
    "version": "0.1.0",
    "pyiec61850_ng": "1.6.1.10",
    "libiec61850": "1.6.1"
  },
  "generated_at": "2026-09-25T14:51:30.904Z",
  "command": "read",
  "device": "bcu1",
  "ok": true,
  "result": { "...": "command-specific payload" },
  "exit_code": 0
}
```

`result` is command-specific but individually stable — e.g. `read`'s result always has
`reference`, `fc`, `mms` (the raw MMS variable name), `type`, `value`, `quality`, `timestamp`,
`error`, `duration_s`. A failed command still emits a well-formed envelope with `"ok": false`
and populates `result.error` — parse this instead of scraping text, and instead of relying on
the process exit code alone if you need the *reason*, not just success/failure.

`diagnose --json`'s `result` is worth knowing in detail if you're building tooling on top of
it: `layers` (list, each with `layer`, `status`, `summary`, and a `results` list of individual
probe results), `stopped_at`, `verdict`, `next_steps`, `findings`, `association_failure`,
`probes`, `duration_s`, `notes`. Each probe result under a layer carries not just pass/fail but
`evidence.raw`, which for the transport/session layers includes **the literal bytes sent and
received** and a parsed breakdown (COTP CC parameters, ISO session SPDU fields, ACSE AARE
diagnostics down to the individual context results). This is deliberate — DIA-3 requires that
libiec61850's generic connect failures be decomposable into which exact layer rejected the
association, and the raw bytes are there so you (or a colleague who knows the wire protocol
better than the hint catalogue does) can verify the tool's classification independently rather
than trusting it blindly.

Exit codes (stable, script against these):

| Code | Meaning |
|---|---|
| 0 | ok |
| 1 | the command failed, or the device refused it |
| 2 | usage error (bad command line) |
| 3 | the association could not be opened, or was lost |
| 4 | refused by the tool itself (mode/safety guardrail), or not confirmed |
| 130 | interrupted (Ctrl-C) |

Exit code 4 is worth distinguishing from 1 in automation: it means the tool itself declined to
proceed (wrong mode, a control that needed confirmation it couldn't get), not that the device
rejected anything.

---

## 5. Scripting against the core library directly (bypassing the CLI)

ARC-1 exists specifically so you're not limited to the CLI. `ied_client.core.session.Session`
is the object the CLI itself drives — everything the shell does is a thin call into it. Minimal
example:

```python
from ied_client.core.session import Session, resolve_target
from ied_client.core.safety import Policy, NonInteractive
from ied_client.core import readwrite
from ied_client.codes import ErrorInfo  # for typed error handling

session = Session(
    resolve_target("10.0.0.12", None),  # or a device name + Inventory; protocol="mms" is the default
    policy=Policy(),                     # mode, safety profile
    ui=NonInteractive(),                 # never blocks waiting for input (IDN-5) — use Scripted() in tests
)
session.connect()
result = readwrite.read(session, "CTRL/CSWI1.Pos.stVal")
```

Key things to know before building on this:

- `Session.ui` is the `Interaction` protocol (`core/safety.py`) through which the core asks the
  operator anything — object name confirmation, y/N, the orCat question. `NonInteractive` is
  what one-shot `--json` and any future automated-testing phase use: it **never blocks**, and
  answers every question by refusing (IDN-5 — this is why `--json` runs never hang waiting for
  a TTY that isn't there). `Scripted` drives the test suite by feeding canned answers; use it
  if you're writing your own tests against `Session` rather than mocking `input()`.
- Every RCB/edit-session change the core makes is registered in `Session.cleanups` **before**
  it's made, and unwound in reverse order on `disconnect()`, on `KeyboardInterrupt` (SIGTERM is
  mapped to `KeyboardInterrupt`), or on scope exit — this is RPT-7 and it's not CLI-specific.
  If you drive a `Session` from your own script and skip calling `disconnect()`, you skip
  cleanup; wrap your usage in `try`/`finally` or a context manager.
- `Session.last_error` (a `LastError`, carrying an `ErrorInfo` plus context) is what backs
  `explain last`. If you're scripting, you can read it directly instead of shelling out to
  `explain`.
- Reads/writes/controls all return the same structured dataclasses the CLI renders
  (`ied_client/protocol/types.py`: `VarSpec`, `BitString`, `UtcTime`, `RcbValues`, `Report`,
  `ControlStepResult`, …) — nothing native (no ctypes pointers, no SWIG objects) ever crosses
  out of a protocol module, so these are safe to hold onto, pickle, or pass to other code.
- `session.client` is the protocol module's `Association` (`ied_client/protocol/api.py`) if you
  need a service the core does not wrap; for MMS it wraps `mms_protocol.adapter.IedClient`.
- `ied_client.core.results.envelope(...)` is the exact function the CLI uses to build the JSON
  document in §4 — call it yourself if you want your script's output to be indistinguishable
  from the CLI's `--json` output for downstream tooling.

This is also where the future pytest phase (§18, deferred) is expected to attach: fixtures and
assertions directly on `CheckResult`/`CheckReport` from `ied_client.verify`, not on CLI text
output.

---

## 6. Safety/confirmation internals, for anyone automating around them

`core/safety.py`'s `Policy` combines three things: `mode` (standard/expert), `safety` (the
inventory's `lab`/`strict`), and whether `--yes` was passed. A few points that aren't obvious
from the CLI surface:

- `--yes` is implemented as *skip the confirmation prompt for a write*, full stop — it is not a
  blanket "assume yes" flag, and controls/takeovers do not consult it at all (SAF-3, enforced
  structurally, not just by convention: the control/takeover confirmation path doesn't accept a
  `--yes` bypass parameter).
- A run that has no way to ask (non-interactive: `--json`, or stdin isn't a TTY) uses
  `NonInteractive`, which **answers every confirmation question by refusing** rather than
  hanging. This means a script that calls `operate` without `--json` but with stdin redirected
  from `/dev/null` (or a pipe, as in CI) will get exit code 4, not a hang and not an accidental
  yes. If you need controls in a fully automated pipeline, that pipeline needs a human in the
  loop at that step by design — this is intentional, not a gap to work around.
- The `strict` typed-confirmation string is the **bare object reference relative to the
  device**, e.g. `GGIO1.SPCSO1` for `BCU1CTRL/GGIO1.SPCSO1` (you saw this in the pre-flight
  output's `(confirm by typing ...)` line) — build any wrapper tooling's prompts around that
  exact string, not the full reference with the logical device prefix.

---

## 7. Diagnosis internals (`ied_client.diagnosis`, `mms_protocol.diagnosis`)

`diagnose` (DIA-1) runs layers in a fixed order and **stops at the first failing one**:
network → transport/session → MMS initiate → identity → model → reports → controls (only with
`--controls`). The engine (`ied_client/diagnosis/diagnose.py`) owns the order and the ACSI
layers; the layers below identity come from the protocol module (`ConnectionDiagnosis`). For
MMS each is independently callable if you're scripting around specific failures rather than
always wanting the full sequence — see `mms_protocol/diagnosis/probes.py` (raw
TCP/COTP/session/presentation/ACSE/MMS-initiate probes), `classify.py` (turns a libiec61850
generic connect failure into a specific layer classification, DIA-3) and `connection.py` (the
MMS layers as `diagnose` runs them).

`classify.py`'s module docstring documents the **observed libiec61850 server behaviour when
association slots are exhausted** and which layer each of our own probes sees each failure
at — read it before trying to interpret a slot-exhaustion failure from raw evidence yourself;
someone already characterised the pattern.

DIA-6 (security detection): when TCP 102 is closed but TCP 3782 (the IEC 62351 TLS port) is
open, or an association is rejected specifically for authentication, `diagnose` says so
explicitly instead of reporting a generic failure. v1 has no ACSE authentication or TLS support
at all (§2.3) — this detection exists purely so operators aren't left debugging a "connection
failure" that's actually "this device requires security we don't implement."

`discover <subnet>` (DIA-5) is sequential and rate-limited by design (`--delay`, default
0.2 s between hosts) and releases each association before moving to the next — it is not
meant to be sped up by parallelising; a subnet full of IEDs with tight association limits will
tell you no if you hammer it concurrently. `--allow-large` is a deliberate friction point for
scanning anything bigger than a /22; don't script around it without first checking why the
default exists (it's there so a typo'd subnet doesn't turn into scanning a /8).

---

## 8. Verification internals: SCL parsing, snapshots, and the ignore file

`ied_client.scl` implements its own SCL (SCD/CID/ICD/IID) parser for both the 2003 (Ed1) and
2007 (Ed2/Ed2.1) schemas, including mixed-edition SCDs (VER-4) — **pyiec61850-ng has no Python
SCL parser**, so this is original parsing code, not a wrapper. If you're debugging a
reference-comparison mismatch, `ied_client/scl/parser.py`, `expected.py` (builds the expected
device model from parsed SCL) and `edition.py` (edition inference, see §9 below) are the
modules to read.

`check` (§12 of the spec) always runs self-consistency checks regardless of whether a reference
is given, then layers on SCD/CID/snapshot/device-supplied-SCL comparison if one is available or
found (`--device-scl`). Results are reported in **Configuration** and **Communication**
categories that are never merged (VER-7) — if you're parsing `check --json` output
programmatically, treat these as two independent pass/fail signals, not as inputs to compute a
single verdict yourself; the spec is explicit that combining them is a design decision, not an
oversight to work around.

**Snapshot layering** (VER-5, three layers, each with its own diff default) is worth
internalising if you're deciding what to compare against what:

| Layer | Contents | Diff default |
|---|---|---|
| Structure | LD/LN/DO/DA tree, types, FCs; datasets and members; RCB/LCB/SGCB attributes | change = **fail** |
| Configuration values | readable CF/SP/DC/EX values; **active** SG values only; identity incl. ConfRev/swRev | change = **warn** |
| Operational state | `Mod`, `Beh`, `Health`, `Loc`, `LocSta`, `LocKey`, `LPHD.Sim`, `PhyHealth`, RCB runtime state (`RptEna`, `Resv`, `Owner`) | change = **info** |

Other ST/MX values are never captured — they're excluded by design, not a bug, because they'd
make every diff noisy with routine measurement churn (see the table's footnote in §12.2 of the
spec). Snapshots are read-only by construction: capturing a non-active setting group would
require switching groups, which is a write, so VER-9 forbids it outright — there's no flag to
override this.

`--ignore FILE` (VER-14) lists references to exclude from a `diff` — use this per experiment
for counters or anything else that changes on its own and would otherwise fail a diff every
time; it isn't a general noise filter, it's meant to be a short, deliberate, reviewed list
checked in alongside the experiment.

---

## 9. Identity and edition inference — precedence order and how to override it

IDN-2's precedence order, in full, because getting this wrong leads to debugging the wrong
layer:

1. Inventory override (`devices[].identity.edition` — explicit, always wins)
2. SCL (if a reference is in play)
3. `LLN0.NamPlt.ldNs` namespace string — `LDNS_RULES` in `core/identity.py` implements:
   2003 → Ed1, `2007` and `2007A` → Ed2, `2007B` and later → Ed2.1, **and this mapping is
   always marked `inferred`, never `confirmed`**, because many real Ed2 devices report
   `2007A` when they're actually Ed2.1-capable (this is flagged as open issue OI-9 in the spec
   — if you find a device where this mapping is wrong, that's exactly the kind of thing to put
   in an incident file and the quirks file, not just work around locally).
4. Model heuristics: presence of Ed2-only attributes (BRCB `ResvTms`/`Owner`, `LocSta`)
5. Ask the operator — but only if an edition-dependent check actually needs the answer
   (IDN-4), and the question always offers "don't know". Non-interactive runs and "don't know"
   both resolve to edition `unknown` (IDN-5) — edition-independent checks still run;
   edition-dependent ones report `not-run` with the reason, they don't guess.

Every identified attribute — not just edition — carries `source` and `confidence`
(`confirmed`/`inferred`/`operator-supplied`/`unknown`) and this is surfaced in both `info` and
`diagnose` output (you saw this above: `edition Ed2.1 (inferred, not confirmed)`). If you're
building anything that consumes identity programmatically, use the `--json` output's
confidence field rather than assuming a returned value is authoritative — IDN-3 exists
specifically so inferred data is never silently presented as confirmed.

`info --save-identity` writes what was found back into the inventory (after confirmation),
which is the intended way to stop being asked/inferring per session (IDN-6) — prefer this over
hand-editing the inventory's `identity:` block unless you already know the values from
documentation.

---

## 10. Extending the explanation and quirks catalogues

These are plain YAML, versioned in the repo, and are meant to be edited by anyone running the
tool, not just by developers — this is explicit in the spec (EXP-5) and in the quirks file's
own header comment.

**Hints** (`src/ied_client/data/hints/*.yaml`, one file per area — `control.yaml`,
`tool.yaml`, `checks.yaml` — plus the general `hints.yaml`, and the MMS module's own
`src/mms_protocol/data/hints/` — `codes.yaml`, `association.yaml`): keyed by
`"<domain>:<name>"` matching `ErrorInfo.key` (generic domains in `ied_client/codes.py`, MMS
domains in `mms_protocol/codes.py`) (e.g.
`data-access:object-access-denied`), or `check:<check id>` for check-result hints. Matching is
context-sensitive by design (EXP-3): the same error code can have different entries depending
on FC/CDC/service/mode (`object-access-denied` on FC=ST reads as "not writable by design"; on
FC=SP it reads as "check access rights / source IP" — these are two different catalogue
entries, not one generic message with substitution). Every entry must state its certainty —
`fact`/`likely`/`check` — and EXP-4 is explicit that a hint may state something as fact **only
when the tool has actually proved it**, not when it's merely the most probable explanation.
EXP-6: hint text must be original — IEC 61850 standard text must never be copied in verbatim
(copyright), so if you're adding an entry, write your own description of the behaviour rather
than pasting from the standard.

EXP-7 (acceptance criterion, not aspirational): v1 must cover every libiec61850
`IED_ERROR_*` code, every MMS DataAccessError value, every control AddCause value, and every
association/initiate rejection reason. If you add a new code to a `codes.py`, add its catalogue
entry in the same change — `check.unlisted` / a generic fallback entry exists precisely to make
an uncovered code visible rather than silently swallowed, treat seeing it as a bug to fix, not
normal output.

**Quirks** (`src/mms_protocol/data/quirks.yaml`, IDN-9): starts empty by design (no vendor IEDs
were available for Phase 0 — RSK-6 is explicitly "pending real devices"). Matching is by
`match.vendor`/`match.model`/`match.firmware` against the identity the device actually reports
(MMS Identify), each field an exact string or glob, case/whitespace-insensitive; when several
entries match, most-specific wins (exact beats glob, a firmware condition present counts as
more specific), and at equal specificity the most-recently-loaded entry wins — so if you're
layering an experiment-specific quirks file on top of the shared one, load order matters.
Fields are documented in the file's own header (`max_associations`,
`slot_release_after_abrupt_disconnect_s`, `refusal_behaviour`, `resv_tms_behaviour`, etc.) and
unknown fields are kept and shown as-is rather than rejected, so you can record something the
schema doesn't have a named field for yet. **Always cite `source`** (spike or incident file)
and `added` (date) — this file is the institutional memory the spec explicitly says doesn't
exist anywhere else (LOG-3: "there is currently no other record of rack failures").

Procedure for a new IED model (from RSK-6, this is the actual recommended sequence, not just a
suggestion): `diagnose`, `authority-probe` in expert mode across all orCats, `rcb` +
`subscribe` (note `ResvTms`/`Owner` behaviour specifically), `setgroup show`/`edit`, `info`
(identity sources and `ldNs` value) — then write the quirks entry from what you observed.

---

## 11. Known stack limitations worth knowing before you file a bug

These are established findings (`docs/spikes.md`), not open questions:

- Reading a whole LN without specifying FC fails (`mms:parsing-response`) on this
  libiec61850/pyiec61850-ng combination — the tool always reads per DO+FC; this isn't
  configurable and shouldn't be "fixed" without re-validating against the stack.
- libiec61850 1.6.1's **server** implementation crashes on `GetDataSetDirectory` for a dataset
  member that lives in another LD when that LD has an `ldName` set. This affects the simulator
  (avoided deliberately in the shipped fixtures) and will affect any real IED built on the same
  libiec61850 server version if its SCD does this.
- `authority-probe` cannot report orCat results for `sbo-with-normal-security` objects — the
  underlying SBO-normal Select carries no origin field at the protocol level in this stack.
- Everything under RSK-1–RSK-6 was validated **against the simulated IED only** — no real
  vendor IED was available during Phase 0. Anything RSK-6-tagged ("pending real devices",
  vendor variation in authority checks/RCB reservation/ResvTms/setting groups/identity
  reporting) needs re-validation per real model, which is exactly what the quirks file (§10) is
  for.

---

## 12. Development workflow

```sh
uv run pytest                                        # unit + integration; simulated IED spawns automatically
uv run pytest -m soak -s                              # RSK-5 memory soak (10k read/write cycles) — not run by default
uv run pytest tests/unit/test_architecture.py         # the PLT-4 / ARC-1 / ARC-5 structural checks, fast, run these first on any adapter/CLI change
uv run ruff check src tests
uv run python -m tests.sim --scl tests/fixtures/scl/bcu_ed2.cid --port 10102   # standalone simulator, see below
```

`addopts = "-m 'not soak'"` in `pyproject.toml` means the soak test never runs by default —
run it explicitly (`-m soak -s`) whenever you touch adapter memory handling (native object
lifetimes, callback registration) even though CI won't force you to.

### The simulated IED (`tests/sim/`)

It's a **real libiec61850 server** (not a mock), driven through the adapter's own ctypes
layer, built from a model config or directly from an SCL file
(`mms_protocol.libiec_config.to_libiec61850_config`). It runs as a **separate process** in the test suite
specifically so server-side callbacks never compete with the client under test for one
interpreter's GIL (this matters given §2 above). It has scriptable behaviour worth knowing
about if you're writing tests against it: switching-hierarchy authority checks with AddCause,
interlocking, failing execution (CommandTermination-), execution delays, write refusal or
silent non-application (for RW-5's "accepted but didn't apply" case), setting-group storage,
file service, edition, `Owner`, and a configurable association limit — i.e. it can reproduce
most of the failure modes this tool is meant to diagnose, on demand.

Fixtures used above: `tests/fixtures/scl/bcu_ed2.cid` (a normal Ed2 BCU),
`tests/fixtures/scl/ied_ed1.icd` (Ed1), `tests/fixtures/scl/broken_refs.cid` (deliberately
broken dataset/RCB references, for testing the self-consistency checks),
`tests/fixtures/scl/rack_mixed.scd` (mixed-edition SCD, for VER-4).

Run it standalone (as shown throughout this doc) for manual exploration, demos, or training
against a device that behaves like a real IED without needing rack access:

```sh
uv run python -m tests.sim --scl tests/fixtures/scl/bcu_ed2.cid --port 10102
export MMS_CLIENT_INVENTORY=/path/to/a/minimal-inventory.yaml   # point a device at 127.0.0.1:10102
uv run mms-client diagnose <that-device>
```

### Architecture-test-driven extension

Before adding a new command or touching the adapter, run
`uv run pytest tests/unit/test_architecture.py` first — it's fast (pure AST analysis, no
associations opened) and will tell you immediately if a new import violates PLT-4 or if a new
CLI handler reached past `core` into `adapter` internals, before you've written anything else
that depends on the violation.

---

## 13. Open issues to know about (spec §19)

Worth checking before you build something that assumes these are settled:

- **OI-9** — the `ldNs` → edition mapping (`LDNS_RULES`, `core/identity.py`) is a documented
  guess (2007A treated as Ed2, always inferred) because real devices are inconsistent about
  it; expect to correct this as real-device evidence accumulates via the quirks file.
- **OI-10** — the edition-compatibility check (IDN-8: object reference length limits differing
  by edition) is **not yet implemented**, pending the exact limits being taken from the
  standard rather than from memory. If you need this, get the numbers from the standard text
  itself, not from this codebase or from an LLM's recollection of it (EXP-6's "don't copy the
  standard" concern is about hint text; this concern is about correctness — getting a length
  limit wrong is worse than not implementing the check).
- **`assoc-probe`** (active measurement of max associations, refusal behaviour, slot-release
  timing) is explicitly deferred to v2 — don't expect it, and don't confuse it with the passive
  DIA-4 classification that v1 does have.
