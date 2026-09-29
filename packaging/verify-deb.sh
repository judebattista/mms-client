#!/usr/bin/env bash
# Install the package in a clean container with networking off, smoke-test it, run the repository's
# test suite against the installed copy, and check that removing it leaves nothing in /opt (PLT-5).
#
# usage: packaging/verify-deb.sh DEB IMAGE
#   e.g. packaging/verify-deb.sh dist/ied-client_2.2.0-1_amd64.deb ubuntu:24.04
#   images used by CI: ubuntu:22.04, ubuntu:24.04, kalilinux/kali-rolling
# Needs docker and uv on the host (network is used only to build the test image and fetch pytest).
set -euo pipefail

die() { echo "verify-deb: $*" >&2; exit 1; }
[ $# -eq 2 ] || { sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//' >&2; exit 2; }
DEB="$(readlink -f "$1")"
IMAGE="$2"
[ -f "$DEB" ] || die "no such file: $1"
command -v docker >/dev/null || die "docker is required"
command -v uv >/dev/null || die "uv is required"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
chmod 755 "$WORK"

echo "==> test dependencies (pytest), fetched on the host: the container has no network"
uv pip install --quiet --target "$WORK/testdeps" --python-version 3.12 \
    --python-platform x86_64-manylinux_2_28 pytest pytest-timeout
cp "$DEB" "$WORK/"

echo "==> test image: $IMAGE plus the package's Recommends"
TAG="ied-client-verify:$(printf '%s' "$IMAGE" | tr '/:' '--')"
docker build --quiet --tag "$TAG" - >/dev/null <<EOF
FROM $IMAGE
RUN apt-get update \\
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends iproute2 iputils-ping \\
 && rm -rf /var/lib/apt/lists/* \\
 && useradd --create-home tester
EOF

echo "==> offline install, smoke tests, test suite, removal ($IMAGE, --network none)"
docker run --rm --network none -v "$ROOT:/src:ro" -v "$WORK:/verify:ro" "$TAG" bash -euo pipefail -c '
    export DEBIAN_FRONTEND=noninteractive
    apt-get install -y --no-install-recommends /verify/*.deb
    ied-client --version | tee /dev/stderr | grep -q "libiec61850 1.6.1"
    ied-client --version | grep -q "(package "
    mms-client --version >/dev/null
    ied-client explain object-access-denied >/dev/null
    /opt/ied-client/python/bin/python3 -I -c "import ied_client; assert ied_client.__file__.startswith(\"/opt/ied-client/\"), ied_client.__file__"
    test -d /etc/ied-client/hints -a -d /etc/ied-client/quirks/mms

    mkdir /home/tester/work
    cp -r /src/pyproject.toml /src/src /src/tests /src/tools /home/tester/work/
    chown -R tester: /home/tester/work
    runuser -u tester -- env HOME=/home/tester PYTHONPATH=/verify/testdeps \
        sh -c "cd /home/tester/work && /opt/ied-client/python/bin/python3 -m pytest -q -p no:cacheprovider"
    runuser -u tester -- env HOME=/home/tester ied-client local status >/dev/null

    apt-get remove -y ied-client >/dev/null
    if [ -e /opt/ied-client ]; then echo "left behind:"; find /opt/ied-client; exit 1; fi
    echo "verify-deb: OK"
'
