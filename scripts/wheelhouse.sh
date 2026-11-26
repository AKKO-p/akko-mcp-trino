#!/usr/bin/env bash
# The wheelhouse of the UBI 9 image: every Python package Dockerfile.ubi9 installs, read from the
# locks with their hashes, downloaded once by a machine with access to the package index, so that
# the image then builds with no network at all (docker build --network=none).
#
#   bash scripts/wheelhouse.sh
#
# It runs in the base image of the build stage (BUILD_BASE in Dockerfile.ubi9), so that pip picks
# the wheels of that Python and of RHEL 9's glibc, and fails on a pin that has none: on any other
# system it starts that image with docker (CONTAINER_ENGINE=podman for Podman) and runs itself
# there; WHEELHOUSE_RUN_OPTIONS adds options to that run (a memory limit, a proxy...). The
# wheelhouse is that of the machine's platform: linux/amd64 is the one built and checked.
#
# It writes into wheelhouse/, the folder Dockerfile.ubi9 mounts:
#   - the wheels of requirements/build.txt, the tools of the build stage;
#   - the wheels of requirements/image.txt, the dependencies of the image;
#   - the sdists of gssapi and krb5 and the other wheels of requirements/kerberos.txt, the
#     kerberos extra, which the build stage compiles against RHEL's MIT Kerberos.
# pip checks every file against the hashes of its lock as it downloads it, and the build checks
# them again as it installs them.
set -euo pipefail

cd "$(dirname "$0")/.."
out=wheelhouse

# The Python of the build stage: the name of its base image says it (python-312 is 3.12).
minor=$(sed -nE 's/^ARG BUILD_BASE=.*\/python-3([0-9]+)[:@].*/\1/p' Dockerfile.ubi9)
want="3.$minor"
# Inside that image, its stack id says so; anywhere else, a RHEL 9 host included, the script
# starts the image and runs itself there.
if [ "${CNB_STACK_ID:-}" != "com.redhat.stacks.ubi9-python-3$minor" ]; then
  base=$(sed -nE 's/^ARG BUILD_BASE=//p' Dockerfile.ubi9)
  # shellcheck disable=SC2086
  exec "${CONTAINER_ENGINE:-docker}" run --rm --user "$(id -u):0" -v "$PWD:/src:z" -w /src \
    ${WHEELHOUSE_RUN_OPTIONS:-} "$base" bash scripts/wheelhouse.sh
fi
have=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
if [ "$have" != "$want" ]; then
  echo "wheelhouse.sh: Python $have here, $want in Dockerfile.ubi9" >&2
  exit 1
fi

# A throwaway environment with the locked build tools: gssapi and krb5 have their metadata
# prepared without isolation, by the locked Cython, so that no build tool is fetched unlocked.
tools=$(mktemp -d)
trap 'rm -rf "$tools"' EXIT
python3 -m venv "$tools"
pip() { "$tools/bin/pip" --quiet --disable-pip-version-check --no-cache-dir "$@"; }
pip install --only-binary=:all: --require-hashes -r requirements/build.txt

mkdir -p "$out"
for lock in requirements/build.txt requirements/image.txt; do
  pip download --no-deps --only-binary=:all: --require-hashes -r "$lock" -d "$out"
done
pip download --no-deps --no-build-isolation --require-hashes -r requirements/kerberos.txt -d "$out"

echo "wheelhouse.sh: $out holds $(find "$out" -name '*.whl' | wc -l | tr -d ' ') wheels and" \
  "$(find "$out" -name '*.tar.gz' | wc -l | tr -d ' ') sdists for $(uname -m)"
