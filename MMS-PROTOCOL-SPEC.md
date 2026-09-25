# MMS protocol module — specification

**Status:** Draft 0.1 (2026-09-25) — split out of `SPEC.md` 0.3.

This document specifies the **MMS protocol module**: the implementation of the protocol module
contract defined in [IED-CLIENT-SPEC.md](IED-CLIENT-SPEC.md) §4.1, for IEC 61850's MMS mapping
(IEC 61850-8-1), built on [pyiec61850-ng](https://pypi.org/project/pyiec61850-ng/) (Python
bindings for libiec61850). It does not repeat anything protocol-independent — CLI shape, safety
model, control model, verification engine, explanation-system framework — which lives in the
client spec. Read that document first; this one only makes sense against it.

The module is the `mms_protocol` package (`src/mms_protocol`), registered under the
`ied_client.protocols` entry point as `mms`. Paths below are relative to it unless they start
with `ied_client/`.

Requirement IDs here are either MMS-specific IDs in their own right (`PLT-3`, `PLT-4`, `RSK-*`,
…) or the MMS instantiation of a client-spec contract point (`PROTO-9`'s concrete layers, for
example) — cross-references are given either way.

## 1. Platform and dependencies

| ID | Requirement |
|---|---|
| PLT-3 | Must use pyiec61850-ng as the only protocol stack. Lower-level probes (TCP, COTP) for diagnosis (§4) may use raw sockets. |
| PLT-4 | All access to pyiec61850-ng must go through this module's adapter layer (the IED-CLIENT-SPEC.md ARC-4 instantiation for MMS). No other module may import it directly. |

**Licence note:** libiec61850 is dual-licensed GPLv3 / commercial. Internal use is fine;
distribution outside the organisation needs review.

## 2. Architecture: why the adapter uses ctypes instead of the SWIG bindings

```
  Core client (protocol-independent, see IED-CLIENT-SPEC.md §14)
        │
  MMS adapter          the only code touching pyiec61850-ng; owns MmsValue lifetimes and error mapping
        │
  pyiec61850-ng / libiec61850
```

- The pyiec61850-ng SWIG module isn't built with `-threads`. Only four wrapped functions
  (`pyWrap_IedConnection_readObject/writeObject/getRCBValues/setRCBValues`) release the GIL.
  Everything else — `operate`/`select`/`cancel`, directory services, dataset reads, file
  services, `MmsConnection_identify` — holds the GIL for the duration of the call.
- Reports and CommandTermination arrive as C→Python callbacks on libiec61850's own receive
  thread, which needs the GIL to run them. If the main thread is blocked in one of those
  GIL-holding SWIG calls while a callback needs to fire, this deadlocks permanently (reproduced:
  500 concurrent `operate` calls against ~50 reports/s hung until killed).
- **Workaround:** the adapter resolves libiec61850 symbols through the SWIG extension's own
  loaded handle (guaranteed to be exactly the wheel's build) and declares ctypes prototypes
  directly, bypassing SWIG's Python wrapper layer entirely. Every ctypes foreign call releases
  the GIL automatically. Callbacks are `ctypes.CFUNCTYPE`s that only copy data into Python
  objects and never issue new requests (a request issued from the connection thread would block
  waiting for a response that thread is supposed to deliver).
- Verified: 3000 reads and 3000 direct-enhanced operates concurrent with reports and
  CommandTerminations, no deadlock.

Practical consequences for anyone extending `adapter/client.py`:

1. **Never call back into a blocking request from inside a callback.** Callbacks put decoded
   objects on a `queue.Queue` (reports) or set a condition variable (CommandTermination); the
   consuming loop runs on the normal calling thread.
2. **CommandTermination polarity is decided after arrival**, not from the callback's own
   signal — libiec61850 can invoke CommandTermination *before* storing the LastApplError that
   goes with a negative termination. The adapter compares LastApplError against its
   pre-operate value with a 150 ms grace period.
3. Other SWIG bugs found and avoided entirely by going through ctypes: `EventSubscriber`
   double-free on GC (segfault, reproduced), NULL-check typemaps rejecting NULLs the C API
   accepts, and wrong `RCB_ELEMENT_*` bitmasks in `pyiec61850.mms.reporting` (0x04 is `Resv`,
   not `RptEna` — the high-level wrapper silently writes the wrong attribute). Don't use that
   high-level wrapper for anything.
4. Every native handle (`MmsValue`, `LinkedList`, `MmsVariableSpecification`,
   `ClientReportControlBlock`, `FileDirectoryEntry`, `MmsServerIdentity`,
   `ControlObjectClient`) must be owned and explicitly freed by the adapter before plain Python
   data crosses back out (the `ied_client/protocol/types.py` dataclasses only — nothing native
   crosses the boundary). If you add a new native call, free what you allocate in the same function,
   including on the exception path.
5. Reading a whole logical node without an FC fails with `mms:parsing-response` on this stack —
   always read per DO and per FC (§10).
6. A dataset member that lives in another LD, on an LD that has an `ldName`, crashes the
   libiec61850 1.6.1 **server** on GetDataSetDirectory. This is a server-side bug you'll hit if
   you write SCL for the simulator; the shipped fixtures avoid it deliberately.
7. `authority-probe` cannot test orCat on `sbo-with-normal-security` objects — libiec61850's
   SBO-normal select carries no origin at the protocol level. This is the concrete instance of
   PROTO-6's "declare what it can't support"; the tool says so rather than silently skipping it.

## 3. Association: `--as` / `--bind`, and PROTO-1's connection parameters

`--as <client>` (NBR-1) makes the module assume the *identity* of a client from the inventory:
its `source_ip` and its configured RCBs.

`--bind <ip>` (NBR-2) is the lower-level mechanism `--as` uses under the hood
(`IedConnection_setLocalAddress`, confirmed working — a bound association shows up at the server
with that peer address, and RCB `Owner` reflects the bound IP in the `…7f000002`-style byte
encoding). Use `--bind` directly for a specific source IP without pulling in a whole named
client's RCB list.

Before binding, the tool checks the address is actually configured on a local interface and, if
not, prints the exact `ip addr add` command to run. `--as` and `--bind` are session-scoped
options — they must be given at `connect` time (or as a global option on a one-shot command); the
bound IP of an already-open association can't be changed.

The TCP port is the device's `port` (INV-1; this module's default is 102). The device's `mms:`
inventory block (INV-6) is reserved for per-device MMS connection parameters; v1 defines none.

## 4. Diagnosis: MMS's instantiation of PROTO-9

The concrete layers below "identity" in IED-CLIENT-SPEC.md DIA-1, for this module:

network (ICMP, TCP 102) → transport/session (COTP, ISO session) → MMS initiate (PDU size,
services) → *(identity, model, reports, controls: protocol-independent, see client spec)*.

| ID | Requirement |
|---|---|
| DIA-3 (MMS) | Where libiec61850 reports only a generic connect failure, the module probes below it: raw TCP, COTP, ISO session/presentation/ACSE, MMS initiate — implemented in `diagnosis/probes.py`. `diagnosis/classify.py` turns the failure into a specific layer classification and documents, in its module docstring, the **observed libiec61850 server behaviour when association slots are exhausted** and which layer each probe sees each failure at. |
| DIA-4 (MMS) | IEC 61850 has no standard way to report association limits or usage over MMS. Refusal is classified as: TCP refused, MMS initiate rejected, accepted then closed, or timeout. |
| DIA-6 (MMS) | Security-requirement detection (PROTO-10): TCP 102 closed but TCP 3782 (the IEC 62351 TLS port) open, or an association rejected specifically for authentication. |

`discover <subnet>` (DIA-5) scans TCP 102, and is sequential and rate-limited by design
(`--delay`, default 0.2 s between hosts) — not meant to be sped up by parallelising, since a
subnet full of IEDs with tight association limits will refuse a concurrent scan. `--allow-large`
is a deliberate friction point above a /22, so a typo'd subnet doesn't turn into scanning a /8.

## 5. Identity and edition: MMS-specific sources and limits

`IDN-1`'s protocol-native source (PROTO-3) for this module is **MMS Identify**
(`MmsConnection_identify`): vendor, model, revision.

The `LLN0.NamPlt.ldNs` → edition mapping (IDN-2, OI-9) is protocol-independent and specified in
the client spec §6.3.

| ID | Requirement |
|---|---|
| IDN-8 | `diagnose` runs an edition-compatibility check when client and server editions differ, starting with **MMS object reference length limits**, which differ by edition. (Exact limits to be taken from the standard when this is implemented, not from memory — see OI-10.) |

## 6. Model browsing and file services over MMS

Directory services: GetServerDirectory, GetLogicalDeviceDirectory, GetVariableAccessAttributes.
Reading a whole logical node without an FC fails on this stack (§2 point 5) — the module always
reads per DO and per FC; this isn't configurable without re-validating against the stack.

File services (FIL-1/FIL-2) use MMS file services (GetFile-family). No upload/delete (FIL-3, at
the client-spec level; MMS supports SetFile but this module never calls it in v1).

## 7. Read and write over MMS

Reads and writes use MMS-level services (`MmsConnection_readVariable`/`writeVariable`) so that
the device's `DataAccessError` is reported exactly (CLI-7, RW-6). Writes are always encoded from
the device's own type (RW-3); `parse_text` turns operator input into that type or refuses.

## 8. Controls over MMS

Control services use libiec61850's IEC 61850 client services (not raw MMS Read/Write) for
Select/Operate/Cancel. CommandTermination race handling and the `sbo-with-normal-security`
orCat limitation are covered in §2.

## 9. Reports over MMS

RCBs, like controls, use libiec61850's IEC 61850 client services rather than raw MMS Read/Write,
except where the adapter's ctypes layer bypasses the high-level wrapper (§2, `RCB_ELEMENT_*`
bitmask bug).

## 10. Explanation catalogue: MMS instantiation of EXP-7

| ID | Requirement |
|---|---|
| EXP-7 (MMS) | **Minimum v1 coverage (acceptance criterion):** all libiec61850 `IED_ERROR_*` codes, all MMS DataAccessError values, all control AddCause values, and association/initiate rejection reasons. |

Catalogue and quirks files (EXP-5, PROTO-12, IDN-9):

**Hints** (`data/hints/*.yaml`: `codes.yaml` for the `ied`, `mms`, `data-access` and `acse-diag`
domains, `association.yaml`; the client's own `ied_client/data/hints*` cover AddCause, CtlError,
tool and check codes): keyed by `"<domain>:<name>"` matching `ErrorInfo.key` (the MMS domains are
registered in `codes.py`) (e.g.
`data-access:object-access-denied`), or `check:<check id>` for check-result hints. Matching is
context-sensitive (EXP-3): the same error code can have different entries depending on
FC/CDC/service/mode. Every entry states its certainty — `fact`/`likely`/`check` — per EXP-4.

**Quirks** (`data/quirks.yaml`, IDN-9): starts empty by design (no vendor IEDs
were available for Phase 0 — RSK-6 is explicitly "pending real devices"). Matching is by
`match.vendor`/`match.model`/`match.firmware` against the identity MMS Identify reports, each
field an exact string or glob, case/whitespace-insensitive; when several entries match,
most-specific wins (exact beats glob, a firmware condition present counts as more specific), and
at equal specificity the most-recently-loaded entry wins. Fields documented in the file's own
header (`max_associations`, `slot_release_after_abrupt_disconnect_s`, `refusal_behaviour`,
`resv_tms_behaviour`, etc.); unknown fields are kept and shown as-is. Always cite `source` (spike
or incident file) and `added` (date).

Procedure for a new IED model (from RSK-6): `diagnose`, `authority-probe` in expert mode across
all orCats, `rcb` + `subscribe` (note `ResvTms`/`Owner` behaviour specifically), `setgroup
show`/`edit`, `info` (identity sources and `ldNs` value) — then write the quirks entry from what
you observed.

## 11. Known stack limitations

These are established findings (`docs/spikes.md`), not open questions:

- Reading a whole LN without specifying FC fails (`mms:parsing-response`) on this
  libiec61850/pyiec61850-ng combination.
- libiec61850 1.6.1's **server** implementation crashes on `GetDataSetDirectory` for a dataset
  member that lives in another LD when that LD has an `ldName` set.
- `authority-probe` cannot report orCat results for `sbo-with-normal-security` objects.
- Everything below was validated **against the simulated IED only** — no real vendor IED was
  available during Phase 0. Anything tagged RSK-6 needs re-validation per real model, which is
  exactly what the quirks file (§10) is for.

## 12. Technical risks and spikes

| ID | Risk | Spike (must be done before building on the feature) |
|---|---|---|
| RSK-1 | **Callbacks through SWIG.** CommandTermination (enhanced controls), report handlers and file download are C→Python callbacks. If pyiec61850-ng doesn't support them reliably, enhanced controls, RPT-2 and FIL-2 need a workaround. | Against a real IED: install a report handler and receive reports; run an SBO-enhanced operate and receive CommandTermination; download a file. Also test under Ctrl-C and connection loss. |
| RSK-2 | **Local address binding.** NBR-2 depends on `IedConnection_setLocalAddress` (or equivalent) being exposed. | Confirm the binding exists; associate from a secondary IP. |
| RSK-3 | **Error granularity.** DIA-3 assumes that failure layers can be separated. | Cause failures at each layer; see what libiec61850 reports and what our own probes add. |
| RSK-4 | **Wheel availability** for the chosen Python on Ubuntu LTS and current Kali. | Install test on both. |
| RSK-5 | **Memory management.** MmsValue and linked-list ownership across SWIG. | Soak test: 10k reads/writes, check for leaks. |
| RSK-6 | **Vendor variation** in authority checks, RCB reservation, ResvTms, setting groups and identity/`ldNs` reporting. Models in any given experiment are not known in advance. | Run the spikes on every IED model available now; record results in the quirks file. Re-run for new models as they appear. |

## 13. Open issues

| # | Topic | Notes |
|---|---|---|
| OI-5 | Python version | Pinned to CPython 3.12 (see decision record). |
| OI-10 | Object reference length limits (IDN-8) | Limits must be taken from the standard, not from memory; the edition-compatibility check is not yet implemented pending those values. |

`assoc-probe` (active measurement of max associations, refusal behaviour, slot-release timing) is
explicitly deferred to v2 — don't expect it, and don't confuse it with the passive DIA-4
classification v1 does have.

## 14. Decision record

| Date | Decision |
|---|---|
| 2026-09-24 | RSK-1 outcome: the adapter calls the libiec61850 library bundled in the pinned pyiec61850-ng wheel through ctypes instead of the SWIG wrappers, because the SWIG module holds the GIL during blocking calls and deadlocks when a report or CommandTermination arrives (reproduced). PLT-3/PLT-4 still hold: pyiec61850-ng's libiec61850 is the only stack and only this module's adapter touches it. See `docs/spikes.md`. |
| 2026-09-24 | OI-5: Python pinned to CPython 3.12 via uv (`.python-version`); pyiec61850-ng pinned to 1.6.1.10. |
| 2026-09-24 | A simulated IED (real libiec61850 server, scriptable behaviour, built from SCL) is part of the test suite and can be run standalone for training (`python -m tests.sim`). |
| 2026-09-25 | This document split out of the original combined `SPEC.md` as the MMS instantiation of IED-CLIENT-SPEC.md's protocol module contract (§4.1 there). No behavioural change; IDs kept stable so existing citations (code, tests, `docs/architecture.md`, `docs/spikes.md`) remain valid. |
| 2026-09-25 | Code split to match: the module lives in `src/mms_protocol` and plugs into the client through the contract in `ied_client.protocol`. It registers the `ied`, `mms`, `data-access` and `acse-diag` error domains, ships its own hints and quirks, and implements PROTO-9 as `diagnosis/connection.py`. OI-9 moved to the client spec. |
