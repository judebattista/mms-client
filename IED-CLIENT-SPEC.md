# IED client — general specification

**Status:** Draft 0.1 (2026-09-25) — split out of `SPEC.md` 0.3.

This is the protocol-independent half of the tool's specification. It describes an **IED
client**: a test-rack tool that associates with an IEC 61850 device, browses its data model,
reads/writes/operates it, and verifies and diagnoses it — without saying *how* those things go
over the wire. The wire protocol is supplied by a separate **protocol spec** that implements the
contract in §4.1. Today there is one such document, [MMS-PROTOCOL-SPEC.md](MMS-PROTOCOL-SPEC.md),
covering MMS via `pyiec61850-ng`/libiec61850. This document is written so that a second protocol
spec (a different IEC 61850 SCSM, or a non-MMS protocol entirely) could be added later and plug
into the same CLI, safety model, verification engine and explanation system.

Requirements carry IDs (e.g. `CTL-4`) so that code, tests and reviews can refer back to them.
Keywords **must**, **should** and **may** are used in their usual sense. IDs are unique across
this document and the protocol spec(s); grep for an ID across the whole repo, not just one file.

**How to read this pair of documents:** if a requirement would still make sense verbatim for a
client that spoke a different protocol to the IED, it lives here. If it depends on the wire
protocol, the association/session stack, a specific client library, or that library's quirks, it
lives in the protocol spec. The data model (logical devices/nodes/data objects/attributes, FCs,
CDCs, control model, RCBs, SCL) is treated as protocol-independent throughout — it comes from
IEC 61850-7-x, not from the SCSM — so it lives here even though the current build only ever talks
to it over MMS.

---

## 1. Purpose

An IEC 61850 IED client for the test rack that lets an operator:

1. Verify that an IED's server is **configured** correctly.
2. Verify that it **communicates** correctly with the clients it must serve.
3. Read and write data attributes, and operate controls.
4. Troubleshoot failing communication between devices in the rack.

The rack is reconfigured for each experiment. It usually contains a Station Manager acting as
SCADA gateway, one or more Bay Control Units (BCUs) and assorted other IEDs. It is isolated from
any power grid, but the IEDs are real devices with real outputs.

## 2. Scope

### 2.1 In scope (v1)
- Interactive CLI shell and one-shot CLI commands (§5).
- Association, identity and layered connectivity diagnosis (§6).
- Model browsing and read-only file services (§7).
- Read / write of data attributes, including setting groups (§8).
- Control services for SPC, DPC and INC (§9).
- Report control block inspection, subscription and takeover (§10).
- Verification against SCD, CID, snapshot, or self-consistency only (§11).
- Explanation system for non-expert users (§12).
- Session logging and restore (§13).
- Exactly one protocol module: MMS (see [MMS-PROTOCOL-SPEC.md](MMS-PROTOCOL-SPEC.md)).

### 2.2 Deferred (later phases)
- Automated testing: pytest plugin, fixtures, JUnit output (§17).
- Controls for APC, BSC, ISC (analogue setpoints, tap changers).
- File upload and delete.
- Read-only inspection of GOOSE and SV control blocks (GoCB, SVCB) — deferred regardless of
  transport; see the protocol spec for whether a given protocol module can even see them.
- IEC 61850 logs: LCB inspection commands and log queries. Revisit after the prototype. Note:
  logs record data values, not connection events.
- Active probing of association/session limits and refusal behaviour (protocol-specific:
  `assoc-probe`, see MMS-PROTOCOL-SPEC.md). Planned for v2.
- GUI or TUI front end.
- A second protocol module. The contract in §4.1 is written with this in mind, but nothing beyond
  MMS is planned or built.

### 2.3 Out of scope
- **Passive capture of traffic between other devices.** The tool is an active client. Diagnosing
  the link between two other devices directly requires packet capture (e.g. Wireshark), which is
  not part of this tool. What a given protocol module can and cannot observe at all (e.g. an MMS
  module cannot see GOOSE/SV) is documented in that protocol's spec, not here.
- **Access control.** Operating modes (§4) are guardrails against mistakes, not security.
- **Communication security (v1).** No transport/association authentication or encryption in v1.
  Detection of an IED that appears to require it is in scope (§6.2); implementing it is not.

## 3. Platform

| ID | Requirement |
|---|---|
| PLT-1 | Must run on Ubuntu (LTS) and Kali Linux. Windows and macOS are not supported. |
| PLT-2 | Must run in a virtual environment with a pinned Python version (uv or pyenv), not the system Python. Kali's rolling Python may be ahead of what a protocol module's dependencies ship wheels for. |

Protocol-library dependencies and licensing are specified per protocol module (see
MMS-PROTOCOL-SPEC.md §2 for the current one).

## 4. Users and operating modes

The tool will be used by non-expert users over time, so the explanation system (§12) is a core
feature.

| ID | Requirement |
|---|---|
| MOD-1 | Two modes: **standard** (default) and **expert** (`--expert` flag, or `set mode expert` in the shell). The current mode must be visible in the shell prompt. |
| MOD-2 | Standard mode allows: browsing, reads, writes to normally-writable FCs, controls with interlock and synchrocheck checks forced on, orCat 1–3, and report subscription to free RCBs only. |
| MOD-3 | Expert mode additionally allows: RCB takeover, disabling interlock/synchrocheck, orCat 0 and 4–8, and writes to FCs that aren't normally writable (for negative testing). |
| MOD-4 | Documentation must state that modes are a guardrail, not access control. |

### 4.1 Protocol module contract

A protocol module is the thing MMS-PROTOCOL-SPEC.md (or any future protocol spec) specifies.
Everything above the adapter line in §14 is protocol-independent and must not assume which module
is loaded; everything a module must provide to make that possible is listed here.

In code the contract is `ied_client.protocol` (`api.py`: `ProtocolModule`, `Association`,
`Naming`, `ConnectionDiagnosis`; `types.py`: the shared value and result types; `errors.py`;
`registry.py`). Modules are installed Python packages that expose a `ProtocolModule` object under
the `ied_client.protocols` entry-point group; the client selects one per device (INV-6) or with
`--protocol`. The core works in ACSI terms (object and dataset references, FCs); a module's
native names are opaque strings supplied by its `Naming` and shown alongside the IEC reference.
`docs/architecture.md` maps each PROTO point to the code and describes how to add a module.

| ID | A protocol module must provide... |
|---|---|
| PROTO-1 | **Connect/associate**, given a host, port (module-defined default) and any module-specific connection parameters carried in the inventory (INV-6) or CLI flags (e.g. a bound source address, NBR-2); return association-level parameters (e.g. negotiated message size) for `info`. |
| PROTO-2 | **Disconnect/release** cleanly, unwinding anything the module itself enabled server-side (mirrors RPT-7 at the module level). |
| PROTO-3 | **Identify**, if the protocol has a native identify mechanism, returning (vendor, model, revision) marked `confirmed` (IDN-3). A module without one reports that source as absent, not as unknown-with-a-guess. |
| PROTO-4 | **Browse** the model into the generic LD → LN → DO → DA tree with FC and CDC/type per node, and resolve dataset members. |
| PROTO-5 | **Read / write** values as the protocol-independent typed representation the core defines (`ied_client/protocol/types.py`), deriving the type from the device's own model (RW-3) rather than guessing from input text. |
| PROTO-6 | **Controls**: implement Select / SelectWithValue / Operate / Cancel / CommandTermination-wait per `ctlModel` (CTL-2), report AddCause / LastApplError in the standard enumeration, and declare which control patterns it cannot fully support (e.g. cannot surface orCat for some select variant) so `authority-probe` can mark them "not probeable" instead of guessing. |
| PROTO-7 | **Reports**: enable/disable/reserve RCBs, deliver events into the generic Report shape (SqNum, EntryID, trigger reason, values), support takeover, and register its own cleanups with the core's cleanup registry (RPT-7) rather than unwinding silently on its own. |
| PROTO-8 | **Files**: list/download a read-only file directory if the protocol has file transfer at all; otherwise report the capability as absent, not as an error. |
| PROTO-9 | **Layered diagnosis**: expose its own connection process as an ordered list of named layers below "model" (DIA-1) that the diagnosis engine can probe independently and stop at; each layer produces a structured result the generic engine can render. Provide a `classify` function that turns the module's own generic connect failure into a specific layer and reason (DIA-3). |
| PROTO-10 | **Security-requirement detection** (optional): a check for "this device appears to require transport/association security this module doesn't implement," feeding DIA-6. A module without such a signal to check simply doesn't implement this. |
| PROTO-11 | **Version/library metadata** for the `tool` block of the JSON envelope (ARC-3). |
| PROTO-12 | **Association-limit / quirk hooks**: accept identity-keyed hints (limits, refusal-classification patterns) from a quirks-style data file for DIA-4; the file's schema and content are module-specific and live with that module's spec. |
| PROTO-13 | **Error/status catalogue**: supply the full enumeration of error and status codes it can produce, so the explanation catalogue's completeness (EXP-7) can be checked mechanically against it rather than by inspection. |

### 4.2 Safety profile
| ID | Requirement |
|---|---|
| SAF-1 | The inventory may set `safety: lab` or `safety: strict` (default `strict`). |
| SAF-2 | In `strict`, controls and takeovers require **typed confirmation** (the operator types the object name). In `lab`, a `y/N` prompt is enough. |
| SAF-3 | `--yes` may skip write confirmations. It must never skip control or takeover confirmation. |

## 5. CLI

### 5.1 Shell
| ID | Requirement |
|---|---|
| CLI-1 | `ied-client shell <device>` opens a persistent association and an interactive shell. Commands reuse that association. (Repeated connect/release consumes limited resources on the device and can itself cause the faults under investigation.) |
| CLI-2 | The model is navigable like a filesystem: `/` → logical devices → logical nodes → data objects → attributes. `ls`, `cd`, `tree` behave as expected. |
| CLI-3 | Tab completion of object references from the model browsed on connect. |
| CLI-4 | The prompt shows the device, the current path, the mode, and the orCat once one is set, e.g. `relay-F12:/CTRL/CSWI1 [orCat=remote]>`. |
| CLI-5 | Suggested libraries: prompt_toolkit (shell, completion) and rich (tables). Not a hard requirement. |

### 5.2 Commands
| Area | Commands |
|---|---|
| Connection | `connect`, `disconnect`, `info` |
| Diagnosis | `diagnose`, `discover` |
| Model | `ls`, `cd`, `tree`, `describe`, `dataset` |
| Files | `files ls`, `files get` |
| Data | `read`, `write`, `watch`, `setgroup` |
| Controls | `operate`, `select`, `cancel`, `authority-probe` |
| Reports | `rcb`, `subscribe`, `gi` |
| Verification | `check`, `snapshot`, `diff` |
| Help | `explain` |
| Session | `set`, `log`, `restore`, `export` |
| Inventory | `inventory from-scd`, `inventory validate` |

| ID | Requirement |
|---|---|
| CLI-6 | Every shell command must also be available as a one-shot command, e.g. `ied-client read relay-F12 PROT/PTOC1.Str.general --json`. |
| CLI-7 | Output must always show the exact object reference, FC and raw error code alongside any friendly text — "raw error code" is whatever the active protocol module reports (PROTO-13), rendered as-is. |
| CLI-8 | `--terse` (or `set terse on`) suppresses hints for expert users. |

## 6. Connection and diagnosis

### 6.1 Acting as a neighbour
| ID | Requirement |
|---|---|
| NBR-1 | `--as <client>` makes the tool behave like a named client from the inventory: its source address, authentication, and the RCBs it uses. |
| NBR-2 | The tool must be able to bind the association to a specified local address (for IEDs that filter clients by source address). The operator adds that address to the interface (e.g. `ip addr add`). The tool must check that the address is present and give the exact command if it is not. How the binding is actually done is module-specific (PROTO-1). |

### 6.2 Layered diagnosis
| ID | Requirement |
|---|---|
| DIA-1 | `diagnose <device> [--as <client>]` runs checks in order and stops at the first failing layer: connectivity → session/transport establishment → protocol handshake → identity → model (vs reference) → reports (the client's RCBs) → controls (authority, if requested). The layers below "identity" are generic; everything from "connectivity" through "protocol handshake" is supplied by the active protocol module (PROTO-9) and named concretely in its own spec. |
| DIA-2 | `diagnose` ends with a **verdict** (the most likely cause, with the certainty stated) and **next steps**. |
| DIA-3 | Where the protocol module reports only a generic connect failure, the tool must probe below it to separate the failure into specific layers (PROTO-9's `classify`). |
| DIA-4 | Most protocols have no standard way to report connection/session-limit usage. The tool must: (a) show the known maximum and release behaviour for the device's model/firmware if a quirks-style file (PROTO-12) has them; (b) classify how its own connection attempt was refused (refused outright, handshake rejected, accepted then closed, timeout) and, where the pattern fits, give the "limit probably exhausted / stale sessions from a client that disconnected abruptly" hint; (c) remind the operator that the tool itself occupies one slot. |
| DIA-5 | `discover <subnet>` scans for the protocol's listening port, briefly associates with each responder, reads identity and logical devices, and writes a draft inventory. It must be sequential and rate-limited, and release each association before moving on. |
| DIA-6 | v1 has no security support (§2.3). `diagnose` must still recognise when an IED appears to require it, via whatever signal the protocol module can detect (PROTO-10), and say so explicitly rather than reporting a generic connection failure. |

### 6.3 Device identification and edition

The models, vendors and editions in an experiment aren't known in advance. The tool must find
them out, and fall back to asking the operator only when it can't.

| ID | Requirement |
|---|---|
| IDN-1 | On association, collect identity from every available source: the protocol module's own identify mechanism if it has one (PROTO-3), `LPHD.PhyNam` (vendor, model, hwRev, swRev, serNum), and `LLN0.NamPlt` (vendor, swRev, configRev, ldNs) for each logical device. `info` shows all of them, including any disagreement between sources. The latter two sources are data-model based and available regardless of protocol module. |
| IDN-2 | Determine the edition for each device, in order of precedence: (1) inventory override, (2) SCL, (3) the namespace in `LLN0.NamPlt.ldNs` (2003 → Ed1; 2007 → Ed2; later revisions → Ed2.1), (4) model heuristics (whether Ed2-only attributes such as BRCB `ResvTms`/`Owner` or `LocSta` are present), (5) ask the operator. |
| IDN-3 | Every identified attribute (vendor, model, firmware, edition) carries its **source** and **confidence** (confirmed / inferred / operator-supplied / unknown), and output shows them. An inferred edition is never displayed as if confirmed. |
| IDN-4 | The operator is asked only when an edition-dependent check actually needs the answer. The question must be answerable by a non-expert: it names what to look for (device documentation, the configuration tool's project settings) and always offers **"don't know"**. |
| IDN-5 | "Don't know", and all non-interactive use (one-shot commands with `--json`, the future pytest phase), result in edition `unknown`. Edition-independent checks run; edition-dependent checks report `not-run` with the reason. The tool never blocks waiting for input in non-interactive mode. |
| IDN-6 | Operator-supplied and discovered identities can be written back to the experiment inventory (with confirmation), so each experiment is only asked once. |
| IDN-7 | Edition-dependent checks adapt: e.g. a missing `LocSta` is not an issue on Ed1, but is a warning on Ed2. |
| IDN-9 | Known vendor/model/firmware quirks that affect diagnosis or checks are recorded in a per-protocol-module data file (PROTO-12), keyed by identity. It starts empty per protocol module and grows from spikes and incident files (LOG-3). |

IDN-2's `LLN0.NamPlt.ldNs` → edition mapping (`LDNS_RULES` in `core/identity.py`): 2003 → Ed1,
`2007` and `2007A` → Ed2, `2007B` and later → Ed2.1 — **always marked `inferred`, never
`confirmed`**, because many real Ed2 devices report `2007A` when they're actually Ed2.1-capable
(open issue OI-9).

Protocol-specific identification concerns — e.g. object reference length limits that differ by
edition — are specified in the relevant protocol spec (see MMS-PROTOCOL-SPEC.md §5 for IDN-8).

## 7. Model browsing and file services

| ID | Requirement |
|---|---|
| MDL-1 | Browse the logical devices, logical nodes, data objects and data attributes, including FC and type. |
| MDL-2 | `describe` shows the type, the FC, whether the attribute is writable, and a plain-language description of the LN class, CDC and FC (§12). |
| MDL-3 | `dataset` lists the members of a dataset and whether each member resolves. |
| MDL-4 | The browsed model is cached per session. `ls --refresh` re-reads it. |

### 7.1 File services (read-only)

| ID | Requirement |
|---|---|
| FIL-1 | `files ls [path]` lists the device's file directory (name, size, last modified), if the protocol module supports file transfer (PROTO-8). |
| FIL-2 | `files get <path> [--out <local>]` downloads a file. Downloads are logged (LOG-1). |
| FIL-3 | No upload or delete in v1. On many IEDs, uploading a file installs configuration or firmware, which is a different class of risk from writing a value. |
| FIL-4 | COMTRADE and other files are downloaded as-is. Parsing and analysis are left to existing tools. |

## 8. Read and write

| ID | Requirement |
|---|---|
| RW-1 | `read` shows the value, type, FC, and quality and timestamp where present. |
| RW-2 | `watch <ref> [--interval]` polls and shows changes. |
| RW-3 | Value types for `write` must be taken from the device's model, never guessed from the input text. If the type is ambiguous or unsupported, the write is refused. |
| RW-4 | Before writing, show the current and new values and ask for confirmation. |
| RW-5 | After writing, read the value back and report a mismatch as a failure (the device accepted the write but didn't apply it). |
| RW-6 | Writes to FCs that aren't normally writable (ST, MX, …) are allowed only in expert mode, with a warning. The device's response is reported exactly as received. |
| RW-7 | Setting-group edits (SE/SG) go through `setgroup`, which runs the select-edit-confirm sequence. Plain `write` must refuse SE targets and point to `setgroup`. |
| RW-8 | Writes to CO attributes (e.g. `Oper`) through `write` must be refused, with a pointer to `operate` (§9). |

## 9. Controls

The IEC 61850 control model (SPC/DPC/INC, `ctlModel` and its sequences, orCat/orIdent, AddCause,
LastApplError) is defined at the data-model level (IEC 61850-7-x), not by the SCSM, so it is
specified here in full; only "how a Select/Operate PDU actually goes over the wire" is
protocol-specific (PROTO-6).

### 9.1 Sequences
| ID | Requirement |
|---|---|
| CTL-1 | Supported CDCs in v1: SPC, DPC, INC. |
| CTL-2 | Read `ctlModel` and run the matching sequence automatically: status-only → refuse; direct-normal → Operate; SBO-normal → Select, Operate; direct-enhanced → Operate, wait for CommandTermination; SBO-enhanced → SelectWithValue, Operate, wait for CommandTermination. |
| CTL-3 | Report the time taken by each step (select, operate, termination) and read back the resulting `stVal`. |
| CTL-4 | Decode failures to the named AddCause and LastApplError, with a hint (§12). |
| CTL-5 | `select` and `cancel` are also available as separate commands (for testing SBO timeout and cancel handling). |
| CTL-6 | Support the Test flag. For Ed2 devices, read and show Mod/Beh before operating. |

### 9.2 Pre-flight and confirmation
| ID | Requirement |
|---|---|
| CTL-7 | Before sending, show: object, CDC, ctlModel, current and target value, origin, check flags, test flag, and the relevant `Loc` / `LocSta` / `LocKey` values. If these indicate that the command will be blocked, warn *before* sending. |
| CTL-8 | Confirmation follows the safety profile (SAF-2, SAF-3). |
| CTL-9 | Interlock and synchrocheck check flags default on. Turning them off requires expert mode and is logged. |

### 9.3 Origin
The rack is reconfigured for each experiment, and a single device can legitimately send several
categories (e.g. a Station Manager sends remote-control for commands from the control centre and
station-control for its own HMI functions). So orCat belongs to the **command**, not to a device
or a relationship.

| ID | Requirement |
|---|---|
| ORG-1 | No silent default. The first control in a session asks the operator to choose bay-control (1), station-control (2) or remote-control (3). |
| ORG-2 | `set orcat <1-3|bay|station|remote>` in the shell; `--orcat N` per command. Values 0 and 4–8 require expert mode. |
| ORG-3 | Default `orIdent` = `ied-client/<user>@<host>`. Can be overridden. |
| ORG-4 | The tool must be able to show the origin of the last command a device received (`<DO>.origin`, `ctlNum`), so that operators can see what a neighbour actually sent. |
| ORG-5 | `authority-probe [<object>]` runs Select + Cancel for orCat 1, 2 and 3 (all values in expert mode) on every SBO object and shows a matrix of accepted/rejected with AddCause, next to the `Loc`/`LocSta`/`LocKey` values. Direct-control objects, and any pattern the protocol module declares unprobeable (PROTO-6), are listed as "not probeable". Nothing is operated. |

### 9.4 Logging
| ID | Requirement |
|---|---|
| CTL-10 | Every control is logged (§13). `restore` never replays or reverses controls. |

## 10. Reports

RCBs (BRCB/URCB) are a data-model concept (IEC 61850-7-2); this section is protocol-independent.
How report data actually arrives over the wire is the protocol module's concern (PROTO-7).

### 10.1 Levels
| Level | Action | Effect on the rack | Mode |
|---|---|---|---|
| A. Inspect | Read RCB attributes | None | standard |
| B. Subscribe (free) | Enable a disabled, unreserved RCB not assigned to another client in the SCD | Uses a spare RCB | standard |
| C. Takeover | Enable an RCB another client owns or is assigned | Disrupts that client | expert |

| ID | Requirement |
|---|---|
| RPT-1 | `rcb [<LN>]` lists RCBs with type (BR/RP), dataset, RptEna, Resv/ResvTms, Owner (resolved to an inventory name where possible), TrgOps, IntgPd, ConfRev, and BufOvfl for BRCBs. |
| RPT-2 | `subscribe <rcb>` enables and displays reports: arrival time, SqNum, trigger reason, values, EntryID and overflow for BRCBs. It must flag SqNum gaps. |
| RPT-3 | If no free instance is available, `subscribe` must list every instance with its owner or reservation state, rather than a bare error. |
| RPT-4 | Takeover requires expert mode, `--takeover`, and typed confirmation. The warning names the client that will lose the RCB. |
| RPT-5 | `gi` triggers a general interrogation on the subscribed RCB. |
| RPT-6 | Compare ConfRev against the reference (SCD/CID/snapshot) and flag a mismatch. |
| RPT-7 | **Cleanup (tested requirement):** on exit, including Ctrl-C and lost connection, disable every RCB the tool enabled, release its reservations where the IED allows, and restore any RCB parameters it changed. Anything it can't undo (e.g. a lingering ResvTms) must be reported with what it will affect and for how long. |
| RPT-8 | RCB parameter writes (TrgOps, IntgPd, OptFlds, …) are logged like any other write. |

## 11. Verification

SCL parsing, snapshot comparison and self-consistency checks compare the device's data model
against a reference; none of this depends on the wire protocol used to read that model, so it is
entirely specified here.

### 11.1 References
| ID | Requirement |
|---|---|
| VER-1 | References are optional. In order of preference: **SCD**, **CID**, **snapshot**, **device-supplied SCL** (VER-8). With none supplied, only self-consistency checks run and raw responses are shown for the operator to review. |
| VER-2 | Against a CID: the device's model, datasets, RCBs and ConfRev match its own configuration. |
| VER-3 | Against an SCD, additionally: cross-device consistency. Do the RCBs, datasets and client assignments that each neighbour expects exist and match? Do the addresses match the ConnectedAP entries? Does ConfRev match the SCD? (A CID exported from an older SCD can match its device and still be wrong for the rack.) |
| VER-4 | SCL parsing is implemented in this project. It must handle both the 2003 (Ed1) and 2007 (Ed2/Ed2.1) schemas, including mixed-edition SCDs. |
| VER-8 | If no reference is supplied, `check` looks in the device's file directory for SCL files (`.cid`, `.icd`, `.iid`, `.scd`, `.xml`) and offers any it finds as the reference. Such a reference is labelled **device-supplied**: the device's claim about its own configuration, not an independent reference. |

### 11.2 Snapshots
| ID | Requirement |
|---|---|
| VER-5 | `snapshot <device>` saves three layers as JSON (see table below). |
| VER-9 | A snapshot is strictly read-only: it must never write to or change the state of the device. In particular, only the **active** setting group is captured, because capturing other groups would require switching groups. |
| VER-10 | Attributes that cannot be read are recorded with their error (whatever the protocol module reports, PROTO-13), not skipped, so that changes in access rights appear in diffs. |
| VER-11 | Metadata: capture time and duration, tool version, experiment, device identity and edition (with source and confidence, IDN-3), and the count of unreadable attributes. |
| VER-12 | Reads are batched and rate-limited, and progress is shown. A full capture of a large IED may take minutes. |
| VER-13 | JSON with stable key ordering, so that snapshots can be committed alongside the experiment and compared with git as well. |
| VER-14 | An optional per-experiment ignore file lists attributes to leave out of diffs (e.g. counters). |

| Layer | Contents | Diff default |
|---|---|---|
| **Structure** | LD/LN/DO/DA tree with types and FCs; datasets and members; RCB, LCB and SGCB attributes | Any change = **fail** |
| **Configuration values** | All readable CF, SP, DC and EX values; SG values of the active setting group; identity (IDN-1) including ConfRev and swRev | Change = **warn** |
| **Operational state** | ST values that behave like configuration: `Mod`, `Beh`, `Health`, `Loc`, `LocSta`, `LocKey`, `LPHD.Sim`, `PhyHealth`; RCB runtime state (`RptEna`, `Resv`, `Owner`) | Change = **info** |

Other ST and MX values are not captured: they change continuously and would make every diff noisy.
| VER-6 | `diff <device> <snapshot>` compares the live device against a snapshot. `diff <snap> <snap>` compares two snapshots. |

### 11.3 Self-consistency checks (always run)
- An RCB references a dataset that doesn't exist.
- A dataset member doesn't resolve.
- An RCB is enabled or reserved by a client not in the inventory.
- A BRCB buffer has overflowed.
- A write that the model marks as writable is refused (when writes are tested).

### 11.4 Result categories
| ID | Requirement |
|---|---|
| VER-7 | Results are reported in two separate categories: **Configuration** (matches its reference) and **Communication** (a client, acting as the real neighbour, can do what the neighbour needs). A device passes only if both pass. The two are never combined into a single status. |

## 12. Explanation system

| ID | Requirement |
|---|---|
| EXP-1 | Every failure shows a one-line hint from the catalogue, unless `--terse` is set. |
| EXP-2 | `explain last`, `explain <code>` and `explain <term>` (LN class, CDC, FC, ctlModel, AddCause, …) give a fuller explanation and the next checks to try. |
| EXP-3 | Catalogue entries match on the error **and** its context (FC, CDC, service, mode). Example: an access-denied error on FC=ST → "not writable by design"; on FC=SP → "check access rights / source address". |
| EXP-4 | Hints must state their certainty: "likely cause", "check". A hint may state a diagnosis as fact only when the tool has proved it. |
| EXP-5 | The catalogue is a data file in the repo (e.g. YAML), editable without code changes. |
| EXP-6 | Descriptions are original text. IEC 61850 standard text must not be copied (copyright). |
| EXP-7 | **Minimum v1 coverage (acceptance criterion):** the catalogue must cover every error/status code the active protocol module's own catalogue (PROTO-13) declares, plus every control AddCause value (data-model level, protocol-independent). The concrete enumeration for the MMS module is in MMS-PROTOCOL-SPEC.md. |

## 13. Session logging and restore

| ID | Requirement |
|---|---|
| LOG-1 | Each session writes a JSONL log: commands, raw requests/responses (summarised), writes with before/after values, controls, RCB changes, and check results. |
| LOG-2 | `restore` puts back every value written in the session in reverse order, with confirmation. Controls are excluded (CTL-10). |
| LOG-3 | **Failure record:** `diagnose` and failed operations can be saved as an incident file (device, experiment, inventory, results, log excerpt) with `export --incident`. There is currently no record of rack failures; these files are meant to build one, and to feed the explanation catalogue with patterns observed in this rack. |

## 14. Architecture

```
  CLI (shell + one-shot)          [v1]
  pytest plugin                   [future phase]
        │
  Check engine        checks → structured results (status, category, evidence, raw responses)
        │
  Core client         session, browse, read/write, controls, RCBs, SCL, snapshots, explanations
        │
  Protocol module      the one thing that differs per protocol (§4.1); owns its client library
        │
  protocol client library
```

| ID | Requirement |
|---|---|
| ARC-1 | The CLI must be a thin layer over the core library. Anything the CLI can do must be callable from Python without the CLI. This keeps the future pytest phase from requiring a rewrite. |
| ARC-2 | Every check must produce a structured result: `status` (pass / fail / warn / info / not-run), `category` (Configuration / Communication), `evidence`, and the raw responses behind it. |
| ARC-3 | All output must be available as versioned JSON (`schema_version` field) as well as human-readable text. The `tool` block may carry protocol-module version metadata (PROTO-11) alongside the tool's own version. |
| ARC-4 | All access to a protocol module's underlying client library must go through that module's own adapter layer. No other code may import the library directly. (The MMS module's instantiation of this — `pyiec61850-ng`, ctypes — is PLT-3/PLT-4 in MMS-PROTOCOL-SPEC.md.) |
| ARC-5 | The client (core, CLI, check engine) must not import any protocol module. It reaches modules only through the contract (§4.1) and the registry of installed modules, so a module can be added, replaced or removed without changing the client. |

## 15. Rack inventory

One inventory file per experiment, e.g. `experiments/<name>.yaml`.

| ID | Requirement |
|---|---|
| INV-1 | Per device: name, address, port, reference (SCD/CID path or snapshot), role (e.g. station-manager, bcu, ied), and optional identity overrides (vendor, model, edition; see IDN-2). The schema must reserve a `security` block that v1 ignores, so that adding security later doesn't change the schema. |
| INV-2 | Per client relationship: the client, the server, the client's source address, and the RCBs it uses. orCat is **not** part of the inventory (§9.3). |
| INV-3 | Rack-level settings: `safety` profile (SAF-1). |
| INV-4 | `inventory from-scd <file>` creates a draft from an SCD. `discover` creates one from the network (DIA-5). `inventory validate` checks the file against the network. |
| INV-5 | Commands refer to devices and clients by inventory name. A raw address is also accepted for quick use. |
| INV-6 | Per device, a `protocol` field names which protocol module drives it (default and currently only value: `mms`; `--protocol` selects one for a raw address). Module-specific connection parameters live under a nested block named for that protocol (e.g. `mms:` for anything MMS-specific), so the schema doesn't grow protocol-specific top-level fields. |

## 16. Recommended rack practices

These are not code requirements, but the tool works best when they are followed.

1. **Test-client RCB instances.** When engineering each experiment's SCD, assign one RCB instance
   per dataset to a "test client". This gives report subscription a legitimate block to use, so
   takeover is only needed for copying a neighbour's session exactly. (Not currently feasible;
   recorded as a recommendation.)
2. **Export SCD/CID per experiment** where the devices allow it, and store it next to the
   inventory.
3. **Take a snapshot when an experiment is working**, so there is a baseline for later diffs.
4. **Save an incident file** (LOG-3) for every failure investigated.

## 17. Phases

1. **Phase 0: Spikes.** Protocol-module-specific technical risks; see the protocol spec.
2. **Phase 1: Core + CLI (v1).** Everything in §2.1.
3. **Phase 2: Automated testing.** pytest plugin and fixtures over the core library, assertions
   on check results, JUnit output, rack test suites per experiment.
4. **Later:** APC/BSC/ISC controls, GUI/TUI, a second protocol module, further items from the
   open issues below.

## 18. Open issues

| # | Topic | Notes |
|---|---|---|
| OI-4 | Layered probe design | Generic framework here (§6.2); concrete probes are protocol-specific — see MMS-PROTOCOL-SPEC.md. |
| OI-5 | Python version | Protocol-module-specific pinning question; see MMS-PROTOCOL-SPEC.md. |
| OI-9 | ldNs → edition mapping | IDN-2 says "2007 → Ed2; later revisions → Ed2.1". Implemented as 2003 → Ed1, 2007 and 2007A → Ed2, 2007B and later → Ed2.1 (always *inferred*), because many Ed2 devices report 2007A. Confirm or correct (`core/identity.py`, `LDNS_RULES`). |

Protocol-module-specific open issues (object reference length limits, etc.) are tracked in that
module's own spec, not here.

## 19. Decision record

| Date | Decision |
|---|---|
| 2026-09-23 | SCL comparison is optional. SCD, CID and snapshot are all valid references. Self-consistency checks always run. |
| 2026-09-23 | Configuration and Communication results are reported separately. |
| 2026-09-23 | v1 is the CLI (shell + one-shot). Automated testing is deferred to phase 2, but the library/CLI layering is kept in v1. |
| 2026-09-23 | Linux only (Ubuntu, Kali). |
| 2026-09-23 | Controls are in v1 (SPC, DPC, INC). The rack is isolated, but strict confirmation is the default. |
| 2026-09-23 | orCat is per command, not per device or relationship. 1–3 are always offered. |
| 2026-09-23 | Report inspection, free subscription and takeover (expert only) are all in v1. Test-client RCBs are recommended. |
| 2026-09-23 | Target users include non-experts. Strong explanation system. Standard and expert modes. |
| 2026-09-23 | OI-1: no security in v1. Inventory schema reserves a `security` block; `diagnose` detects IEDs that require security (DIA-6), via a module-specific signal (PROTO-10). |
| 2026-09-23 | OI-2: all editions supported (Ed2.1 treated as Ed2 plus additions). Models, vendors and editions are unknown per experiment: discovered by the tool, with the operator asked only as a fallback (§6.3). |
| 2026-09-23 | OI-3: read-only file services in v1 (list, download), where the protocol module supports file transfer at all. Upload and delete deferred. Device-stored SCL files may serve as a labelled reference. |
| 2026-09-23 | OI-6: snapshots have three layers (structure / configuration values / operational state) with fail / warn / info diff defaults. Snapshots are strictly read-only. GoCB/SVCB inspection deferred. |
| 2026-09-23 | OI-7: IEC 61850 logs deferred until after the prototype. LCB attributes remain in the snapshot structure layer (reads only). |
| 2026-09-23 | OI-8: connection/session limits in v1 come from a quirks-style file (PROTO-12) and refusal classification (DIA-4). Active probing deferred to v2. |
| 2026-09-25 | Spec split into this protocol-independent client spec and a per-protocol spec (MMS-PROTOCOL-SPEC.md), joined by the protocol module contract in §4.1, so that a future non-MMS module could reuse the CLI, safety model, verification engine and explanation system without rewriting them. `SPEC.md` is now a short index pointing to both. |
| 2026-09-25 | Code split to match (ARC-5): the client is the `ied_client` package, the protocol module contract is `ied_client.protocol`, and the MMS module is the separate `mms_protocol` package, found through the `ied_client.protocols` entry point. Error-code domains are an open registry that modules extend. OI-9 moved here from the MMS spec, since the `ldNs` mapping reads a data-model attribute and is protocol-independent. |
