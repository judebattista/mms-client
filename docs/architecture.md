# Architecture

```
  CLI (shell + one-shot)          mms_client.cli          argparse registry, prompt_toolkit, rich
        │
  Check engine                    mms_client.verify       checks → CheckResult (status, category, evidence, raw)
        │                         mms_client.diagnosis    layered diagnosis, raw probes, discover
  Core client                     mms_client.core         session, model, read/write, controls, reports,
        │                                                 setting groups, files, identity, log, restore
        │                         mms_client.scl          SCL parser (Ed1/Ed2/Ed2.1), expected model
        │                         mms_client.explain      hint catalogue (YAML data)
  Adapter                         mms_client.adapter      the only code touching pyiec61850-ng (PLT-4)
        │
  libiec61850 1.6.1 as bundled in the pyiec61850-ng wheel
```

`tests/unit/test_architecture.py` enforces PLT-4 (only the adapter imports pyiec61850) and ARC-1 (the CLI
does not reach into adapter internals).

## Adapter (`mms_client.adapter`)

* `_native.py` — loads the wheel's libiec61850 through the SWIG extension's handle and declares ctypes
  prototypes for the ~180 functions we use. Why ctypes: see `docs/spikes.md` RSK-1 (GIL deadlock in the
  SWIG layer).
* `client.py` — `IedClient` (one association) and `ControlObject`. All requests are synchronous ctypes calls
  (GIL released). Callbacks (reports, CommandTermination, connection closed) run on libiec61850's
  connection thread and **only copy data** into Python objects; they never issue requests (a request from
  the connection thread would wait for itself).
* `codec.py` — `MmsValue` ⇄ Python and `MmsVariableSpecification` → `VarSpec`. Writes are always encoded
  from the device's own type (RW-3); `parse_text` turns operator input into that type or refuses.
* `types.py` — plain dataclasses returned to callers (`VarSpec`, `BitString`, `UtcTime`, `RcbValues`,
  `Report`, `ControlStepResult`, …). Nothing native crosses the adapter boundary.
* Errors carry an `ErrorInfo` (`mms_client.codes`): one spelling per code everywhere, and the key used by
  the hint catalogue (`data-access:object-access-denied`, `add-cause:blocked-by-interlocking`, …).

Reads and writes use MMS-level services (`MmsConnection_readVariable/writeVariable`) so that the device's
DataAccessError is reported exactly (CLI-7, RW-6). RCBs, controls and files use the IEC 61850 client
services of libiec61850.

## Core (`mms_client.core`)

* `Session` — one persistent association (CLI-1), the operator's settings (mode, orCat, terse), the
  cached model (MDL-4), the **cleanup registry** (RPT-7), the JSONL log (LOG-1), `last_error` for
  `explain last`. Every change the tool makes to an RCB or an edit session is registered *before* it is
  made, and undone in reverse order on exit, Ctrl-C (SIGTERM is mapped to KeyboardInterrupt) or
  disconnect; what cannot be undone (connection lost) is reported with its lingering effect.
* `model.py` — browsing: GetNameList per LD, one GetVariableAccessAttributes per LN, FC branches merged
  into an IEC view (LD → LN → DO → DA, each node knowing its FCs). Control blocks (RP/BR/LG/GO/MS/US and
  the SGCB) are kept separately.
* `readwrite.py`, `controls.py`, `reports.py`, `setgroup.py`, `files.py`, `restore.py`, `identity.py`,
  `incident.py` — one module per spec area. Each returns structured results that the CLI renders as text or
  JSON (ARC-3, `results.envelope`).
* `safety.py` — `Policy` (mode + safety profile + `--yes`) and the `Interaction` protocol through which the
  core asks the operator. `NonInteractive` never blocks (IDN-5); `Scripted` drives tests.

## Verification (`mms_client.verify`)

`run_checks(session, reference, options)` → `CheckReport`: self-consistency checks always, then
reference checks (SCD/CID via `mms_client.scl`, snapshot via `snapshot.py`/`diff.py`, or a device-supplied
SCL file), then communication checks (client relationships from the SCD/inventory; `--as` a client).
Configuration and Communication are summarised separately; a device passes only if both pass (VER-7).

## Diagnosis (`mms_client.diagnosis`)

Raw probes below libiec61850 (TCP, COTP, ISO session / presentation / ACSE / MMS initiate, TLS port),
classification of association failures (DIA-3, DIA-4, DIA-6), local-address checks (NBR-2), `discover`
(DIA-5), and `diagnose` which runs the layers in order and stops at the first failing one (DIA-1) with a
verdict and next steps (DIA-2).

## Threading model

* One Python thread drives a session (CLI or test). libiec61850 runs one receive thread per association.
* All blocking native calls release the GIL (ctypes), so callbacks can always acquire it.
* Report callbacks put decoded `Report` objects on a `queue.Queue`; the subscription loop consumes them.
* CommandTermination arrivals are recorded under a condition variable; `wait_termination` decides polarity.
* The simulated IED runs in a separate process in tests (`tests/sim/fixture.py`) so that server callbacks
  never compete with the client for one interpreter's GIL.

## Test infrastructure

`tests/sim/` is a simulated IED: a real libiec61850 server (ctypes) built from a model config or an SCL
file (via `mms_client.scl.to_libiec61850_config`), with scriptable behaviour — switching-hierarchy
authority checks with AddCause, interlocking, failing execution (CommandTermination-), execution delays,
write refusal / silent non-application, setting-group storage, file service, edition, Owner, association
limit. `uv run python -m tests.sim --scl FILE --port N` runs it standalone for demos and training.
