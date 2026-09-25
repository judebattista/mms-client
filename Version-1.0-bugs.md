# mms-client Version 1.0: review against SPEC.md

**Reviewed:** commit `f89fe39` ("V1"), 2026-09-24. **Spec:** `SPEC.md` Draft 0.3.
**Test status at review time:** `uv run pytest`: 1175 passed, 1 deselected (the soak test, excluded by default).
`uv run ruff check src tests` is clean.

Line references below are to commit `f89fe39`. The fixes are described in `Version-1.01-fixes.md`.

## Summary

The code matches the spec closely. All the spec's requirement areas are implemented, and the requirement IDs
are cited throughout the code. The problems are at the edges:

* four bugs, reproduced on the simulated IED (`tests/sim`);
* eight requirements that are only partly met, found by reading the code;
* one real gap that the spec doesn't cover (OSI selectors and AP titles);
* some spec text that is wrong or out of date.

## 1. Bugs reproduced on the simulated IED

Each was reproduced with a short script against `SimProcess(config=tests/fixtures/sim/basic.cfg)`, using the
`make_session` helper from `tests/integration/test_core.py`.

### 1.1 RPT-7: the tool leaves a reservation on buffered RCBs and doesn't report it

*Where:* `src/mms_client/core/reports.py:481` (subscribe registers only the RptEna cleanup for a BRCB) and
`Subscription.stop` (`reports.py:297`).

*Reproduction:* `subscribe brcbMeas01`, then stop.

| moment | RptEna | ResvTms | Owner |
|---|---|---|---|
| before subscribing | false | 0 | (empty) |
| after stop (cleanup note: `undone: RptEna of SIMCTRL/LLN0.BR.brcbMeas01`) | false | 10 | 7f000001 |
| a new association, after the tool released its own | false | 10 | 7f000001 |

A new client sees the RCB as "reserved (ResvTms=10) by 127.0.0.1". Writing `ResvTms := 0` is accepted and
releases the reservation on the simulator. RPT-7 requires the tool to "release its reservations where the IED
allows", and to report anything it can't undo, with its effect and how long it lasts. The tool does neither for
BRCBs.

### 1.2 CTL-5: `select` followed by `operate` always fails

*Where:* `src/mms_client/core/controls.py:425` (`execute`). The earlier select is stored in `session.selected`,
but `execute` never looks at it and selects again.

*Reproduction:* `select_only(...)`, then `execute(...)` on the same object:

* `GGIO1.SPCSO2` (sbo-with-normal-security): the second select reads an empty SBO value, and the tool reports
  `add-cause:select-failed`.
* `GGIO1.SPCSO4` (sbo-with-enhanced-security): the device refuses SelectWithValue with
  `add-cause:object-already-selected (19)`.

The `select` command tells the operator to "Operate or `cancel` it within sboTimeout", so the normal CTL-5
workflow (testing SBO timeout and operate-after-select) is broken. The entry also stays in `session.selected`.

### 1.3 LOG-2: `restore` is not repeatable

*Where:* `restore_value` → `readwrite.write` logs an ordinary `write` entry without `restored_from`
(`src/mms_client/core/readwrite.py:279`). `restorable_writes` therefore treats the restore's own writes as
operator writes, and the original writes stay restorable.

*Reproduction:* `Setp1.setVal` is 10. Write 11, then restore: the value is 10. A second `restore` plans
`[(seq 5, back to 11), (seq 4, back to 10)]`: it writes 11 and then 10, when nothing should be written.
`restore --last` from a later session has the same problem.

### 1.4 RW-7: declining a `setgroup edit` leaves the setting group in edit mode

*Where:* `src/mms_client/core/setgroup.py:196`. `EditSG := group` is written before the confirmation, and the
`try` block only catches `ServiceError`, so `ConfirmationDeclined` leaves the edit open.

*Reproduction:* `setgroup.edit(s, 2, …)` with the confirmation declined. Afterwards `EditSG` is still 2, and
`sgcb:SIMCTRL:edit` is still pending in the cleanup registry, so the edit stays open until the session ends.
In a long shell session this blocks other clients from editing settings.

## 2. Requirements only partly met (found by reading the code, not run)

1. **IDN-6: an edition the operator gives can never be saved.** `info` rebuilds the identity and overwrites
   `s.identity` (`src/mms_client/cli/commands/connection.py:210`), so an answer given during `check` is lost.
   `info --save-identity` only saves an operator-supplied edition, and that can only come from the inventory
   already (`connection.py:178`). In one-shot mode the operator is asked again every time, although IDN-6 says
   each experiment should be asked only once. `snapshot` and `diagnose` also overwrite `session.identity`.
2. **IDN-3 / VER-4: an edition taken from SCL is always "confirmed".** See
   `src/mms_client/core/identity.py:258`. When it comes from the document-level schema version of an SCD, this
   labels every IED in a mixed-edition SCD with the SCD's edition, e.g. an Ed1 device as "Ed2 confirmed". It
   should be *inferred* unless the IED itself declares `originalSclVersion`, or the file describes only that IED.
3. **DIA-1: with a snapshot reference, the model layer passes without comparing anything.**
   `src/mms_client/diagnosis/diagnose.py:362` only compares SCL references and marks "no reference" as not-run.
   A snapshot reference is neither compared nor marked not-run.
4. **The two ways of matching RCB names differ.** Diagnose's reports layer (`diagnose.py:456`) accepts only
   `LD/LN.FC.name` or `LD/LN$FC$name`. `rcb` and `subscribe` (`reports.py:119`, `_same_rcb`) also accept the IEC
   form `LD/LLN0.brcbA01`. An inventory entry in the IEC form makes diagnose report "RCB not found on the device",
   stated as fact.
5. **CLI-7 / EXP-4: the tool reports codes that the device never sent.**
   * An unreadable `ctlModel` always appears as `ied:object-does-not-exist (22)`, whatever the real error was:
     `_read_opt` discards it (`controls.py:226`, `controls.py:324`).
   * An empty SBO read is reported as AddCause `select-failed` (3), as if the device had sent that AddCause
     (`controls.py:446`).
6. **ORG-5: `authority-probe` ignores the result of Cancel** (`controls.py:651`). If a device refuses Cancel,
   the object stays selected, the next orCat's select fails with "object-already-selected", and the matrix shows
   that as an authority refusal.
7. **CTL-1: ENC controls are handled inconsistently.** Without a reference, an ENC object (e.g. `LLN0.Mod`)
   looks like INC over MMS and is allowed (`controls.py:216`). With an SCL reference, the same object is refused
   as ENC. The spec needs to decide whether ENC is supported in v1.
8. **VER-8: looking for SCL files on the device is opt-in.** It only happens with `check --device-scl`
   (`src/mms_client/cli/commands/verification.py:78`). The spec says that when no reference is given, `check`
   looks for them and offers any it finds.

## 3. A gap the spec should close: OSI selectors and AP titles

The SCL parser reads the `Address` P-types (`OSI-PSEL`, `OSI-SSEL`, `OSI-TSEL`, `OSI-AP-Title`,
`OSI-AE-Qualifier`), but nothing uses them:

* associations and the layered probes always send libiec61850's defaults
  (`src/mms_client/diagnosis/iso.py:44`);
* VER-3's "do the addresses match the ConnectedAP entries" compares only the IP
  (`src/mms_client/verify/reference.py:466`).

A wrong AP title or selector is a classic cause of association failures in a rack, and `--as` can't reproduce
what the real neighbour sends. Recommendation:

* add an optional `osi:` block to the inventory (INV-1), filled in by `inventory from-scd`;
* apply it to the association;
* have `diagnose` try both the defaults and the SCD values.

I'd put this ahead of most of the other items.

## 4. Spec text to fix

* **RPT-1 and §12.3:** BufOvfl isn't an RCB attribute; it only appears in reports. The code handles this
  correctly (it uses the last report received, or reports not-run); the spec wording is wrong.
* **§6.2:** the command table is missing `origin` (ORG-4), the `setgroup show / activate / edit` subcommands,
  and `pwd`, `help` and `exit`.
* **VER-6:** the row sits outside any table (`SPEC.md:293`), so the markdown table is broken.
* **VER-14** says the ignore file is "per-experiment", but it is only a CLI flag. Add an `ignore:` path to the
  inventory.
* **IDN-8 / OI-10:** still not implemented (reports not-run). Either make it a v1 exit criterion or defer it
  explicitly.
* **Phase 0 isn't finished.** The spikes ran only against the simulator (`docs/spikes.md`). RSK-1, RSK-3 and
  RSK-6 still need real IEDs, and RSK-4 still needs the Ubuntu install test. §18 says these come before
  building on the features, so they should gate the v1 release.

## 5. Other suggested improvements

* **Restore from an earlier log:** before writing, check that the current value still equals the logged
  "after" value, and skip it if not. Otherwise `restore --last` can overwrite someone else's later change.
* **RW-5:** the write read-back is immediate, while controls wait up to 1 s for `stVal`. A short retry would
  avoid false "not applied" failures on devices that apply values asynchronously.
* **ARC-1:** some logic lives in the CLI layer (the one-shot incident assembly in
  `cli/commands/session.py`, `_save_identity` in `cli/commands/connection.py`). Diagnose also imports the private
  `_scl_model_checks` and `_communication_checks` from `verify/reference.py`; make them public core functions.
* **ARC-2:** the `raw` field is filled for RCB and dataset checks, but not for model and value checks. Either
  fill it or relax the requirement.
* **Traceability:** tests rarely cite requirement IDs; for example, the RPT-7 and CTL-2 tests don't. Adding
  IDs (or pytest markers) would give a requirement coverage matrix, which is what the spec says the IDs are for.
