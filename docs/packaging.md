# The offline package (PLT-5)

Operator machines — rack laptops and workstations that may never be online — install ied-client
from one file:

```sh
sudo apt install ./ied-client_2.2.0-1_amd64.deb
```

Nothing is downloaded, at install time or later. The development workflow (`uv sync`, `uv run`)
is unchanged and is still how the tool is worked on; the package is built from the same
`uv.lock`.

## What is in the package

```
/opt/ied-client/python/            CPython 3.12.14 (python-build-standalone, the build uv manages)
  lib/python3.12/site-packages/    ied_client, mms_protocol, pyiec61850-ng (+ its libiec61850), rich,
                                   prompt_toolkit, PyYAML, … exactly as pinned in uv.lock
/opt/ied-client/bin/ied-client     launcher: #!/opt/ied-client/python/bin/python3 -I
/opt/ied-client/bin/mms-client     the old name (symlink)
/opt/ied-client/BUILD-INFO.json    version, git commit, source date, Python version, pinned wheels
/usr/bin/ied-client, mms-client    symlinks to the launchers
/etc/ied-client/                   machine-wide local data layer (FLD-1): hints/, quirks/mms/, incidents/
/usr/share/doc/ied-client/         README, basic and advanced instructions, safety-and-scope, field-data,
                                   copyright (every bundled licence)
```

* **Private Python.** The interpreter is the relocatable python-build-standalone build that uv
  installs for development, so the package runs exactly the Python the tests ran on (PLT-2). It
  needs nothing from the system but glibc; the system Python is never used. Tk, IDLE, the test
  suite, pip, headers and the shared `libpython` are trimmed (the interpreter is statically
  linked).
* **Isolated launcher.** `-I` means `PYTHONPATH`, the user's site-packages and the current
  directory cannot change what runs.
* **Precompiled.** Every module is compiled to bytecode at build time with unchecked-hash
  invalidation: `/opt` is read-only for users, and dpkg clamps file times, so bytecode written
  any other way would be recompiled in memory on every start (measured: 0.15 s against 0.56 s
  for `--version`).
* **Dependencies.** `Depends: libc6 (>= 2.28), libstdc++6, libgcc-s1` — present on every Ubuntu
  20.04+ and Kali system, so `apt install ./…deb` never needs the network to resolve them
  (glibc 2.28 is what the pyiec61850-ng wheel requires). `Recommends: iproute2, iputils-ping`:
  without `ip` the tool falls back to `/proc` for local addresses and routes, and without `ping`
  the ICMP probe reports "undecided".
* **What upgrades leave alone.** Session logs and shell history (`~/.local/state/ied-client/`),
  inventories, and both local data layers: files users add under `/etc/ied-client/` and
  `~/.config/ied-client/` are never touched by the package (only `/etc/ied-client/README` is a
  conffile).

`ied-client --version` shows which build is installed:

```
ied-client 2.2.0 (package 2.2.0-1, commit f26a5f1064fe (2026-09-28); Python 3.12.14)
protocols: mms: pyiec61850-ng 1.6.1.10, libiec61850 1.6.1
```

## Building

Step by step, with prerequisites and troubleshooting: [build-instructions.md](../build-instructions.md).

On any x86_64 Linux machine with `uv`, `git` and `dpkg-deb` (Ubuntu, Debian, Kali, WSL) and
internet access, or a uv cache that already holds the pinned wheels and Python:

```sh
packaging/build-deb.sh              # refuses a dirty working tree
packaging/build-deb.sh --allow-dirty   # a test build; BUILD-INFO.json records "+local-changes"
```

Output: `dist/ied-client_<version>-<revision>_amd64.deb` and its `.sha256`. The version comes
from `pyproject.toml` (the single source; `ied_client.__version__` reads it from the installed
metadata), the revision from `--revision N` (default 1), the Python patch release from
`packaging/python-version`. File dates are clamped to the commit date (`SOURCE_DATE_EPOCH`).

Steps, in `packaging/build-deb.sh`:

1. `uv build --wheel` for the project; `uv export --frozen --no-dev` for the hash-pinned
   requirements.
2. `uv python install <version>`, copy that interpreter into the staging tree, trim it.
3. `uv pip install --require-hashes --no-deps` into it; check that the `mms` entry point is
   registered and the bundled libiec61850 loads.
4. Compile the bytecode.
5. Launchers, `BUILD-INFO.json` and the copyright file (`packaging/deb_helpers.py`), the
   `/etc/ied-client/` directories, the documentation.
6. `DEBIAN/control`; 7. `dpkg-deb --root-owner-group -Zxz`.

The build takes well under a minute with a warm cache; the package is about 20 MB (95 MB
installed).

## Verifying

```sh
packaging/verify-deb.sh dist/ied-client_2.2.0-1_amd64.deb ubuntu:24.04
```

Needs docker (on WSL: Docker Desktop with WSL integration turned on for the distribution) and
uv. It builds a test image from the given base image plus the package's Recommends, then, in a
container with **`--network none`**:

1. installs the package with `apt`;
2. checks `ied-client --version` (the package build and libiec61850 1.6.1), `mms-client`,
   `explain`, and that `ied_client` is imported from `/opt/ied-client`;
3. runs this repository's test suite, as an unprivileged user, against the **installed** copy
   (pytest comes from a directory prepared on the host; the simulated IED runs on loopback);
4. runs `local status`;
5. removes the package and checks nothing is left in `/opt`.

A quicker check without docker: `dpkg-deb -x` the package into a scratch directory and run
`<dir>/opt/ied-client/python/bin/python3 -I -c 'from ied_client.cli.main import main; main(["--version"])'`
— the interpreter is relocatable, so this exercises everything but the launcher.

## Releasing

`.github/workflows/package.yml` runs on every `v*` tag (and on demand): lint and the test suite
in the development install, `build-deb.sh`, `verify-deb.sh` on `ubuntu:22.04`, `ubuntu:24.04` and
`kalilinux/kali-rolling`, and — for a tag — attaches the `.deb` and `.sha256` to the GitHub
release. The tag must match the `pyproject.toml` version.

1. Bump `version` in `pyproject.toml` (and `uv lock`), commit.
2. `git tag v2.2.0 && git push --tags`.
3. Copy the release's `.deb` and `.sha256` to the laptops.

## Scope and limits

* amd64 only. arm64 would need the same build run on an arm64 machine (the wheels and Python
  exist for aarch64); `build-deb.sh` refuses other architectures rather than produce a broken
  package.
* The package is for use inside the organisation: it bundles GPLv3 code (libiec61850,
  pyiec61850-ng), and distribution outside needs review (MMS-PROTOCOL-SPEC.md §1). The copyright
  file lists every bundled component and its licence.
