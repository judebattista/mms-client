# mms-client

An IEC 61850 MMS client for the IED test rack. It lets an operator:

1. verify that the MMS server of an IED is **configured** correctly (against an SCD, a CID, a snapshot,
   or on its own);
2. verify that it **communicates** correctly with the clients it must serve (acting as that client);
3. read and write data attributes, edit setting groups, and operate controls (SPC, DPC, INC);
4. troubleshoot failing MMS communication, layer by layer, with explanations written for
   non-experts.

The specification is [SPEC.md](SPEC.md). Operators should read
[docs/safety-and-scope.md](docs/safety-and-scope.md) first: it explains the modes, the confirmations,
and what the tool cannot see (GOOSE and Sampled Values are not MMS).

## Install (Ubuntu LTS or Kali)

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh     # once
git clone <this repo> && cd mms-client
uv sync                                            # CPython 3.12 + pinned pyiec61850-ng, never the system Python
uv run mms-client --help
```

The Python version is pinned (`.python-version`); uv downloads it if needed (PLT-2).

## Quick start

```sh
# an inventory describes the rack for one experiment (see "Inventory" below)
export MMS_CLIENT_INVENTORY=experiments/feeder-trip.yaml

uv run mms-client diagnose relay-F12                  # layered connectivity diagnosis with a verdict
uv run mms-client shell relay-F12                     # interactive shell on one association
uv run mms-client read relay-F12 PROT/PTOC1.Str.general --json
uv run mms-client check relay-F12 --reference scd/rack.scd
uv run mms-client snapshot relay-F12 --out snapshots/relay-F12.json
uv run mms-client diff relay-F12 snapshots/relay-F12.json
uv run mms-client explain object-access-denied
```

In the shell the model is navigable like a filesystem (`ls`, `cd /CTRL/CSWI1`, `tree`, `describe
Pos`), references tab-complete, and the prompt shows the mode, the device, the location and the orCat
(`[EXPERT]` in red when expert mode is on):

```
[std] relay-F12:/CTRL/CSWI1 [orCat=remote]> operate Pos close
```

Every command is also available one-shot (`mms-client <command> <device> …`), and every command can
print versioned JSON (`--json`). Friendly text is always accompanied by the exact object reference, FC
and raw error code; `--terse` drops the hints.

## Commands

| Area | Commands |
|---|---|
| Connection | `connect`, `disconnect`, `info` (identity from every source, with source and confidence) |
| Diagnosis | `diagnose` (network → transport/session → MMS initiate → identity → model → reports → controls), `discover <subnet>` |
| Model | `ls`, `cd`, `tree`, `describe`, `dataset` |
| Files | `files ls`, `files get` (read-only: no upload, no delete) |
| Data | `read`, `write`, `watch`, `setgroup show / activate / edit` |
| Controls | `operate`, `select`, `cancel`, `authority-probe`, `origin` |
| Reports | `rcb`, `subscribe`, `gi` |
| Verification | `check`, `snapshot`, `diff` |
| Help | `explain <code or term>`, `explain last` |
| Session | `set mode/orcat/terse`, `log`, `restore`, `export --incident FILE` |
| Inventory | `inventory from-scd FILE`, `inventory validate` |

## Inventory

One YAML file per experiment (full schema in `src/mms_client/inventory.py`):

```yaml
schema_version: 1
experiment: feeder-trip
safety: strict                    # strict: type the object name to confirm controls; lab: y/N
devices:
  - {name: relay-F12, ip: 10.0.0.12, role: bcu, reference: {scd: ../scd/rack.scd, ied: F12}}
  - {name: sm1, ip: 10.0.0.2, role: station-manager}
clients:
  - {name: sm1-f12, client: sm1, server: relay-F12, source_ip: 10.0.0.5, rcbs: [CTRL/LLN0.BR.brcbA01]}
```

`mms-client inventory from-scd rack.scd` drafts one from an SCD; `discover 10.0.0.0/24` drafts one from
the network. `--as sm1` makes the tool behave like that client (its source IP, its RCBs); the tool
checks that the address exists on this machine and prints the exact `ip addr add` command if not.

## Session log, restore, incidents

Each session writes a JSONL log (default `~/.local/state/mms-client/sessions/`, override with
`--log-dir` or `MMS_CLIENT_LOG_DIR`). `restore` writes back every value written in the session, newest
first, after confirmation — controls are never replayed (one-shot: `restore <device> --last` or
`--from LOG`). `export --incident FILE` saves a
self-contained failure record (device, inventory, identity, last diagnose/check results, log
excerpt); please keep them, they are how the explanation catalogue and the quirks file learn about
this rack.

## Development

```sh
uv run pytest                      # unit + integration tests (the simulated IED starts automatically)
uv run pytest -m soak -s           # memory soak (RSK-5)
uv run ruff check src tests
uv run python -m tests.sim --scl tests/fixtures/scl/bcu_ed2.cid --port 10102   # a simulated IED to play with
```

* [docs/architecture.md](docs/architecture.md) — layers, threading, why the adapter uses ctypes.
* [docs/spikes.md](docs/spikes.md) — Phase 0 results (RSK-1 … RSK-6).
* `src/mms_client/data/hints.yaml` and `glossary*` — the explanation catalogue (editable YAML).
* `src/mms_client/data/quirks.yaml` — known vendor/model/firmware quirks (starts empty).

## Licence

libiec61850 is dual-licensed GPLv3 / commercial and pyiec61850-ng is GPLv3; this project is therefore
GPLv3 for internal use. Distribution outside the organisation needs review (SPEC §3).
