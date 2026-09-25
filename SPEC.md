# mms-client — Specification index

**Status:** Draft 0.4 (2026-09-25) — v1 implementation in progress; see decision records.

As of this draft, the specification is split into two documents so that the protocol-independent
parts of the tool can be described once and reused if a second protocol is ever added:

- **[IED-CLIENT-SPEC.md](IED-CLIENT-SPEC.md)** — the general IED client: purpose, scope, CLI,
  users/modes/safety, the data model (browsing, read/write, controls, reports), verification,
  the explanation system, session logging, the rack inventory, and the **protocol module
  contract** (§4.1) that any protocol spec must implement.
- **[MMS-PROTOCOL-SPEC.md](MMS-PROTOCOL-SPEC.md)** — the one protocol module that currently
  exists: MMS (IEC 61850-8-1) via `pyiec61850-ng`/libiec61850. Platform/dependencies, the
  adapter's ctypes architecture, association/diagnosis internals, MMS-specific identity and
  edition limits, and the MMS-specific technical risks and spikes.

Requirement IDs (e.g. `CTL-4`, `PLT-3`) are unique across both documents and are grep-able from
either — code, tests and reviews cite them without needing to know which document a given ID
ended up in.

This file previously held the full combined specification (draft 0.3, 2026-09-24). Its content
was split, not rewritten: every requirement ID from that draft still exists, in one of the two
documents above, and the two decision records together are its decision record.
