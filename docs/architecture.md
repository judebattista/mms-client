# Architecture

The code is two packages: the protocol-independent IED client (`ied_client`, IED-CLIENT-SPEC.md) and a
protocol module that plugs into it (`mms_protocol`, MMS-PROTOCOL-SPEC.md). The client never imports a
protocol module; it finds them through the `ied_client.protocols` entry-point group and talks to them only
through the contract in `ied_client.protocol`.

```
  CLI (shell + one-shot)          ied_client.cli          argparse registry, prompt_toolkit, rich
        │
  Check engine                    ied_client.verify       checks → CheckResult (status, category, evidence, raw)
        │                         ied_client.diagnosis    layered diagnosis engine, discover, TCP/ping probes
  Core client                     ied_client.core         session, model, read/write, controls, reports,
        │                                                 setting groups, files, identity, log, restore
        │                         ied_client.scl          SCL parser (Ed1/Ed2/Ed2.1), expected model
        │                         ied_client.explain      hint catalogue (YAML data)
        │
  Protocol contract               ied_client.protocol     ProtocolModule / Association / Naming protocols,
        │                                                 shared value types, errors, registry
        │                         ied_client.codes        generic codes + the open error-domain registry
        ▼  entry point  ied_client.protocols: mms = "mms_protocol:protocol"
  ─────────────────────────────────────────────────────────────────────────────────────────────────────
  MMS protocol module             mms_protocol            MmsProtocol, MmsAssociation, MmsNaming,
        │                                                 MMS error domains, hints, quirks
        │                         mms_protocol.diagnosis  COTP / ISO / ACSE / MMS-initiate probes, classification
  Adapter                         mms_protocol.adapter    the only code touching pyiec61850-ng (PLT-4)
        │
  libiec61850 1.6.1 as bundled in the pyiec61850-ng wheel
```

`tests/unit/test_architecture.py` enforces the boundaries by walking the imports (static, relative and
`import_module` string constants): only `mms_protocol.adapter` imports pyiec61850 (PLT-4); `_native` is
private to the adapter; nothing in `ied_client` imports `mms_protocol` (ARC-5); the contract package
`ied_client.protocol` depends only on itself, `ied_client.codes` and `ied_client.core.refs`; the CLI does not
reach into a protocol module (ARC-1). `tests/unit/test_protocol_plugin.py` plugs an in-memory fake protocol
into the unchanged client (session, browse, read/write, datasets, snapshot, diagnose, one-shot CLI) to show
the contract is sufficient on its own.

## Protocol contract (`ied_client.protocol`)

* `api.py` — `typing.Protocol` interfaces a module implements:
  * `ProtocolModule` — name, display name, default port, `names`, `open()` → `Association`, `versions()`,
    `describe_association()`, `connection_diagnosis()`, `classify_failed_association()` (for discover),
    the texts the client shows (`identify_source`, `blind_spots`, `sbo_normal_select_note`,
    `association_layers`, `next_steps`), and its data files (`catalogue_sources()`, `quirks_sources()`).
  * `Association` — one open association: identify, directory (LDs, LNs, LN type, datasets), read /
    read_many / write by `ObjectRef` with the device's `VarSpec`, RCB get/set and report handlers, files,
    `control(ref)` → `ControlObject`, release/abort/close, closed listeners.
  * `Naming` — the protocol's native spelling of references (`key` is the JSON field, e.g. `"mms"`). The
    core keeps `ObjectRef` / `DatasetRef`; native names are opaque strings it only prints and parses back.
  * `ConnectionDiagnosis` — the protocol's layers of `diagnose` below the ACSI (`run`) and its
    identity-dependent checks (`after_identity`).
* `types.py` — values and results that cross the boundary: `ValueKind`, `VarSpec`, `BitString`, `UtcTime`,
  `AccessError`, `ServerIdentity`, `DatasetRef`, `DataSetMember`, `RcbValues`, `Report`,
  `ControlStepResult`, `CommandTermination`, `FileEntry`. Nothing native crosses.
* `values.py` — `coerce` / `parse_text`: operator input → the device's own type (RW-3), shared by all
  protocols so the refusal messages are the same.
* `errors.py` — `ProtocolError`, `ServiceError`, `ConnectError`, `NotConnectedError`, `EncodeError`, each
  carrying an `ErrorInfo`.
* `registry.py` — `get(name)`, `available()`, `register()` (tests, embedding), lazy loading of the entry
  points; an unknown name is `tool:unknown-protocol` (exit 2).

`ied_client.codes` holds the ACSI code tables (AddCause, CtlError, ctlModel, orCat, FC, TrgOp, OptFlds, …)
and the generic domains (`add-cause`, `ctl-error`, `association`, `tool`, `check`). Protocol modules
register their own domains (`register_domain`, with aliases and a preference used to resolve bare names in
`explain`), association outcomes and constant prefixes. `ErrorInfo.domain` is a plain string, so one
spelling per code everywhere and one catalogue key (`data-access:object-access-denied`,
`add-cause:blocked-by-interlocking`, …) regardless of which module raised it.

## CLI (`ied_client.cli`)

One command registry serves the shell and one-shot mode (CLI-6). A command is a decorated handler in
`cli/commands/*.py` (`@command("name", "summary", area=..., needs=..., configure=..., service=...)`)
returning a `CommandResult` (data + text renderer); `cli/runner.py` parses, prepares the session, maps
exceptions to exit codes and raw error codes (CLI-7), adds catalogue hints (EXP-1, not with `--terse`),
logs the command, and prints either text or the JSON envelope (ARC-3). The shell (`cli/shell.py`,
prompt_toolkit) keeps one association, completes commands and object references from the browsed
model, and reads lines from any iterable so it is testable without a TTY. `--protocol NAME` (or the
inventory device's `protocol:`) picks the module; the default is `mms`. Exit codes: 0 ok, 1 failed /
refused by the device, 2 usage, 3 connection failed or lost, 4 refused by policy or not confirmed,
130 Ctrl-C.

## Core (`ied_client.core`)

* `Session` — one persistent association (CLI-1) opened through `session.protocol.open(...)`, the
  operator's settings (mode, orCat, terse), the cached model (MDL-4), the **cleanup registry** (RPT-7), the
  JSONL log (LOG-1), `last_error` for `explain last`. Every change the tool makes to an RCB or an edit
  session is registered *before* it is made, and undone in reverse order on exit, Ctrl-C (SIGTERM is mapped
  to KeyboardInterrupt) or disconnect; what cannot be undone (connection lost) is reported with its
  lingering effect. `session.parse_ref(text)` accepts IEC references and the protocol's native names.
* `model.py` — browsing through the `Association` directory services: LDs, LNs, one LN type description
  per LN, FC branches merged into an IEC view (LD → LN → DO → DA, each node knowing its FCs). Control
  blocks (RP/BR/LG/GO/MS/US and the SGCB) are kept separately.
* `readwrite.py`, `controls.py`, `reports.py`, `setgroup.py`, `files.py`, `restore.py`, `identity.py`,
  `datasets.py`, `incident.py` — one module per spec area. Each returns structured results that the CLI
  renders as text or JSON (ARC-3, `results.envelope`). Results that name an object carry both the IEC
  reference and the native name under the module's key (`"mms": "LD/LN$FC$DO$DA"`).
* `safety.py` — `Policy` (mode + safety profile + `--yes`) and the `Interaction` protocol through which the
  core asks the operator. `NonInteractive` never blocks (IDN-5); `Scripted` drives tests.

## Verification (`ied_client.verify`)

`run_checks(session, reference, options)` → `CheckReport`: self-consistency checks always, then
reference checks (SCD/CID via `ied_client.scl`, snapshot via `snapshot.py`/`diff.py`, or a device-supplied
SCL file), then communication checks (client relationships from the SCD/inventory; `--as` a client).
Configuration and Communication are summarised separately; a device passes only if both pass (VER-7).
The expected model from SCL is in ACSI terms (`ObjectRef`, `DatasetRef`); native names for evidence come
from `session.protocol.names`.

## Diagnosis (`ied_client.diagnosis`, `mms_protocol.diagnosis`)

`diagnose` (DIA-1, DIA-2) runs the layers in order and stops at the first failing one with a verdict and
next steps: bind (local address, NBR-2), then the protocol's own layers (`ConnectionDiagnosis.run`), then
identity, model, reports and controls, which are ACSI and live in the client. For MMS the protocol layers
are raw probes below libiec61850 (TCP 102, COTP, ISO session / presentation / ACSE / MMS initiate, TLS
3782) and the classification of association failures (DIA-3, DIA-4, DIA-6); `after_identity` adds the quirk
re-assessment and the edition compatibility check (IDN-8). `discover` (DIA-5) is generic: TCP sweep,
`protocol.open` + identify, and `protocol.classify_failed_association` for hosts that refuse.

## MMS protocol module (`mms_protocol`)

* `module.py` — `MmsProtocol`, the object the entry point exposes (`mms_protocol:protocol`).
* `association.py` — `MmsAssociation`, the `Association` over `adapter.IedClient` (one GetNameList /
  GetVariableAccessAttributes per LN, readMultiple grouped per LD).
* `names.py` — `MmsNaming`: `LD/LN$FC$DO$DA`, `LD/LN$dataset`, `LD/LN$RP$name`.
* `codes.py` — the `ied`, `mms`, `data-access` and `acse-diag` domains and the full association outcomes.
* `libiec_config.py` — SCL → libiec61850 model config (used by the simulator in `tests/sim`).
* `data/hints/*.yaml`, `data/quirks.yaml` — the module's part of the explanation catalogue and quirks.

### Adapter (`mms_protocol.adapter`)

* `_native.py` — loads the wheel's libiec61850 through the SWIG extension's handle and declares ctypes
  prototypes for the ~180 functions we use. Why ctypes: see `docs/spikes.md` RSK-1 (GIL deadlock in the
  SWIG layer).
* `client.py` — `IedClient` (one association) and `ControlObject`. All requests are synchronous ctypes calls
  (GIL released). Callbacks (reports, CommandTermination, connection closed) run on libiec61850's
  connection thread and **only copy data** into Python objects; they never issue requests (a request from
  the connection thread would wait for itself).
* `codec.py` — `MmsValue` ⇄ Python and `MmsVariableSpecification` → `VarSpec`. Writes are always encoded
  from the device's own type (RW-3) via `ied_client.protocol.values.coerce`.
* `types.py` — MMS-only details (`ConnectionParams`, `VariableListEntry`, type numbers, service bits).

Reads and writes use MMS-level services (`MmsConnection_readVariable/writeVariable`) so that the device's
DataAccessError is reported exactly (CLI-7, RW-6). RCBs, controls and files use the IEC 61850 client
services of libiec61850.

## Adding a protocol module

1. Implement `ProtocolModule`, `Association`, `Naming` and `ConnectionDiagnosis` from
   `ied_client.protocol.api`; return the shared types from `ied_client.protocol.types` and raise the errors
   from `ied_client.protocol.errors`.
2. Register the module's error domains with `ied_client.codes.register_domain` at import time, and ship
   hints for every code in them (the catalogue coverage tests check this for registered modules).
3. Expose the module object under the `ied_client.protocols` entry-point group in its `pyproject.toml`.
4. Select it with `--protocol NAME` or `protocol: NAME` on an inventory device.

`tests/unit/test_protocol_plugin.py` is a minimal working example.

## Threading model

* One Python thread drives a session (CLI or test). libiec61850 runs one receive thread per association.
* All blocking native calls release the GIL (ctypes), so callbacks can always acquire it.
* Report callbacks put decoded `Report` objects on a `queue.Queue`; the subscription loop consumes them.
* CommandTermination arrivals are recorded under a condition variable; `wait_termination` decides polarity.
* The simulated IED runs in a separate process in tests (`tests/sim/fixture.py`) so that server callbacks
  never compete with the client for one interpreter's GIL.

## Test infrastructure

`tests/sim/` is a simulated IED: a real libiec61850 server (ctypes) built from a model config or an SCL
file (via `mms_protocol.libiec_config.to_libiec61850_config`), with scriptable behaviour —
switching-hierarchy authority checks with AddCause, interlocking, failing execution (CommandTermination-),
execution delays, write refusal / silent non-application, setting-group storage, file service, edition,
Owner, association limit. `uv run python -m tests.sim --scl FILE --port N` runs it standalone for demos
and training.
