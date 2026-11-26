#!/usr/bin/env bash
# The live proof of the ldap scenario (compose.yaml): builds the UBI 9 image with no network
# (or uses MCP_IMAGE), starts the directory, Keycloak, Trino and the server, runs check.py
# inside the network, checks that the server's log holds no token and no password, and
# removes everything unless KEEP=1. Needs Docker with Compose, and the package index once,
# for scripts/wheelhouse.sh, when wheelhouse/ is absent.
#
#   integration/ldap/run.sh
#   COMPOSE_PROJECT_NAME=my-run MCP_IMAGE=registry.example.internal/akko-mcp-trino:ubi9 integration/ldap/run.sh
set -uo pipefail
cd "$(dirname "$0")"
export COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-akko-mcp-trino-ldap}"
secret() { head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n'; }
export LDAP_ADMIN_PASSWORD="$(secret)" LDAP_JWT_SECRET="$(secret)" LDAP_KEY_SEED="$(secret)"
export MCP_SERVER_PASSWORD="$(secret)" ALICE_PASSWORD="$(secret)" BOB_PASSWORD="$(secret)"
export TRINO_SHARED_SECRET="$(secret)" KEYCLOAK_ADMIN_PASSWORD="$(secret)"
failed=0
began=$(date +%s)
# Trino's configuration: integration/trino, with this scenario's files over it.
TRINO_ETC="$(mktemp -d)"
export TRINO_ETC
cp -R ../trino/. "$TRINO_ETC/" && cp trino/* "$TRINO_ETC/" && chmod -R a+rX "$TRINO_ETC"
cleanup() {
  if [ "${KEEP:-0}" = "1" ]; then echo "KEEP=1: project $COMPOSE_PROJECT_NAME left running"; return; fi
  docker compose --profile check down -v --remove-orphans >/dev/null 2>&1
  rm -rf "$TRINO_ETC"
  echo "== project $COMPOSE_PROJECT_NAME removed: $(docker ps -aq --filter "label=com.docker.compose.project=$COMPOSE_PROJECT_NAME" | wc -l | tr -d ' ') containers left"
}
trap cleanup EXIT

if [ -z "${MCP_IMAGE:-}" ]; then
  echo "== 0. Build the UBI 9 image with no network"
  [ -d ../../wheelhouse ] || bash ../../scripts/wheelhouse.sh || exit 1
  docker build --quiet --network=none -f ../../Dockerfile.ubi9 -t akko-mcp-trino:ubi9 ../.. || exit 1
fi

echo "== 1. Start (project $COMPOSE_PROJECT_NAME)"
start=$(date +%s)
if ! docker compose up -d --wait --wait-timeout 420; then
  docker compose ps -a; docker compose logs --tail 60; exit 1
fi
echo "  all services healthy in $(( $(date +%s) - start )) s"
docker compose logs ldap-seed | sed -n 's/^[^|]*| /  /p'
mcp=$(docker compose ps -q mcp)
echo "  the server runs as $(docker exec "$mcp" id -u):$(docker exec "$mcp" id -g)," \
  "root file system read-only: $(docker inspect -f '{{.HostConfig.ReadonlyRootfs}}' "$mcp")," \
  "image $(docker inspect -f '{{.Config.Image}}' "$mcp")"

echo "== 2. Memory at rest"
docker stats --no-stream --format '  {{.Name}} {{.MemUsage}}' $(docker compose ps -q) | sed "s/$COMPOSE_PROJECT_NAME-//"

echo "== 3. Checks"
docker compose --profile check run --rm --no-deps check || failed=1

echo "== 4. The server's log"
logs=$(docker compose logs mcp)
if grep -q 'eyJ' <<<"$logs"; then echo "  FAIL  a token is in the server's log"; failed=1; else echo "  OK    no token in the server's log"; fi
leaked=0
for password in "$MCP_SERVER_PASSWORD" "$ALICE_PASSWORD" "$BOB_PASSWORD" "$LDAP_ADMIN_PASSWORD"; do
  grep -qF "$password" <<<"$logs" && leaked=1
done
if [ $leaked -eq 0 ]; then echo "  OK    no password in the server's log"; else echo "  FAIL  a password is in the server's log"; failed=1; fi
grep -E "WARNING|serving" <<<"$logs" | sed 's/^/  /'
echo "  the run took $(( $(date +%s) - began )) s"

[ $failed -eq 0 ] && echo "== LDAP SCENARIO OK ==" || { echo "== LDAP SCENARIO FAILED =="; exit 1; }
