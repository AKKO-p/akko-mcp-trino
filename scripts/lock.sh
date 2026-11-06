#!/usr/bin/env bash
# The locks of requirements/ (pip-compile, with hashes) and the versions they are made for: the
# one place CI and the release read the Python versions and the image platforms from. The Python
# of the image is the one of the Dockerfile's base image. Run in an environment installed from
# requirements/dev.txt (it brings pip-compile), on Python LOCK_PYTHON.
#
#   bash scripts/lock.sh compile   write requirements/*.txt from requirements/*.in
#   bash scripts/lock.sh check     compile again and fail if a lock changes (a stale lock)
#   bash scripts/lock.sh verify    download the image lock for each of IMAGE_PLATFORMS, hashes checked
#   bash scripts/lock.sh outputs   print the versions as key=value lines, for $GITHUB_OUTPUT
#
# To upgrade every pin: PIP_COMPILE_ARGS=--upgrade bash scripts/lock.sh compile
# One package: PIP_COMPILE_ARGS="--upgrade-package mcp" bash scripts/lock.sh compile
set -euo pipefail

cd "$(dirname "$0")/.."
# The Python of the image and of the locks: by default, the major and minor version of the base
# image of the Dockerfile (FROM python:3.12.16-slim@sha256:... gives 3.12). Not named
# PYTHON_VERSION, which the python images set to their full version.
if [ -z "${LOCK_PYTHON:-}" ]; then
  LOCK_PYTHON=$(sed -nE 's/^FROM python:([0-9]+\.[0-9]+)[.-].*/\1/p' Dockerfile | head -1)
  [ -n "$LOCK_PYTHON" ] || { echo "lock.sh: no python base image in the Dockerfile" >&2; exit 1; }
fi
# Every Python the checks run on; the locks install on each of them.
: "${CHECK_PYTHONS:=3.12 3.13}"
# The platforms of the published image.
: "${IMAGE_PLATFORMS:=linux/amd64,linux/arm64}"
# The locks, in order: dev.in is constrained by image.txt.
: "${LOCKS:=image dev}"
: "${PIP_COMPILE_ARGS:=}"

compile() {
  local running lock
  running=$(python -c 'import sys; print("%d.%d" % sys.version_info[:2])')
  if [ "$running" != "$LOCK_PYTHON" ]; then
    echo "lock.sh: the locks are compiled on Python $LOCK_PYTHON, not $running" >&2
    return 1
  fi
  for lock in $LOCKS; do
    # The package itself is not locked: it is installed from its source or its wheel, without
    # dependencies, after the lock.
    # shellcheck disable=SC2086
    pip-compile --quiet --generate-hashes --strip-extras --unsafe-package=akko-mcp-trino \
      $PIP_COMPILE_ARGS --output-file="requirements/$lock.txt" "requirements/$lock.in"
  done
}

verify() {
  local dest platform arch
  dest=$(mktemp -d)
  trap 'rm -rf "$dest"' RETURN
  for platform in ${IMAGE_PLATFORMS//,/ }; do
    case "$platform" in
      linux/amd64) arch=x86_64 ;;
      linux/arm64) arch=aarch64 ;;
      *) echo "lock.sh: no wheel tags known for $platform" >&2; return 1 ;;
    esac
    # Wheels only, every one matching a hash of the lock; manylinux_2_28 and later are the
    # glibc of the python:*-slim images.
    pip download --quiet --no-deps --only-binary=:all: --require-hashes \
      --python-version "$LOCK_PYTHON" --implementation cp \
      --platform "manylinux_2_17_$arch" --platform "manylinux2014_$arch" \
      --platform "manylinux_2_28_$arch" --platform "manylinux_2_34_$arch" \
      --dest "$dest/$arch" -r requirements/image.txt
    echo "lock.sh: requirements/image.txt resolves for $platform ($(ls "$dest/$arch" | wc -l | tr -d ' ') wheels)"
  done
}

case "${1:-}" in
  compile) compile ;;
  check)
    compile
    # Comment lines are left out: the header names the pip-compile options, which another
    # release of pip-tools (Dependabot's) may write otherwise.
    git diff --exit-code --ignore-matching-lines='^#' -- requirements/ \
      || { echo "lock.sh: a lock is stale; run: bash scripts/lock.sh compile" >&2; exit 1; } ;;
  verify) verify ;;
  outputs)
    printf 'python=%s\n' "$LOCK_PYTHON"
    printf 'pythons=["%s"]\n' "$(echo "$CHECK_PYTHONS" | sed 's/ /", "/g')"
    printf 'platforms=%s\n' "$IMAGE_PLATFORMS" ;;
  *) echo "usage: bash scripts/lock.sh compile|check|verify|outputs" >&2; exit 2 ;;
esac
