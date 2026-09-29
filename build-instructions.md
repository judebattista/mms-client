# ied-client — Build Instructions

This guide takes you step by step through building the offline installation package,
`ied-client_<version>-<revision>_amd64.deb`, checking it, and getting it onto rack laptops. The
package holds everything the tool needs, including its own Python, so the laptops never need an
internet connection. The **build** machine does need one.

Operators installing the package only need section 7. For what the package contains and why it is
built this way, see [docs/packaging.md](docs/packaging.md). The requirement it implements is
PLT-5 in [IED-CLIENT-SPEC.md](IED-CLIENT-SPEC.md).

---

## 1. The short version

On an x86_64 Linux machine with internet access, uv and a clean checkout:

```sh
uv sync && uv run python -m pytest -q        # the tests pass
packaging/build-deb.sh                       # → dist/ied-client_2.2.0-1_amd64.deb (+ .sha256)
```

For a release, let CI do it: push a tag that matches the version (section 6).

---

## 2. What you need on the build machine

| Need | Why | How to check / get it |
|---|---|---|
| Linux on **x86_64** | the package is amd64 only; the build downloads x86_64 Python and wheels | `uname -m` prints `x86_64` |
| Ubuntu, Debian, Kali, or WSL running one of them | any distribution with `dpkg-deb` works; its own Python and glibc version don't matter | `dpkg-deb --version` |
| `git` | the build records the commit and refuses uncommitted changes | `git --version` |
| `uv` | installs the pinned Python and wheels | `uv --version`; install with `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| internet access | uv downloads CPython 3.12.14, the wheels pinned in `uv.lock`, and the build backend | a machine that has built before may work from uv's cache |
| docker (optional) | only for the full check in section 5 | `docker version`; on WSL, turn on Docker Desktop's WSL integration for your distribution |

About 500 MB of free disk space is plenty. `build/` holds the work area and `dist/` the result;
git ignores both.

---

## 3. Before you build

1. **Get the source and the development environment:**

   ```sh
   git clone git@github.com:judebattista/mms-client.git ied-client
   cd ied-client
   uv sync
   ```

2. **Set the version, if this is a new release.** The version lives in one place:
   `version = "…"` in `pyproject.toml`. After changing it, run `uv lock` so `uv.lock` matches, and
   commit both files. `ied-client --version` reports this number, and so does the package name.

3. **Run the tests and the linter.** Everything must pass before you package it:

   ```sh
   uv run python -m pytest -q                      # unit + integration; the simulated IED starts by itself
   uv run ruff check src tests tools packaging
   ```

4. **Commit everything.** The build refuses a working tree with uncommitted changes or untracked
   files, because the package records the exact commit it was built from. `git status` should say
   `nothing to commit, working tree clean`.

---

## 4. Build the package

```sh
packaging/build-deb.sh
```

It prints seven steps and finishes with something like:

```
built dist/ied-client_2.2.0-1_amd64.deb (20M, installed size 95 MB)
      dist/ied-client_2.2.0-1_amd64.deb.sha256
install offline with:  sudo apt install ./ied-client_2.2.0-1_amd64.deb
```

It takes well under a minute when uv's cache is warm. Each run starts from a clean `build/deb/`
and overwrites the same file in `dist/`.

**Options:**

| Option | Use it for |
|---|---|
| `--revision N` | a rebuild of the same version, e.g. a packaging fix: `…_2.2.0-2_amd64.deb`. Default 1. |
| `--allow-dirty` | a **test** build from uncommitted changes. The package still works, but `--version` shows `commit …+local-changes`. Don't hand these out. |

**Environment variables (rarely needed):**
- `DEB_MAINTAINER="Name <email>"` sets the package's Maintainer field (default: the author of the
  last commit).
- `SOURCE_DATE_EPOCH` sets the file dates (default: the last commit's time).

**What the steps do:**
1. Build the project wheel and export the hash-pinned requirements from `uv.lock`.
2. Copy uv's CPython (the version in `packaging/python-version`) and trim what the tool never uses.
3. Install the pinned wheels into it, then check that the `mms` protocol module is registered and
   that libiec61850 loads. If this step fails, the package would not work, so the build stops.
4. Precompile all Python files. The install directory is read-only for users, and without this
   every start takes about 0.4 s longer.
5. Write the launcher, the build record, `/etc/ied-client/`, the documentation and the licence
   file.
6. Write the Debian control files.
7. Pack the result with `dpkg-deb` and write the checksum.

---

## 5. Check the package

### Quick checks (no docker)

```sh
dpkg-deb -I dist/ied-client_2.2.0-1_amd64.deb      # control data: version, dependencies, size
dpkg-deb -c dist/ied-client_2.2.0-1_amd64.deb | grep -E 'usr/bin|BUILD-INFO|etc/ied-client'
```

You can also run the packaged tool without installing it. The bundled Python works from any
directory:

```sh
mkdir -p /tmp/deb-check && dpkg-deb -x dist/ied-client_2.2.0-1_amd64.deb /tmp/deb-check
/tmp/deb-check/opt/ied-client/python/bin/python3 -I -c \
    'from ied_client.cli.main import main; main(["--version"])'
# ied-client 2.2.0 (package 2.2.0-1, commit …; Python 3.12.14)
# protocols: mms: pyiec61850-ng 1.6.1.10, libiec61850 1.6.1
```

### The full check (docker)

```sh
packaging/verify-deb.sh dist/ied-client_2.2.0-1_amd64.deb ubuntu:24.04
packaging/verify-deb.sh dist/ied-client_2.2.0-1_amd64.deb ubuntu:22.04
packaging/verify-deb.sh dist/ied-client_2.2.0-1_amd64.deb kalilinux/kali-rolling
```

For each image, the script starts a container with **networking switched off** (just like a rack
laptop) and:
1. installs the package with `apt`;
2. runs a few commands;
3. runs the whole test suite against the *installed* copy, as a normal user;
4. removes the package and checks that nothing is left behind.

It ends with `verify-deb: OK`. The first run per image downloads the image and pytest, which
needs network on the host. The container itself never has network.

---

## 6. Releasing through CI (recommended)

`.github/workflows/package.yml` does sections 3–5 on GitHub's machines for every tag that
starts with `v`:
1. lint and tests;
2. `build-deb.sh`;
3. `verify-deb.sh` on Ubuntu 22.04, Ubuntu 24.04 and Kali;
4. attaching the `.deb` and `.sha256` to the GitHub release for that tag.

```sh
# pyproject.toml says version = "2.2.0", uv.lock is updated, everything is committed and pushed
git tag v2.2.0
git push origin v2.2.0
```

- **The tag must match the version:** `v2.2.0` goes with `2.2.0`, or the build job stops.
- **Watching and downloading:** follow the run under the repository's *Actions* tab. When it is
  green, download both files from the *Releases* page.
- **Without a tag:** "Run workflow" on the *package* workflow builds and verifies without making
  a release. The `.deb` is then an artifact of that run.

---

## 7. Getting it onto the laptops

Copy **both** files, the `.deb` and its `.sha256`, to the laptop (a USB stick is fine). Then, on the
laptop, in the directory holding them:

```sh
sha256sum -c ied-client_2.2.0-1_amd64.deb.sha256    # must say: OK
sudo apt install ./ied-client_2.2.0-1_amd64.deb     # the ./ tells apt it is a file, not a download
ied-client --version
```

- **Upgrade:** a newer package installs the same way, over the old one.
- **Remove:** `sudo apt remove ied-client`.
- **Kept across both:** neither touches session logs, inventories, or the quirks and explanations
  operators have added themselves in `/etc/ied-client/` and `~/.config/ied-client/`.
- **After an upgrade:** run `ied-client local status`. It shows which of those local entries the
  new package has reviewed, and `ied-client local prune` tidies them away (see
  [docs/field-data.md](docs/field-data.md)).

The package works on Ubuntu 20.04 or later and on Kali. `apt` may offer `iproute2` and
`iputils-ping` as recommended packages. They are nearly always installed already, and the tool
works without them.

---

## 8. When the build fails

| Message | What to do |
|---|---|
| `the working tree has uncommitted changes` | commit (or stash) them. `git status` also lists untracked files, which count. For a throw-away test build, add `--allow-dirty`. |
| `the package is amd64 only; build it on an x86_64 machine` | build on an x86_64 machine, or push a tag and let CI build it. |
| `uv is required` / `dpkg-deb is required` / `git is required` | install the missing tool (section 2). |
| network errors from `uv` (steps 1–3) | the build machine needs internet access to PyPI, GitHub (for Python) and files.pythonhosted.org. Behind a proxy, set `HTTPS_PROXY`. |
| `Failed to spawn: pytest … Permission denied` when running the tests | the checkout was moved or renamed after `uv sync`, so the virtual environment's scripts point at the old path. Run `rm -rf .venv && uv sync`, or use `uv run python -m pytest` as shown above. |
| step 3 fails with an `AssertionError` about `eps`, or `pyiec61850-ng could not be imported` | the pinned wheel didn't install properly. Check that `uv.lock` is committed and unchanged (`uv lock --check`), then run the build again. |
| `verify-deb: docker is required` | install docker, or turn on Docker Desktop's WSL integration. Or rely on CI (section 6). |
| the test suite fails inside `verify-deb.sh` but passes with `uv run` | the installed package differs from the checkout. Usually a data file is missing from the wheel, so check `[tool.hatch.build.targets.wheel]` in `pyproject.toml`. The script output names the failing test. |

If something else goes wrong, the build keeps its work area in `build/deb/root/`. That is exactly
what goes into the package, so you can inspect it there.
