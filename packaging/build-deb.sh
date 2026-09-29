#!/usr/bin/env bash
# Build the offline Debian package dist/ied-client_<version>-<revision>_amd64.deb (PLT-5).
#
# The package carries its own CPython (python-build-standalone, the build uv manages) and every
# wheel pinned in uv.lock, installed under /opt/ied-client, so installing it needs no network:
#     sudo apt install ./ied-client_<version>-<revision>_amd64.deb
# Building it needs uv, git and dpkg-deb, and internet access (or a warm uv cache). See
# docs/packaging.md.
#
# usage: packaging/build-deb.sh [--allow-dirty] [--revision N]
set -euo pipefail

usage() { sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; }
die() { echo "build-deb: $*" >&2; exit 1; }
step() { echo "==> $*"; }

ALLOW_DIRTY=0
DEB_REVISION="${DEB_REVISION:-1}"
while [ $# -gt 0 ]; do
    case "$1" in
        --allow-dirty) ALLOW_DIRTY=1 ;;
        --revision) [ $# -ge 2 ] || die "--revision needs a value"; DEB_REVISION="$2"; shift ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
    shift
done

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
for tool in uv git dpkg-deb sha256sum; do
    command -v "$tool" >/dev/null || die "$tool is required"
done
[ "$(uname -m)" = x86_64 ] || die "the package is amd64 only; build it on an x86_64 machine"

ARCH=amd64
PREFIX=/opt/ied-client
PY_VERSION="$(tr -d '[:space:]' < packaging/python-version)"
PY_MINOR="${PY_VERSION%.*}"
VERSION="$(sed -n 's/^version = "\(.*\)"$/\1/p' pyproject.toml | head -n 1)"
[ -n "$VERSION" ] || die "cannot read the version from pyproject.toml"
PKG_VERSION="${VERSION}-${DEB_REVISION}"

DIRTY=0
if [ -n "$(git status --porcelain)" ]; then
    DIRTY=1
    [ "$ALLOW_DIRTY" = 1 ] || die "the working tree has uncommitted changes (commit them, or use --allow-dirty for a test build)"
    echo "build-deb: warning: building from a dirty tree; BUILD-INFO.json records it" >&2
fi
COMMIT="$(git rev-parse HEAD)"
export SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-$(git log -1 --format=%ct)}"
SOURCE_DATE="$(date -u -d "@$SOURCE_DATE_EPOCH" +%Y-%m-%d)"
MAINTAINER="${DEB_MAINTAINER:-$(git log -1 --format='%an <%ae>')}"

WORK="$ROOT/build/deb"
STAGE="$WORK/root"
PYDIR="$STAGE$PREFIX/python"
LIB="$PYDIR/lib/python$PY_MINOR"
SITE="$LIB/site-packages"
rm -rf "$WORK"
mkdir -p "$WORK" "$STAGE$PREFIX"
umask 022

step "1/7 project wheel and pinned requirements (uv.lock)"
uv build --wheel --out-dir "$WORK/wheel" --quiet
WHEEL="$(ls "$WORK"/wheel/ied_client-"$VERSION"-*.whl)"
uv export --frozen --no-dev --no-emit-project --format requirements-txt --quiet \
    --output-file "$WORK/requirements.txt"

step "2/7 CPython $PY_VERSION (python-build-standalone via uv)"
uv python install --quiet "$PY_VERSION"
PYEXE="$(readlink -f "$(uv python find --managed-python "$PY_VERSION")")"
cp -a "$(dirname "$(dirname "$PYEXE")")" "$PYDIR"
# Trim what the tool never uses: Tk, IDLE, the test suite, pip, headers, man pages, and the shared
# libpython (the interpreter is statically linked; only embedding applications need it).
rm -rf "$LIB"/{test,idlelib,tkinter,turtledemo,ensurepip,lib2to3} "$LIB"/turtle.py \
       "$LIB"/lib-dynload/_tkinter*.so "$SITE"/pip "$SITE"/pip-*.dist-info \
       "$PYDIR"/lib/{tcl*,tk*,itcl*,thread*,libtcl*,libtk*,libpython*,pkgconfig} \
       "$PYDIR"/include "$PYDIR"/share/man "$PYDIR"/BUILD \
       "$PYDIR"/bin/{2to3*,idle3*,pip*,pydoc3*,python3*-config}
STAGED_PY="$PYDIR/bin/python$PY_MINOR"
"$STAGED_PY" -I -c "import sys; assert sys.version.split()[0] == '$PY_VERSION', sys.version"

step "3/7 install the pinned wheels into the bundled Python"
BIN_BEFORE="$(ls "$PYDIR/bin")"
uv pip install --quiet --python "$STAGED_PY" --break-system-packages --link-mode copy \
    --no-deps --require-hashes -r "$WORK/requirements.txt"
uv pip install --quiet --python "$STAGED_PY" --break-system-packages --link-mode copy --no-deps "$WHEEL"
# Console scripts written by the installer point at the staging directory: the package has its own launcher.
for f in "$PYDIR"/bin/*; do
    grep -qxF "$(basename "$f")" <<<"$BIN_BEFORE" || rm -f "$f"
done
rm -f "$SITE"/ied_client-*.dist-info/direct_url.json
"$STAGED_PY" -I -c "
import importlib.metadata as m
eps = {e.name for e in m.entry_points(group='ied_client.protocols')}
assert 'mms' in eps, eps
import ied_client.cli.main, mms_protocol.adapter._native as n
n.library()  # the bundled libiec61850 loads
"

step "4/7 bytecode (unchecked-hash: /opt is read-only for users, and mtimes are clamped)"
find "$PYDIR" -name __pycache__ -type d -prune -exec rm -rf {} +
"$STAGED_PY" -I -m compileall -q -j0 --invalidation-mode unchecked-hash -s "$STAGE" -p / "$LIB" >/dev/null

step "5/7 launcher, build record, local-data directories, documentation"
mkdir -p "$STAGE$PREFIX/bin" "$STAGE/usr/bin"
cat > "$STAGE$PREFIX/bin/ied-client" <<EOF
#!$PREFIX/python/bin/python3 -I
# ied-client launcher (packaging/build-deb.sh). -I (isolated mode): PYTHONPATH, the user's
# site-packages and the current directory cannot change what runs.
import sys

from ied_client.cli.main import main

sys.exit(main())
EOF
chmod 755 "$STAGE$PREFIX/bin/ied-client"
ln -s ied-client "$STAGE$PREFIX/bin/mms-client"  # the old name; main() notices it from argv[0]
ln -s "$PREFIX/bin/ied-client" "$STAGE/usr/bin/ied-client"
ln -s "$PREFIX/bin/mms-client" "$STAGE/usr/bin/mms-client"
"$STAGED_PY" -I packaging/deb_helpers.py build-info --out "$STAGE$PREFIX/BUILD-INFO.json" \
    --version "$VERSION" --package-version "$PKG_VERSION" --commit "$COMMIT" --dirty "$DIRTY" \
    --source-date "$SOURCE_DATE" --python-version "$PY_VERSION" --arch "$ARCH" \
    --requirements "$WORK/requirements.txt" --wheel "$WHEEL"
# Machine-wide local data layer (FLD-1); the files users add there are theirs, not the package's.
mkdir -p "$STAGE/etc/ied-client/hints" "$STAGE/etc/ied-client/quirks/mms" "$STAGE/etc/ied-client/incidents"
install -m 644 packaging/etc-README "$STAGE/etc/ied-client/README"
DOC="$STAGE/usr/share/doc/ied-client"
mkdir -p "$DOC"
install -m 644 README.md basic-instructions.md advanced-instructions.md \
    docs/safety-and-scope.md docs/field-data.md "$DOC/"
"$STAGED_PY" -I packaging/deb_helpers.py copyright --out "$DOC/copyright" \
    --site-packages "$SITE" --python-prefix "$PYDIR"

step "6/7 control files"
chmod -R u=rwX,go=rX "$STAGE"
INSTALLED_SIZE="$(du -sk "$STAGE" | cut -f1)"
mkdir -p "$STAGE/DEBIAN"
cat > "$STAGE/DEBIAN/control" <<EOF
Package: ied-client
Version: $PKG_VERSION
Architecture: $ARCH
Maintainer: $MAINTAINER
Installed-Size: $INSTALLED_SIZE
Depends: libc6 (>= 2.28), libstdc++6, libgcc-s1
Recommends: iproute2, iputils-ping
Section: net
Priority: optional
Description: IEC 61850 IED client for verifying and troubleshooting IEDs in a test rack
 Checks that an IED's server is configured and communicating correctly, reads and
 writes values, operates controls and diagnoses failing associations layer by layer,
 with explanations written for non-experts. The protocol module is MMS
 (IEC 61850-8-1, libiec61850 through pyiec61850-ng).
 .
 Self-contained: a private CPython $PY_VERSION and all Python dependencies are installed
 under $PREFIX, so installing needs no network and the system Python is never used.
EOF
printf '/etc/ied-client/README\n' > "$STAGE/DEBIAN/conffiles"
chmod 644 "$STAGE/DEBIAN/control" "$STAGE/DEBIAN/conffiles"

step "7/7 dpkg-deb"
mkdir -p dist
OUT="dist/ied-client_${PKG_VERSION}_${ARCH}.deb"
dpkg-deb --root-owner-group -Zxz --build "$STAGE" "$OUT" >/dev/null
(cd dist && sha256sum "$(basename "$OUT")" > "$(basename "$OUT").sha256")
echo
echo "built $OUT ($(du -h "$OUT" | cut -f1), installed size $((INSTALLED_SIZE / 1024)) MB)"
echo "      $OUT.sha256"
echo "install offline with:  sudo apt install ./$(basename "$OUT")"
