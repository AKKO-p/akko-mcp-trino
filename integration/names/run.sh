#!/usr/bin/env bash
# The live proof that names are quoted, never filtered: starts a disposable Trino with an
# Iceberg catalog and a memory catalog, creates tables whose names the server once refused
# (an Iceberg metadata table such as orders$snapshots, accented and mixed-case names, a
# schema that starts with a digit, a name with a double quote), runs check.py through the
# tools against it, then removes everything. Needs Docker only.
#
#   integration/names/run.sh
#   TRINO_IMAGE=trinodb/trino:484 PYTHON_IMAGE=python:3.12-slim integration/names/run.sh
set -uo pipefail
cd "$(dirname "$0")/../.."
name="${NAMES_PROJECT:-mcp-names-$$}"
trino_image="${TRINO_IMAGE:-trinodb/trino:484}"
python_image="${PYTHON_IMAGE:-python:3.13-slim}"
data="$(mktemp -d)"
chmod 777 "$data"
cleanup() {
  docker rm -f "$name-trino" "$name-check" >/dev/null 2>&1
  docker network rm "$name" >/dev/null 2>&1
  # Trino's files belong to its own user: removed from a container, then the folder.
  docker run --rm -v "$data:/data" "$python_image" rm -rf /data/warehouse >/dev/null 2>&1
  rm -rf "$data"
}
trap cleanup EXIT

docker network create "$name" >/dev/null || exit 1
docker run -d --name "$name-trino" --network "$name" -m 3g \
  -v "$PWD/integration/names/catalog/lake.properties:/etc/trino/catalog/lake.properties:ro" \
  -v "$PWD/integration/names/catalog/mem.properties:/etc/trino/catalog/mem.properties:ro" \
  -v "$data:/data" "$trino_image" >/dev/null || exit 1
for _ in $(seq 1 120); do
  docker logs "$name-trino" 2>&1 | grep -q "SERVER STARTED" && break
  [ "$(docker inspect -f '{{.State.Running}}' "$name-trino")" = true ] || break
  sleep 2
done
if ! docker logs "$name-trino" 2>&1 | grep -q "SERVER STARTED"; then
  docker logs --tail 40 "$name-trino"; exit 1
fi

docker run --rm --name "$name-check" --network "$name" -v "$PWD:/src:ro" "$python_image" bash -c '
  set -e
  cp -r /src /w && cd /w
  pip install -q --require-hashes -r requirements/build.txt -r requirements/image.txt >/dev/null 2>&1
  pip install -q --no-deps --no-build-isolation . >/dev/null 2>&1
  cd / && python -I /w/integration/names/check.py "$0"
' "$name-trino"
