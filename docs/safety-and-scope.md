# Safety, modes and scope

The IEDs in the rack are real devices with real outputs. The rack is isolated from the grid, but a
control operates real contacts. This page explains what the tool does to protect you from mistakes,
and — just as important — what it does **not** do.

## Modes are a guardrail, not access control (MOD-4)

`mms-client` has two modes:

| | standard (default) | expert (`--expert`, or `set mode expert` in the shell) |
|---|---|---|
| browse, read, `watch`, `rcb`, `check`, `snapshot`, `diff`, `diagnose` | yes | yes |
| write normally writable FCs (SP, SV, CF, DC, EX, BL) | yes, with confirmation | yes |
| write other FCs (ST, MX, …) — negative testing | no | yes, with a warning; the device's answer is shown exactly |
| controls (SPC, DPC, INC) with interlock and synchrocheck checks **on** | yes | yes |
| turn interlock / synchrocheck checks off | no | yes (logged) |
| orCat 1–3 (bay, station, remote) | yes | yes |
| orCat 0 and 4–8 | no | yes |
| subscribe to a *free* report control block (not enabled, not reserved, not assigned to another client) | yes | yes |
| take over an RCB another client owns or is assigned (`--takeover`) | no | yes, with typed confirmation naming the client that loses it |

**Anybody can type `--expert`.** Modes exist so that a slip of the keyboard does not do something you
did not intend. They are not a security feature, and the tool has no user accounts, passwords or
permissions. Protect the rack the way you protect any lab network.

The current mode is always shown in the shell prompt.

## Confirmations (SAF-1 … SAF-3)

The inventory's `safety:` setting chooses how controls are confirmed:

* `strict` (default): you type the object name (e.g. `CSWI1.Pos`) before a control, a select, an
  authority probe or an RCB takeover is sent.
* `lab`: a `y/N` question is enough.

Writes always show the current and the new value and ask `y/N`. `--yes` answers that question for
writes (useful in scripts). **`--yes` never confirms controls or takeovers.** A run that cannot ask
(`--json`, no terminal) will refuse a control rather than send it unconfirmed.

## What the tool undoes, and what it never touches

* Report control blocks: everything the tool enabled, reserved or re-parameterised is undone when you
  unsubscribe, leave the shell, press Ctrl-C, or the process receives SIGTERM (RPT-7). If the connection
  is lost first, the tool tells you what lingers and for how long (for example a buffered RCB stays
  reserved for its `ResvTms`).
* `restore` writes back every value written in the session, newest first, after confirmation.
* **Controls are never replayed or reversed** by `restore` (CTL-10). Undoing a control is a new control
  that you decide on.
* Snapshots never write to the device, not even to read other setting groups (VER-9).
* There is no file upload or delete (FIL-3): on many IEDs, uploading a file installs configuration or
  firmware.

## orCat: chosen per command, never guessed

The originator category (bay, station, remote) belongs to the command, not to a device: the same
Station Manager legitimately sends remote-control for SCADA commands and station-control for its own
HMI. The first control in a session asks you which one to use; `set orcat` or `--orcat` changes it. The
tool never picks one silently.

## What the tool cannot see

* **GOOSE and Sampled Values are not MMS.** This tool cannot observe or diagnose them. Bay-level
  interlocking or tripping between BCUs and IEDs that runs over GOOSE is invisible to it, even when every
  MMS check passes. Use a packet capture (Wireshark) on the process/station bus for those paths.
* **Traffic between two other devices.** The tool is an active MMS client: it can act *like* a
  neighbour (`--as <client>`), but it cannot watch a conversation between the Station Manager and a BCU.
* **Security.** v1 supports neither ACSE authentication nor TLS (IEC 62351). `diagnose` recognises when
  a device appears to require them and says so, instead of reporting a generic failure.

## Association slots

IEDs accept a limited number of MMS associations, and IEC 61850 has no standard way to ask how many are
in use. While connected, **this tool occupies one slot**. Prefer the shell (one association for many
commands) over many one-shot commands, and leave the shell when you are done. `diagnose` explains what a
refused association most likely means, including stale associations left by a client that disconnected
abruptly.
