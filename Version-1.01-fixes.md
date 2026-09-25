# mms-client Version 1.01: fixes

**Base:** commit `f89fe39` ("V1"), reviewed in `Version-1.0-bugs.md`.
**Date:** 2026-09-25. **State:** uncommitted changes in the working tree: 24 files, 929 lines added and 107
removed. Of the added lines, 433 are tests and 496 are source.

| | Version 1.0 | Version 1.01 |
|---|---|---|
| `uv run pytest` | 1175 passed, 1 deselected | **1199 passed**, 1 deselected (24 new regression tests) |
| `uv run ruff check src tests` | clean | clean |
| `uv run pytest -m soak` (RSK-5) | RSS growth 284 kB | RSS growth 272 kB, 1090 ops/s |

What this release fixes:

* the four bugs from §1 of the review, each re-run against the simulated IED after the fix;
* six of the eight partly-met requirements from §2 (2.1–2.6 and 2.8);
* a related bug found while fixing RPT-7: without a bound local address, the tool didn't recognise its own RCB
  reservations.

Section "Not changed" lists what was left out, and why.

## Summary

| # | Req. | Problem in 1.0 | Fix in 1.01 |
|---|---|---|---|
| 1.1 | RPT-7 | A buffered RCB stayed reserved by the tool (ResvTms) after cleanup, and nothing said so | The reservation is released (ResvTms := 0) and the RCB read again; anything that lingers is reported with its effect and duration |
| 1.2 | CTL-5 | `select` followed by `operate` always failed (the device refused a second select) | `operate` uses the earlier selection and doesn't select again |
| 1.3 | LOG-2 | A second `restore` re-applied the values the first one had undone | Restore marks its own writes; writes that were already restored are skipped |
| 1.4 | RW-7 | Declining a `setgroup edit` left the SGCB in edit mode until the session ended | Any refusal or failure after `EditSG := n` ends the edit at once |
| 2.1 | IDN-6 | The operator's edition answer was lost when the identity was read again, and could never be saved | The answer is kept for the session, and the tool offers to save it to the inventory straight away |
| 2.2 | IDN-3, VER-4 | An edition from any SCL file was labelled "confirmed" | "Confirmed" only when the file states it for this IED; otherwise "inferred" |
| 2.3 | DIA-1 | With a snapshot reference, diagnose's model layer passed without comparing anything | The model tree is compared with the snapshot; any difference stops the diagnosis |
| 2.4 | DIA-1, INV-2 | Diagnose didn't find RCBs named in the IEC form (`LD/LLN0.brcbA01`) | Diagnose uses the same name matching as `rcb` and `subscribe` |
| 2.5 | CLI-7, EXP-4 | Codes the device never sent: `object-does-not-exist` for an unreadable ctlModel; AddCause `select-failed` for an empty SBO read | The device's own error, or a tool code that says it is the tool's finding |
| 2.6 | ORG-5 | `authority-probe` ignored a refused Cancel, so the next orCat looked like an authority refusal | A refused Cancel is reported, and probing of that object stops |
| 2.8 | VER-8 | Device-stored SCL files were only used with `--device-scl` | Without a reference, an interactive `check` looks for them and offers them |
| extra | RPT-1, RPT-7 | Without a bound local address, the tool didn't recognise its own RCB Owner | The session works out its own source address towards the device |

## Details

### 1.1 RPT-7: buffered RCB reservation (`core/reports.py`, `core/session.py`)

* `subscribe` now registers a reservation cleanup for every BRCB that has ResvTms. It is registered before
  anything is written, so it is undone last, after RptEna and any changed parameters.
* The cleanup releases (ResvTms := 0) only a reservation held for this tool's own address. After a takeover
  that includes the reservation the tool took over: the previous holder's reservation can't be given back, and
  releasing it lets that client enable the RCB again at once. A reservation held by another address is left
  alone and reported. A reservation made by configuration (ResvTms = -1) is never touched.
* `CleanupAction` gains an optional `verify` step: after a successful undo, the device is read again. Anything
  still in effect is reported as `still in effect: …`, naming the clients it blocks and for how long, e.g.
  "stays reserved for this tool's address for up to 10 s after the association ends; station-manager-1 cannot
  enable it until then". An undo may also return its own note instead of "undone: …".
* If the connection is lost, the reservation cleanup's message states the device's actual ResvTms. It is read
  after enabling, because it is only known once the device has reserved the RCB.
* Simulator run, same steps as the review: after `stop`, ResvTms is 0, Owner is empty, and a new client sees
  the RCB as **free**. In 1.0 it stayed reserved (ResvTms=10) by 127.0.0.1.

**Extra fix found on the way.** `Session.owner_name` said "this tool" only if the association was bound to a
local IP. Without `--bind`/`--as`, the tool therefore didn't recognise its own reservation, and the simulator
run showed this.

* The session now works out its own source address: the bound address, else the address the OS routes from
  (a UDP `connect`, which sends nothing).
* A new `Session.is_own_owner()` compares Owner with that address, independent of inventory names. With `--as`,
  the tool's address is also the neighbour's address, so the inventory resolves it to the neighbour's name.

### 1.2 CTL-5: operate after select (`core/controls.py`, `cli/commands/controls.py`)

* `plan_control` notices a selection made with `select` and shows it in the pre-flight: "selection: made 3.2 s
  ago with `select`; operate only". It adds warnings in two cases:
  * the selection has probably expired (sboTimeout), so the device is expected to answer object-not-selected,
    which is exactly what an SBO-timeout test wants to see;
  * for SBO-enhanced, the operate value differs from the value the selection was made with.
* `execute` consumes the selection and sends only Operate (plus the CommandTermination wait for enhanced
  security). `ControlOutcome.used_earlier_select` records this, and the CLI says so.
* Selections are cleared on connect and disconnect, because the device deselects when the association ends.
* Simulator run: `select` then `operate` now succeeds for SPCSO2 (SBO-normal) and SPCSO4 (SBO-enhanced,
  positive CommandTermination). The next operate runs the full sequence again.

### 1.3 LOG-2: repeatable restore (`core/restore.py`, `core/sessionlog.py`, `core/readwrite.py`, `core/setgroup.py`)

* `write`, `setgroup.edit` and `setgroup.activate` accept `log_extra`. Restore uses it to tag the writes it
  makes with `restored_from` (a sequence number) and `restored_from_session`, so they are never restorable
  themselves.
* Each item's outcome is logged as a new `restore` entry (it used to be `write` or `note`). Only an ok `restore`
  entry proves that a value was put back: the restore's own write entry is "ok" even when the read-back fails.
* `plan_restore` skips writes that have already been restored successfully. It finds that out from markers in
  the source log itself, and, when restoring an earlier session (`--from`/`--last`), from markers in the current
  session's log. The plan reports how many writes it skipped (`already_restored`).
* Markers in pre-1.01 logs (`write` entries with `restored_from`) are still understood, within their own log.
* Simulator run: after one restore, a second restore plans nothing, where 1.0 wrote 11 and then 10. A new write
  after the restore is restorable, and only that write.
* `log` shows `restore` entries as "… restored to X (undoing #n)".

### 1.4 RW-7: declined setting-group edit (`core/setgroup.py`)

`edit` ends the edit session (EditSG := 0) as soon as anything goes wrong after `EditSG := group`: a declined
confirmation, Ctrl-C, or a device error. The exception still propagates. If the release itself fails, the
registered cleanup tries again at the end of the session and reports what lingers, as before. Simulator run:
after a decline, EditSG is 0 and no cleanup is pending.

### 2.1 IDN-6: the operator's edition answer (`core/identity.py`, `verify/checks.py`, `inventory_ops.py`)

* New `identity.identify(session)`. Every command that reads the identity (`info`, `diagnose`, `snapshot`, and
  the edition question in `check`) goes through it, so the inventory override, the SCL reference and the
  operator's answer are always applied.
* The operator's answer is kept in `Session.operator_edition`. It is used as the last step of IDN-2, when the
  inventory, SCL, `ldNs` and model heuristics all give nothing.
* Right after the operator answers, the tool offers to save the edition to the inventory. This happens only
  when the session is interactive and the device is in an inventory file; declining changes nothing. This is
  what makes "each experiment is only asked once" work in one-shot mode too.
* `info --save-identity` now saves an edition the operator gave earlier in the session.

### 2.2 IDN-3 / VER-4: confidence of an SCL edition (`core/identity.py`, `verify/reference.py`)

`Reference.scl_edition()` returns an `SclEdition(edition, reason, confirmed)`:

* **confirmed** when the IED declares its own `originalSclVersion`, or the file describes this IED alone
  (CID/ICD/IID);
* **inferred** when the edition only comes from the schema version of an SCD that holds several IEDs. The
  reason then says that the file doesn't state this IED's own edition.

The IDN-2 precedence (SCL before `ldNs`) is unchanged. The existing `edition-mismatch` check still warns when
SCL and `ldNs` disagree.

### 2.3 DIA-1: snapshot reference in diagnose (`diagnosis/diagnose.py`, `verify/diff.py`, `verify/snapshot.py`)

* New `snapshot.model_structure(model)` and `diff.model_differences(snapshot, model)` compare the snapshot's
  model tree (LD/LN/DO/DA with types and FCs) with the browsed model, without reading anything more.
* Any difference fails the model layer and stops the diagnosis with a fact-level verdict ("Structure: any
  change = fail", §12.2). Datasets and control blocks still need `check`.
* A reference with no model to compare is now reported as not-run instead of passing silently.

### 2.4 RCB names in diagnose (`core/reports.py`, `diagnosis/diagnose.py`)

`_same_rcb` became the public `same_rcb` and is used by diagnose's reports layer. `LD/LLN0.BR.brcbA01`,
`LD/LLN0$BR$brcbA01` and `LD/LLN0.brcbA01` now mean the same RCB everywhere.

### 2.5 CLI-7 / EXP-4: no invented codes (`core/controls.py`, `data/hints/tool.yaml`)

* New `read_ctl_model()`:
  * a failed read raises the device's own error (ServiceError, or the DataAccessError it returned);
  * a ctlModel missing from the model, or not an integer, raises the new `tool:ctlmodel-unusable`, which has its
    own catalogue entry.
* `authority-probe` shows the same error in its note ("ctlModel unreadable (data-access:…)").
* An empty SBO answer to a normal-security select is reported as `tool:control-refused`. That code's existing
  select-context hint describes exactly this case. It is no longer reported as AddCause `select-failed`.

### 2.6 ORG-5: refused Cancel in `authority-probe` (`core/controls.py`)

The result of Cancel is checked. If it is refused:

* the cell says "cancel refused (…)";
* the row notes that probing stopped because the object stays selected until its sboTimeout;
* the remaining orCats are shown as "not probed", not as authority refusals.

### 2.8 VER-8: device-supplied SCL (`verify/reference.py`, `cli/commands/verification.py`, `core/files.py`, `verify/checks.py`)

* `reference_for(device_supplied=None)` is the new default. When no reference is given and the operator can be
  asked, it lists the device's file directory and offers any SCL files ("Don't use any" is the default).
  `--device-scl` still forces it, and a non-interactive run still needs it.
* A non-interactive run never lists files on its own. Its "no reference" result now says to use `--device-scl`.
* A device without file services no longer overwrites `explain last` during this automatic lookup.

## Behaviour changes operators will notice

* `subscribe` on a buffered RCB: cleanup prints an extra line, "undone: reservation of … (ResvTms back to 0)".
  If something lingers, it prints "still in effect: …".
* `rcb` and the other RCB commands show "this tool" as Owner without `--bind`/`--as` too.
* `operate` after `select` shows a "selection" row in the pre-flight, and a note that no new select was sent.
* `restore` run a second time says there is nothing to restore. The plan mentions writes that were already
  restored. The session log has a new entry kind, `restore`.
* The edition question in `check` is followed by an offer to save the answer, if the device is in an inventory
  file.
* An interactive `check` without a reference may ask whether to use an SCL file found on the device.
* The error code for a refused normal-security select is `tool:control-refused`, no longer
  `add-cause:select-failed`.

## Not changed

* **2.7 CTL-1 (ENC).** This needs your decision: should ENC controls (e.g. `LLN0.Mod`, used to put a function
  into test mode) be supported in v1?
  * *Recommendation:* support ENC with the same services as INC, and add it to CTL-1. Otherwise refuse it
    consistently, which needs a heuristic when there is no SCL (ctlVal is INT8 for enums, INT32 for INC, and
    Mod/Beh/Health are known ENC names).
  * Today the behaviour depends on whether an SCL reference is loaded.
* **§3 OSI selectors and AP titles.** This is a feature (inventory schema, association parameters, diagnose
  probes), not a bug fix. It is still the most valuable next item.
* **§4 spec text** (RPT-1/§12.3 BufOvfl, the §6.2 command table, the VER-6 table row, VER-14, IDN-8/OI-10,
  Phase 0 gating). I left `SPEC.md` alone: these are your design decisions.
* **§5 improvements**:
  * restore should compare the current value with the logged "after" value before writing;
  * a read-back retry for RW-5;
  * the ARC-1 and ARC-2 refactors;
  * requirement IDs in tests.

  The new regression tests cite their requirement IDs in their docstrings.
* **Version number.** `pyproject.toml` still says `0.1.0`, and snapshots and JSON envelopes record that. Bump it
  if "1.01" should be visible in snapshot metadata.

## Known limitations of these fixes

* **Owner identifies an address, not an association.** Another client on the same machine as the tool, such as
  a second mms-client, is indistinguishable from the tool. RPT-7 cleanup could then release that client's BRCB
  reservation. This was already true for bound addresses; it now also applies without binding.
* **Restore markers across sessions.** They are found in the source log and in the current session's log. If an
  earlier log is restored with `--from X` in one session, then again with `--from X` in a later session, the
  second run doesn't see the first run's markers, and asks to restore again. `--last` is not affected: after a
  restore, the latest earlier log is the one that did the restore.
* **Tested against the simulator only.** The refused-Cancel and unreadable-ctlModel paths are tested by patching
  the adapter, because the simulator can't produce them. Whether real IEDs accept `ResvTms := 0` from the
  reserving client is vendor-specific. That belongs to the RSK-6 re-runs on real devices; the quirks file is the
  place to record it.

## New tests

Integration tests, against the simulated IED:

* `test_core.py`:
  * `test_brcb_reservation_released_on_stop`
  * `test_operate_uses_the_earlier_select[SPCSO2|SPCSO4]`
  * `test_sbow_select_with_another_value_is_warned`
  * `test_empty_sbo_answer_is_not_reported_as_a_device_add_cause`
  * `test_authority_probe_stops_when_cancel_is_refused`
  * `test_unreadable_ctl_model_reports_the_devices_error`
  * `test_declined_setgroup_edit_releases_the_edit_session`
  * `test_restore_twice_changes_nothing`
  * `test_restore_of_an_earlier_log_twice_changes_nothing`
* `test_diagnose.py`:
  * `test_snapshot_reference_is_compared_in_the_model_layer`
  * `test_client_rcb_named_in_iec_form_is_found`
* `test_verify.py`:
  * `test_operator_edition_is_offered_for_saving`
  * `test_device_scl_is_offered_without_being_asked_for`

Unit tests (`tests/unit/test_core_units.py`):

* BRCB reservation cleanup, with a fake client:
  * `test_brcb_reservation_is_released_and_nothing_lingers`
  * `test_brcb_reservation_that_lingers_is_reported_with_effect_and_duration`
  * `test_brcb_reservation_refused_release_is_reported`
  * `test_brcb_reserved_by_configuration_is_not_released`
  * `test_brcb_reservation_held_by_another_client_is_left_alone`
* Restore markers:
  * `test_restored_seqs_counts_only_successful_restores_of_the_source_session`
  * `test_restored_seqs_reads_pre_1_01_markers_only_in_their_own_log`
  * `test_restorable_writes_skips_restore_writes_and_already_restored`
* Editions:
  * `test_scl_edition_is_confirmed_only_when_the_file_states_it_for_the_ied`
  * `test_operator_edition_is_the_last_resort_and_survives_a_new_identity`
