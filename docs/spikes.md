# Phase 0 — spike results (RSK-1 … RSK-6)

Date: 2026-09-24. Stack: pyiec61850-ng 1.6.1.10 (bundles libiec61850 1.6.1), CPython 3.12.14 (uv-managed),
Kali Linux 2026.3 (WSL2 kernel 6.18). No real IED was available for these spikes: they ran against a real
libiec61850 **server** (the simulated IED in `tests/sim/`, driven from our own SCL files), which exercises
the same client code paths. Everything marked *to repeat on real IEDs* must be re-run per model (RSK-6).

## RSK-1 — Callbacks through SWIG — **confirmed problem, worked around**

**Finding.** The pyiec61850-ng SWIG module is not built with `-threads`. Only four wrappers
(`pyWrap_IedConnection_readObject/writeObject/getRCBValues/setRCBValues`) release the GIL. Every other
blocking call — `ControlObjectClient_operate/select/cancel`, directory services, dataset reads, file
services, `MmsConnection_identify` — holds the GIL while it waits for the device. When a report or a
CommandTermination arrives during such a call, libiec61850's receive thread needs the GIL to run the
Python callback, while the calling thread waits for a response that only the receive thread can
deliver: a **permanent deadlock**.

**Evidence.** Stress test with a URCB reporting at ~50 reports/s: 500 plain `IedConnection_readObject`
calls and 500 `ControlObjectClient_operate` calls both hung until killed (60 s); 500 `pyWrap_` reads
completed in 0.06 s. (Spike scripts: `stress.py` / `cstress.py`, summarised in `tests/integration/test_adapter.py::test_reports_under_load_with_controls`.)

**Other SWIG issues found.**
* `EventSubscriber` deletes its handler in its destructor; a Python-owned director handler whose
  subscriber is garbage-collected is freed twice → segfault (reproduced).
* NULL-check typemaps reject NULL where the C API allows it (`IedConnection_getRCBValues(…, NULL)`,
  `IedServer_createWithConfig(model, NULL, cfg)`).
* The high-level `pyiec61850.mms.reporting` wrapper uses wrong `RCB_ELEMENT_*` masks
  (e.g. 0x04 = Resv, not RptEna), so "enable reporting" silently writes the wrong attributes.

**Workaround (implemented).** The adapter calls the libiec61850 library that the pyiec61850-ng wheel
bundles through **ctypes**, resolving symbols through the SWIG extension's handle (so it is exactly the
wheel's build). ctypes releases the GIL for every foreign call; callbacks are ctypes `CFUNCTYPE`s that
only copy data into Python objects. Result: 3000 reads and 3000 direct-enhanced operates with concurrent
reports and CommandTerminations — no deadlock, no failure. This is a deviation from "use the SWIG
bindings", not from PLT-3: the protocol stack is still pyiec61850-ng's libiec61850. Decision recorded in
SPEC.md §20.

**CommandTermination detail.** libiec61850 can invoke the CommandTermination callback *before* it
stores the LastApplError that accompanies a CommandTermination-. The adapter therefore decides the
polarity after arrival, by comparing LastApplError with its value before the operate (with a 150 ms grace
period). Verified with a simulator control that fails execution (AddCause blocked-by-process).

**File download.** Done through `IedConnection_getFile` with a ctypes callback (no SWIG involvement);
150 kB transfers verified.

*To repeat on real IEDs:* report handler + SBO-enhanced operate + file download, also under Ctrl-C and
cable pull.

## RSK-2 — Local address binding — **works**

`IedConnection_setLocalAddress` is exported and works: an association bound to 127.0.0.2 shows up at the
server with peer address 127.0.0.2, and a URCB reserved from that association carries Owner
`…7f000002` (`test_rcb_owner_reflects_bound_local_ip`). The tool checks that the address exists on an
interface before binding and prints the exact `ip addr add` command if not (NBR-2).

*To repeat on real IEDs:* bind to a secondary IP on the rack interface of an IED that filters by IP.

## RSK-3 — Error granularity — **libiec61850 alone is not enough; own probes added**

libiec61850 reports connect failures as a handful of generic `IedClientError`s (`connection-rejected`,
`timeout`, …). The diagnosis package adds its own raw probes (TCP, COTP CR/CC, ISO session CONNECT with
presentation, ACSE AARQ and MMS initiate, parsed layer by layer) to separate transport, session, ACSE and
MMS failures, and to detect IEC 62351 TLS (port 3782) and ACSE-authentication rejections (DIA-3, DIA-6).
Observed libiec61850 server behaviour when its association slots are exhausted and the layers at which
our probes see each failure are documented in `mms_client/diagnosis/classify.py` (module docstring).

*To repeat on real IEDs:* cause failures at each layer on each model (wrong IP, port closed, slots full,
wrong AP title, security required) and record what they return.

## RSK-4 — Wheel availability — **available; Python pinned to 3.12**

pyiec61850-ng 1.6.1.10 ships manylinux_2_28 x86_64/aarch64 wheels for CPython 3.9–3.14. Kali's system
Python is 3.14.7; Ubuntu 24.04 LTS ships 3.12. The project pins **CPython 3.12** (`.python-version`,
`requires-python >=3.12,<3.13`) and uv installs a managed interpreter, so the system Python is never used
(PLT-2). Installed and tested on Kali 2026.3.

*To repeat:* `uv sync && uv run pytest` on Ubuntu 22.04/24.04 LTS (glibc ≥ 2.28 required by the wheel).

## RSK-5 — Memory management — **no leak found**

The adapter owns every native object (MmsValue, LinkedList, MmsVariableSpecification,
ClientReportControlBlock, FileDirectoryEntry, MmsServerIdentity, ControlObjectClient) and frees it before
returning plain Python data. Soak test (`tests/integration/test_soak.py`, `-m soak`): 10 000 read+write
cycles with periodic GetNameList, RCB reads, file directory, enhanced operates and a BRCB reporting
every 200 ms: **RSS growth 284 kB**, ~1100 operations/s over loopback.

## RSK-6 — Vendor variation — **pending real devices**

No vendor IEDs were available. The quirks file (`src/mms_client/data/quirks.yaml`, IDN-9) is in place and
empty, as specified. Procedure per IED model: run `diagnose`, `authority-probe` (in expert mode for all
orCats), `rcb` + `subscribe` (note ResvTms and Owner behaviour), `setgroup show/edit`, `info` (identity
sources and ldNs), then add an entry to the quirks file.

## Other findings worth knowing

* Reading a whole logical node in one MMS read (`LN` without FC) fails with `mms:parsing-response` on
  this stack; the tool always reads per DO and FC.
* libiec61850 1.6.1 server: a dataset member in another LD, where an LD has an `ldName`, crashes the
  server on GetDataSetDirectory (found by the SCL exporter tests; the simulator avoids it).
* libiec61850's SBO-normal select carries no origin, so `authority-probe` cannot test orCat on
  sbo-with-normal-security objects; it says so.
