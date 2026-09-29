# ied-client

An IEC 61850 client for the IED test rack. It talks to devices through a pluggable protocol module;
the one that exists is MMS (IEC 61850-8-1). It lets an operator:

1. verify that the MMS server of an IED is **configured** correctly (against an SCD, a CID, a snapshot,
   or on its own);
2. verify that it **communicates** correctly with the clients it must serve (acting as that client);
3. read and write data attributes, edit setting groups, and operate controls (SPC, DPC, INC);
4. troubleshoot failing MMS communication, layer by layer, with explanations written for
   non-experts.

The specification is in two parts, indexed by [SPEC.md](SPEC.md): the protocol-independent IED client
([IED-CLIENT-SPEC.md](IED-CLIENT-SPEC.md)) and the MMS protocol module that plugs into it
([MMS-PROTOCOL-SPEC.md](MMS-PROTOCOL-SPEC.md)). Operators should read
[docs/safety-and-scope.md](docs/safety-and-scope.md) first: it explains the modes, the confirmations,
and what the tool cannot see (GOOSE and Sampled Values are not MMS).

The tool was called `mms-client` until 2026-09. During the transition the old command, the
`MMS_CLIENT_*` environment variables, session logs under `~/.local/state/mms-client/` and snapshots
written by `mms-client` all still work.

## Install (Ubuntu LTS or Kali)

**Operator machines, no internet needed (PLT-5).** One self-contained package: its own CPython and
every pinned dependency under `/opt/ied-client`, nothing downloaded at install or run time.

```sh
sha256sum -c ied-client_2.2.0-1_amd64.deb.sha256    # optional: the copy on the USB stick is intact
sudo apt install ./ied-client_2.2.0-1_amd64.deb
ied-client --version                               # ied-client 2.2.0 (package 2.2.0-1, commit …; Python 3.12.14)
```

The package comes from the GitHub release of each `v*` tag, or from `packaging/build-deb.sh` on
any Linux machine with internet access ([docs/packaging.md](docs/packaging.md)). Upgrades install
the same way; `sudo apt remove ied-client` removes it and leaves logs and local data alone.

**Development checkout:**

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh     # once
git clone <this repo> && cd ied-client
uv sync                                            # CPython 3.12 + pinned pyiec61850-ng, never the system Python
uv run ied-client --help
```

The Python version is pinned (`.python-version`); uv downloads it if needed (PLT-2). The examples
below write `uv run ied-client`; on an installed machine it is just `ied-client`.

## Quick start

```sh
# an inventory describes the rack for one experiment (see "Inventory" below)
export IED_CLIENT_INVENTORY=experiments/feeder-trip.yaml

uv run ied-client diagnose relay-F12                  # layered connectivity diagnosis with a verdict
uv run ied-client shell relay-F12                     # interactive shell on one association
uv run ied-client read relay-F12 PROT/PTOC1.Str.general --json
uv run ied-client check relay-F12 --reference scd/rack.scd
uv run ied-client snapshot relay-F12 --out snapshots/relay-F12.json
uv run ied-client diff relay-F12 snapshots/relay-F12.json
uv run ied-client explain object-access-denied
```

In the shell the model is navigable like a filesystem (`ls`, `cd /CTRL/CSWI1`, `tree`, `describe
Pos`), references tab-complete, and the prompt shows the mode, the device, the location and the orCat
(`[EXPERT]` in red when expert mode is on):

```
[std] relay-F12:/CTRL/CSWI1 [orCat=remote]> operate Pos close
```

Every command is also available one-shot (`ied-client <command> <device> …`), and every command can
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
| Local data | `local status`, `local edit hints\|quirks`, `local add-quirk <device>`, `local export`, `local import`, `local prune` |

## Inventory

One YAML file per experiment (full schema in `src/ied_client/inventory.py`):

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

`ied-client inventory from-scd rack.scd` drafts one from an SCD; `discover 10.0.0.0/24` drafts one from
the network. `--as sm1` makes the tool behave like that client (its source IP, its RCBs); the tool
checks that the address exists on this machine and prints the exact `ip addr add` command if not.
A device may name its protocol module (`protocol: mms`, the default; `--protocol` for a bare IP);
`ied-client --version` lists the modules installed.

## Local data on offline machines

What an operator learns on a laptop that never reaches git — a model that accepts four
associations, an explanation that is wrong for this rack — is recorded on that laptop and used at
once, marked `[local]` (spec §13.1, FLD-1 … FLD-6):

```sh
ied-client local add-quirk relay-F12      # matched on the identity the device reports, with its evidence
ied-client local edit hints               # explanation entries, validated before saving
ied-client local export --out /media/usb/ # one bundle file to carry back
```

In the repository, `uv run python tools/ingest_field_data.py <bundle>` reviews the bundle entry by
entry into the data files, and records each decision in a ledger that ships with the next
package; after upgrading, `local status` shows the laptop what was taken and `local prune` tidies
up. See [docs/field-data.md](docs/field-data.md).

## Session log, restore, incidents

Each session writes a JSONL log (default `~/.local/state/ied-client/sessions/`, override with
`--log-dir` or `IED_CLIENT_LOG_DIR`). `restore` writes back every value written in the session, newest
first, after confirmation — controls are never replayed (one-shot: `restore <device> --last` or
`--from LOG`). `export --incident FILE` saves a
self-contained failure record (device, inventory, identity, last diagnose/check results, log
excerpt); please keep them, they are how the explanation catalogue and the quirks file learn about
this rack.

## Development

```sh
uv run pytest                      # unit + integration tests (the simulated IED starts automatically)
uv run pytest -m soak -s           # memory soak (RSK-5)
uv run ruff check src tests tools packaging
packaging/build-deb.sh             # the offline .deb (clean tree; --allow-dirty for a test build)
packaging/verify-deb.sh dist/ied-client_*_amd64.deb ubuntu:24.04   # install + tests, network off (docker)
uv run python -m tests.sim --scl tests/fixtures/scl/bcu_ed2.cid --port 10102   # a simulated IED to play with
```

* [docs/architecture.md](docs/architecture.md) — layers, the protocol-module contract, threading, why the
  adapter uses ctypes, how to add a protocol module.
* [docs/spikes.md](docs/spikes.md) — Phase 0 results (RSK-1 … RSK-6).
* [docs/packaging.md](docs/packaging.md) — the offline `.deb`: what is in it, building, verifying, releasing.
* [docs/field-data.md](docs/field-data.md) — local data layers, the field bundle, ingest and the ledger.
* `src/ied_client/` — the protocol-independent client (CLI, core, verification, diagnosis, SCL).
* `src/mms_protocol/` — the MMS protocol module, found through the `ied_client.protocols` entry point.
* `src/ied_client/data/hints*`, `glossary*` and `src/mms_protocol/data/hints/` — the explanation
  catalogue (editable YAML).
* `src/mms_protocol/data/quirks.yaml` — known vendor/model/firmware quirks (starts empty).
* `src/ied_client/data/field-ledger.yaml` — review decisions on field bundles (FLD-6).
* `packaging/` — offline package build and verification; `tools/ingest_field_data.py` — field bundle review.

## Licence

libiec61850 is dual-licensed GPLv3 / commercial and pyiec61850-ng is GPLv3; this project is therefore
GPLv3 for internal use. Distribution outside the organisation needs review (MMS-PROTOCOL-SPEC §1).
