# mms-client — Basic Instructions

This is a step-by-step guide for someone using `mms-client` for the first time. It assumes
no programming background. It does **not** assume you know IEC 61850 in depth, but it will
explain the handful of terms you can't avoid.

If you want the full technical specification, it's in [SPEC.md](SPEC.md). If you want the
safety rules explained in more depth, read [docs/safety-and-scope.md](docs/safety-and-scope.md).
This guide is the "get productive today" version of both.

---

## 1. What this tool is for, and what it can't do

`mms-client` talks to IEDs (Intelligent Electronic Devices — the relays, breaker controllers
and similar boxes in the test rack) using a protocol called **MMS**, which is how IEC 61850
devices exchange configuration, measurements and control commands over the network.

With it you can:

- Check that a device is talking correctly.
- Browse what's inside a device (its data model) the way you'd browse folders on a computer.
- Read values, write values, and operate controls (open/close a breaker, etc.).
- Compare a device's live configuration against a reference file, or against a snapshot you
  took earlier.
- Get a plain-English diagnosis when something isn't working, instead of a raw error code.

**What it cannot do:** it cannot see GOOSE or Sampled Values traffic. Those are separate
IEC 61850 mechanisms that don't go over MMS, and this tool is an MMS client only. If two
devices talk to each other over GOOSE (common for fast bay-level interlocking between a BCU
and a breaker), this tool is blind to that conversation even if every MMS check it runs
passes. If you need to see GOOSE, you need a packet capture tool (e.g. Wireshark) instead.

It also cannot watch traffic between two *other* devices — it can only act as a client itself,
and only report on the association it holds.

---

## 2. Before you start: three words you'll see everywhere

- **Device / IED.** The physical box you're talking to (a relay, a Station Manager, etc.).
- **Association.** The live connection between this tool and one device. Devices only allow a
  limited number of these at a time, and the tool doesn't know that limit in advance — so
  don't open more associations than you need, and close them (`disconnect`, or leave the
  shell) when you're done.
- **Object reference.** The address of a piece of data inside a device, written like
  `CTRL/CSWI1.Pos.stVal`. Read it left to right: logical device (`CTRL`) / logical node
  (`CSWI1`) . data object (`Pos`) . data attribute (`stVal`). You'll rarely have to type a
  full reference from memory — `ls` and `tree` show you what exists, and you can `cd` into a
  device the way you'd `cd` into a folder.

---

## 3. Installing it

Only Ubuntu (LTS) and Kali Linux are supported — not Windows, not macOS.

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh     # installs "uv", a Python tool manager (once only)
git clone <this repo> && cd mms-client
uv sync                                             # downloads the exact Python version and packages needed
uv run mms-client --help                            # should print the command list
```

Don't try to run `mms-client` with your system's own Python — `uv run` (or the `.venv` it
creates) is what makes sure you get the exact tested versions of everything. If `uv run
mms-client --help` prints a list of commands, the install worked.

From here on, every command in this guide should be typed as `uv run mms-client ...` unless
you've activated the virtual environment yourself.

---

## 4. Set up an inventory file (do this once per experiment)

Every time the rack is wired up for an experiment, the devices and their IP addresses are
written down in one YAML file called the **inventory**. The tool needs this to know which
device names refer to which IP addresses.

Create a file, e.g. `experiments/my-test.yaml`:

```yaml
schema_version: 1
experiment: my-test
safety: lab                          # "lab" = simple y/N confirmations; "strict" = you must type the object name (default)
devices:
  - name: relay-F12
    ip: 10.0.0.12
    role: bcu                        # station-manager | bcu | ied | gateway | other
```

Then tell the tool where to find it, for the rest of your terminal session:

```sh
export MMS_CLIENT_INVENTORY=experiments/my-test.yaml
```

(Add that line to your shell so you don't have to retype it, or pass `-i experiments/my-test.yaml`
on every command instead.)

You now refer to the device as `relay-F12` in every command instead of typing its IP address
each time (though a bare IP address always works too, e.g. for a quick one-off check).

**Don't want to write this file by hand?** If you have an SCD file for the substation (an
XML configuration export), the tool can draft the inventory for you:

```sh
uv run mms-client inventory from-scd scd/rack.scd --out experiments/my-test.yaml
```

Or if you just know the subnet the rack is on, ask the tool to find devices on the network:

```sh
uv run mms-client discover 10.0.0.0/24 --out experiments/my-test.yaml
```

Either way, check the result — `uv run mms-client inventory validate` will test every device
in the file against the network and tell you if anything doesn't match.

### The `safety` setting

- `strict` (the default): before sending a control, a select, or an RCB takeover, you must
  **type the object's name** to confirm. This is the safer choice whenever a control operates
  something physical.
- `lab`: a plain `y/N` question is enough.

Set this per experiment based on how much you trust what's connected. `--yes` on the command
line can skip a **write** confirmation, but it can never skip a control or takeover
confirmation — that always needs a human to actually respond.

---

## 5. The two modes: standard and expert

`mms-client` starts in **standard** mode. Add `--expert` (or type `set mode expert` inside
the shell) to switch to **expert** mode. The current mode is always shown at the start of the
shell prompt, e.g. `[std]` or `[EXPERT]` (shown in red).

| In standard mode you can... | Expert mode additionally allows... |
|---|---|
| browse, read, watch, check, snapshot, diff, diagnose | (same) |
| write the normally-writable settings (SP, SV, CF, DC, EX) | write attributes that normally can't be written (ST, MX), for deliberately testing what the device does |
| operate controls, with interlock/synchrocheck checks forced on | turn interlock/synchrocheck checks off (this is logged) |
| pick orCat 1, 2 or 3 (bay/station/remote) for a control | pick orCat 0 or 4–8 too |
| subscribe to a report block nobody else is using | take over a report block another client is using |

**Important:** these modes are a guardrail against typos and slips, not a security feature.
There are no passwords. Anyone who can run the tool can add `--expert`. Treat the rack network
the way you'd treat any lab network you're responsible for.

---

## 6. Your first session: a walkthrough

Everything below works either as a **one-shot command** (`mms-client <command> <device> ...`,
run once from your normal terminal) or inside the **interactive shell**
(`mms-client shell <device>`), which keeps one connection open so you don't reconnect for
every command. Start with one-shot commands while you're learning; move to the shell once
you're doing several things on the same device (repeated connect/disconnect uses up the
device's limited connection slots and can itself cause the kind of fault you're trying to
diagnose).

### Step 1 — check the device is even reachable

```sh
uv run mms-client diagnose relay-F12
```

This runs a series of checks, in order, and stops at the **first one that fails**: can you
ping it → can you open a raw network connection → does the MMS protocol handshake succeed →
can you read its identity → does its data model look sane → do reports work. At the end you
get a **verdict** (in plain English, with how certain the tool is) and suggested next steps.
This is the command to reach for first whenever something "isn't working" — it tells you
which layer to look at instead of you guessing.

### Step 2 — see who it says it is

```sh
uv run mms-client info relay-F12
```

Shows the vendor, model and firmware from every source the device offers (there can be more
than one, and they don't always agree — the tool shows you the disagreement rather than
picking one silently) and the association parameters (max message size, etc.).

### Step 3 — look inside the device

```sh
uv run mms-client ls relay-F12                     # top level: logical devices
uv run mms-client ls relay-F12 /CTRL                # inside one logical device
uv run mms-client tree relay-F12 /CTRL/CSWI1        # everything under one logical node, as a tree
uv run mms-client describe relay-F12 CTRL/CSWI1.Pos # what this object means and whether it's writable
```

`describe` is worth using liberally — it turns a cryptic object reference into a plain-English
explanation of what the data represents and whether/how you're allowed to change it.

### Step 4 — read a value

```sh
uv run mms-client read relay-F12 CTRL/CSWI1.Pos.stVal
```

Shows the value, its type, its functional constraint (FC — see below), and its quality and
timestamp, if the device provides them.

### Step 5 (optional) — open the interactive shell instead

```sh
uv run mms-client shell relay-F12
```

Inside the shell the model works like a filesystem:

```
[std] relay-F12:/> cd CTRL/CSWI1
[std] relay-F12:/CTRL/CSWI1> ls
[std] relay-F12:/CTRL/CSWI1> tree
[std] relay-F12:/CTRL/CSWI1> describe Pos
[std] relay-F12:/CTRL/CSWI1> read Pos.stVal
```

References tab-complete, so you rarely need to remember or type a reference in full. `cd ..`
goes up a level, `cd /` goes to the root, `cd -` goes back to where you just were. Leave the
shell with `exit` or `quit` — this releases the association and cleans up anything the tool
changed (see §9).

---

## 7. Functional constraints (FC) — the two-letter codes you'll see everywhere

Every attribute belongs to a category called a functional constraint, shown in brackets, e.g.
`[ST]`. You don't need to memorise all of them, but these come up constantly:

| FC | Meaning | Normally writable? |
|---|---|---|
| **ST** | Status — the device's own live status (breaker position, alarms) | No — it's telling you, not the other way round |
| **MX** | Measured values (currents, voltages) | No |
| **CF** | Configuration | Usually yes |
| **SP** | Setting, unprotected group | Usually yes |
| **SG / SE** | Setting group values (the group actually in use / the group you're editing) | Only via `setgroup`, not plain `write` |
| **CO** | Control | Never via `write` — use `operate` / `select` / `cancel` instead |
| **DC** | Description (free text) | Usually yes |

If you try to `write` something in CO, the tool refuses and tells you to use `operate`
instead. If you try to write ST or MX in standard mode, it refuses; in expert mode it's
allowed (with a warning) purely so you can test how the device reacts to an attempted write —
this is not something you'd normally do.

---

## 8. Reading, writing, and operating controls

### Reading and watching

```sh
uv run mms-client read relay-F12 PROT/PTOC1.Str.general
uv run mms-client watch relay-F12 CTRL/CSWI1.Pos.stVal --interval 2   # poll every 2s, Ctrl-C to stop
```

### Writing a setting

```sh
uv run mms-client write relay-F12 CTRL/GGIO1.Setp1.setVal 12
```

The tool figures out the correct data type from the device's own model — you don't tell it
whether something is an integer or a float, it already knows. It shows you the current value
and the new value and asks you to confirm before sending anything, then reads the value back
afterwards to make sure the device actually applied it (if it accepted the write but the value
didn't change, the tool reports that as a failure, because that's a real and useful thing to
know).

Setting groups (SG/SE) can't be written with plain `write` — use `setgroup` instead:

```sh
uv run mms-client setgroup relay-F12 show
uv run mms-client setgroup relay-F12 edit 2 PROT/PTOC1.StrVal.setMag.f 1.2
uv run mms-client setgroup relay-F12 activate 2
```

### Operating a control (e.g. closing a breaker)

**Read this whole section before you run your first `operate` on real hardware.** A control
can move a physical contact.

```sh
uv run mms-client operate relay-F12 CTRL/CSWI1.Pos close --orcat station
```

Before it sends anything, the tool shows a **pre-flight summary**: the object, the control
model (whether it's direct or select-then-operate, and whether the device confirms
completion), current vs. target value, the origin it will send, the interlock/synchrocheck
check flags, and the current `Loc`/`LocSta`/`LocKey` values (these tell you whether the device
is even in a state that will accept a remote command — if it looks like the command will be
blocked, the tool warns you about that *before* sending it, not after).

Then, depending on your inventory's `safety` setting, you either type the object's name
(`strict`) or answer `y/N` (`lab`) to actually send it. `--yes` will **never** skip this step
for a control, even though it can skip it for an ordinary write — this is deliberate.

The very first control you send in a session will also ask you to choose an **origin
category** (orCat): bay-control, station-control, or remote-control. This isn't guessed for
you — see §10 of the spec if you want the reasoning, but in short: pick whichever category
matches who is really issuing the command. You can change it later with `set orcat <value>`
in the shell, or `--orcat` on a one-shot command.

`select` and `cancel` exist separately if you specifically want to test select/cancel
behaviour (e.g. does the device correctly time out a selection you never operate).

`authority-probe` lets you check, without ever actually operating anything, which origin
categories the device will accept a Select from — useful when you're not sure what a device
has been configured to allow.

---

## 9. Reports (RCBs)

A **report control block (RCB)** is how a device pushes changes to a client instead of the
client having to keep asking. `rcb` lists them and shows who (if anyone) currently owns each
one:

```sh
uv run mms-client rcb relay-F12
```

`subscribe` turns one on and streams reports to your terminal until you press Ctrl-C:

```sh
uv run mms-client subscribe relay-F12 urcbEvents
```

If the RCB you want is already in use by someone else, `subscribe` won't just fail — it lists
every instance and who holds each one, so you can pick a free one or (in expert mode, with
`--takeover` and a typed confirmation naming who loses it) take over an in-use one. Only do
that if you understand you'll be disrupting whatever else is currently receiving those
reports.

Whatever the tool enables, reserves, or reconfigures while you're subscribed, it puts back the
way it found it — automatically, on exit, on Ctrl-C, on a lost connection, or a process kill.
If something genuinely can't be undone (e.g. a reservation timer that must simply expire), it
tells you exactly what's left and for how long.

---

## 10. Verifying configuration: check / snapshot / diff

### `check` — compare against a reference, or just sanity-check the device on its own

```sh
uv run mms-client check relay-F12                                    # self-consistency checks only
uv run mms-client check relay-F12 --reference scd/rack.scd --ied F12 # also compares against an SCD
```

Without a reference file, `check` still runs useful checks that don't need one: do all the
report blocks point at datasets that actually exist, does every dataset member actually
resolve, is anything reserved by a client that isn't even in your inventory, and so on.

Results always come back in **two separate categories that are never merged into one
pass/fail**:

- **Configuration** — does the device match its reference?
- **Communication** — can a real client actually get what it needs from this device?

A device only "passes" if both categories pass. Keeping them separate is deliberate: a device
can be configured perfectly and still fail to serve a real client (e.g. its report blocks are
all already taken), or vice versa.

### `snapshot` / `diff` — build your own reference

If you don't have an SCD handy, or you just want a "known good" baseline to come back to
later, take a snapshot once the rack is working correctly:

```sh
uv run mms-client snapshot relay-F12 --out snapshots/relay-F12-good.json
```

Later, compare the live device against it:

```sh
uv run mms-client diff relay-F12 snapshots/relay-F12-good.json
```

or compare two snapshots taken at different times against each other. Snapshots never write
anything to the device — taking one is completely safe to do at any time. Structural changes
(something added or removed from the model) are reported as a failure; configuration value
changes as a warning; and routine operational-state changes (like a breaker's current
position) as informational only, so the diff isn't swamped with noise.

---

## 11. When something goes wrong: explanations

Whenever a command fails, the tool shows you a short hint automatically (unless you passed
`--terse`). If you want more detail:

```sh
uv run mms-client explain last                         # explain whatever just failed
uv run mms-client explain object-access-denied          # explain a specific error code
uv run mms-client explain ctlModel                      # explain a term (LN class, CDC, FC, ...)
```

Hints always say how confident they are — "likely cause" vs. a plain statement of fact — the
tool won't claim to know something it's only guessing at.

If you hit something that seems worth remembering for next time (a device behaving oddly, an
intermittent failure), save it:

```sh
uv run mms-client export relay-F12 --incident incidents/2026-09-25-f12.json --note "reports stop after ~10 minutes"
```

This bundles the device, the relevant results, and a log excerpt into one file. Please keep
these — they're how the hint catalogue and the "known quirks" file (used automatically by
future diagnoses of the same device model) actually grow. There's currently no other record of
rack failures.

---

## 12. Session log and undoing writes

Every session writes a log automatically (JSONL, one line per event) to
`~/.local/state/mms-client/sessions/` by default. You don't need to do anything for this to
happen.

```sh
uv run mms-client log                    # show this session's log path and recent entries
```

If you wrote several values during a session and want to put them back the way they were:

```sh
uv run mms-client restore relay-F12 --last
```

This replays every write from the most recent earlier session on that device, newest value
first, and asks you to confirm. **Controls are never replayed by `restore`** — if you closed a
breaker, `restore` will not "undo" that by opening it again; that would be a new decision, not
an undo, and the tool won't make it for you.

---

## 13. Quick reference

```sh
export MMS_CLIENT_INVENTORY=experiments/my-test.yaml

mms-client diagnose <device>                       # start here when something's wrong
mms-client info <device>
mms-client ls / tree / cd / describe <device> [ref]
mms-client read <device> <ref>
mms-client write <device> <ref> <value>
mms-client watch <device> <ref> --interval N
mms-client setgroup <device> show|activate|edit ...
mms-client operate <device> <ref> <value> --orcat N
mms-client select / cancel <device> <ref>
mms-client authority-probe <device> [ref]
mms-client rcb <device>
mms-client subscribe <device> <rcb>
mms-client gi <device>
mms-client check <device> [--reference FILE]
mms-client snapshot <device> --out FILE
mms-client diff <device> <snapshot>
mms-client explain last | <code> | <term>
mms-client log
mms-client restore <device> --last
mms-client export <device> --incident FILE --note "..."
mms-client shell <device>                          # interactive; commands above minus "<device>"

# every command also takes --json (machine-readable output) and --terse (no hints)
```

For anything not covered here: `mms-client help` lists every command, and
`mms-client help <command>` shows that command's exact options with examples — it's always
accurate, because it's generated from the same code that runs the command.

---

## 14. If you get stuck

1. `mms-client diagnose <device>` — almost always the right first step.
2. `mms-client explain last` — for whatever the last error meant.
3. `mms-client help <command>` — for the exact syntax of one command.
4. [docs/safety-and-scope.md](docs/safety-and-scope.md) — what the tool protects you from, and
   what it explicitly does not (it is not a security tool, and it cannot see GOOSE/SV traffic).
5. Save an incident file (`export --incident`) before you give up on a hard problem — future
   you (or a colleague) will thank you.
