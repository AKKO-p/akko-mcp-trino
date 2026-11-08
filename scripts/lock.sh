#!/usr/bin/env bash
# The locks (pip-compile, with hashes) and the versions they are made for: the one place CI and
# the release read the Python versions and the image platforms from. The Python of the image is
# the one of the Dockerfile's base image. Run in an environment installed from
# requirements/build.txt and requirements/dev.txt (they bring pip-compile), on Python LOCK_PYTHON.
#
#   bash scripts/lock.sh compile        write each lock (.txt) from its source (.in)
#   bash scripts/lock.sh check          compile again, hashes included, and fail if a lock changes
#   bash scripts/lock.sh verify         download the image lock for each of IMAGE_PLATFORMS
#   bash scripts/lock.sh outputs        print the versions as key=value lines, for $GITHUB_OUTPUT
#   bash scripts/lock.sh pins NAME...   print NAME==version of each, from requirements/dev.txt
#   bash scripts/lock.sh tags PLATFORM  print the --platform options of the wheels for PLATFORM
#
# To upgrade every pin: PIP_COMPILE_ARGS=--upgrade bash scripts/lock.sh compile
# One package: PIP_COMPILE_ARGS="--upgrade-package mcp" bash scripts/lock.sh compile
set -euo pipefail

cd "$(dirname "$0")/.."
# The Python of the image and of the locks: by default, the major and minor version of the base
# image on the Dockerfile's FROM line. Not named PYTHON_VERSION, which the python images set to
# their full version.
if [ -z "${LOCK_PYTHON:-}" ]; then
  LOCK_PYTHON=$(sed -nE 's/^FROM python:([0-9]+\.[0-9]+)[.-].*/\1/p' Dockerfile | head -1)
  [ -n "$LOCK_PYTHON" ] || { echo "lock.sh: no python base image in the Dockerfile" >&2; exit 1; }
fi
# Every Python the checks run on; the locks install on each of them.
: "${CHECK_PYTHONS:=3.12 3.13}"
# The platforms of the published image.
: "${IMAGE_PLATFORMS:=linux/amd64,linux/arm64}"
# The glibc of the base image (CI checks it on the image it builds): the newest manylinux wheels
# the image installs.
: "${IMAGE_GLIBC:=2.41}"
# The sources, in the order they are compiled: a lock is compiled after the locks it reads.
: "${LOCKS:=requirements/build.in requirements/image.in requirements/release.in requirements/dev.in requirements/kerberos.in}"
: "${PIP_COMPILE_ARGS:=}"

# The pip-compile options of a source, the ones Dependabot reads back from the lock's header. A
# source that installs the packages from their directories (-e, itself or through -r) leaves the
# packages of the repository out of its lock (the core and each provider of providers/), and pip
# and setuptools too, which Dependabot would otherwise compile with --allow-unsafe, and that fails
# on a package's directory; such a lock installs with build.txt, which pins setuptools. Any other
# source pins them (--allow-unsafe).
# Whether a source installs the package from its directory, itself or through its -r lines.
editable() {
  local child
  grep -q '^-e ' "$1" && return 0
  for child in $(sed -nE 's/^-r[[:space:]]*//p' "$1"); do
    editable "$(dirname "$1")/$child" && return 0
  done
  return 1
}

options() {
  if editable "$1"; then
    local pyproject unsafe=""
    for pyproject in pyproject.toml providers/*/pyproject.toml; do
      unsafe+="--unsafe-package=$(sed -nE 's/^name = "([^"]+)"/\1/p' "$pyproject" | head -1) "
    done
    echo "${unsafe}--unsafe-package=pip --unsafe-package=setuptools"
  else
    echo "--allow-unsafe"
  fi
}

compile() {
  local running source
  running=$(python -c 'import sys; print("%d.%d" % sys.version_info[:2])')
  if [ "$running" != "$LOCK_PYTHON" ]; then
    echo "lock.sh: the locks are compiled on Python $LOCK_PYTHON, not $running" >&2
    return 1
  fi
  for source in $LOCKS; do
    # shellcheck disable=SC2046,SC2086
    pip-compile --quiet --generate-hashes --strip-extras $(options "$source") \
      $PIP_COMPILE_ARGS --output-file="${source%.in}.txt" "$source"
  done
}

# The one mapping from a platform of the image to the tags of the wheels it installs: its CPU,
# and every manylinux glibc up to IMAGE_GLIBC, with the legacy names of 2.5, 2.12 and 2.17.
tags() {
  local arch minor
  case "$1" in
    linux/amd64) arch=x86_64 ;;
    linux/arm64) arch=aarch64 ;;
    *) echo "lock.sh: no wheel tags known for $1" >&2; return 1 ;;
  esac
  for minor in $(seq 5 "${IMAGE_GLIBC#2.}"); do printf -- '--platform manylinux_2_%s_%s ' "$minor" "$arch"; done
  printf -- '--platform manylinux1_%s --platform manylinux2010_%s --platform manylinux2014_%s\n' "$arch" "$arch" "$arch"
}

verify() {
  local dest platform
  dest=$(mktemp -d)
  trap 'rm -rf "$dest"' RETURN
  for platform in ${IMAGE_PLATFORMS//,/ }; do
    # Wheels only, every one matching a hash of the lock.
    # shellcheck disable=SC2046
    pip download --quiet --no-deps --only-binary=:all: --require-hashes \
      --python-version "$LOCK_PYTHON" --implementation cp $(tags "$platform") \
      --dest "$dest/${platform//\//-}" -r requirements/image.txt
    echo "lock.sh: requirements/image.txt resolves for $platform ($(ls "$dest/${platform//\//-}" | wc -l | tr -d ' ') wheels)"
  done
}

pins() {
  local name pin
  for name in "$@"; do
    pin=$(grep -oiE "^${name}==[^ ]+" requirements/dev.txt) \
      || { echo "lock.sh: $name is not in requirements/dev.txt" >&2; return 1; }
    echo "$pin"
  done
}

case "${1:-}" in
  compile) compile ;;
  check)
    # Hashes are computed again, not reused, so that a hash changed by hand shows.
    PIP_COMPILE_ARGS="$PIP_COMPILE_ARGS --no-reuse-hashes" compile
    locks=$(for source in $LOCKS; do echo "${source%.in}.txt"; done)
    # Comment lines are left out: the header names the pip-compile options, which another
    # release of pip-tools (Dependabot's) may write otherwise.
    # shellcheck disable=SC2086
    git diff --exit-code --ignore-matching-lines='^#' -- $locks \
      || { echo "lock.sh: a lock is stale; run: bash scripts/lock.sh compile" >&2; exit 1; }
    # shellcheck disable=SC2086
    untracked=$(git ls-files --others --exclude-standard -- $locks)
    [ -z "$untracked" ] || { echo "lock.sh: a lock is not committed: $untracked" >&2; exit 1; } ;;
  verify) verify ;;
  outputs)
    printf 'python=%s\n' "$LOCK_PYTHON"
    printf 'pythons=["%s"]\n' "$(echo "$CHECK_PYTHONS" | sed 's/ /", "/g')"
    printf 'platforms=%s\n' "$IMAGE_PLATFORMS"
    printf 'glibc=%s\n' "$IMAGE_GLIBC" ;;
  pins) shift; pins "$@" ;;
  tags) tags "${2:?usage: bash scripts/lock.sh tags PLATFORM}" ;;
  *) echo "usage: bash scripts/lock.sh compile|check|verify|outputs|pins|tags" >&2; exit 2 ;;
esac
