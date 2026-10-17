#!/usr/bin/env bash
# The live proof of the identity modes: builds the image from this repository
# (or uses MCP_IMAGE), starts a disposable Keycloak and Trino with three servers,
# runs check.py inside the network, checks the logs hold no token, prints the
# start time and the memory, and removes everything unless KEEP=1.
#
#   integration/run.sh
#   COMPOSE_PROJECT_NAME=my-run MCP_IMAGE=akko-mcp-trino:0.6.0rc1 integration/run.sh
set -uo pipefail
cd "$(dirname "$0")"
export COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-akko-mcp-trino-it}"
secret() { head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n'; }
export MCP_CLIENT_SECRET="$(secret)" INTEGRATION_PASSWORD="$(secret)"
export TRINO_SHARED_SECRET="$(secret)" KEYCLOAK_ADMIN_PASSWORD="$(secret)"
failed=0
cleanup() {
  if [ "${KEEP:-0}" = "1" ]; then echo "KEEP=1: project $COMPOSE_PROJECT_NAME left running"; return; fi
  docker compose --profile check down -v --remove-orphans >/dev/null 2>&1
  echo "== project $COMPOSE_PROJECT_NAME removed: $(docker ps -aq --filter "label=com.docker.compose.project=$COMPOSE_PROJECT_NAME" | wc -l | tr -d ' ') containers left"
}
trap cleanup EXIT

if [ -z "${MCP_IMAGE:-}" ]; then
  echo "== 0. Build the image from this repository"
  docker compose build --quiet mcp-jwt || exit 1
fi

echo "== 1. Start (project $COMPOSE_PROJECT_NAME)"
start=$(date +%s)
if ! docker compose up -d --no-build --wait --wait-timeout 420; then
  docker compose ps -a; docker compose logs --tail 60; exit 1
fi
echo "  all services healthy in $(( $(date +%s) - start )) s"
for s in mcp-jwt mcp-impersonation mcp-service; do
  c=$(docker compose ps -q "$s")
  started=$(docker inspect -f '{{.State.StartedAt}}' "$c")
  healthy=$(docker inspect -f '{{range .State.Health.Log}}{{if eq .ExitCode 0}}{{.End}}{{"\n"}}{{end}}{{end}}' "$c" | head -1)
  python3 - "$s" "$started" "$healthy" <<'EOF' 2>/dev/null || echo "  $s: started $started, ready $healthy"
import re, sys
from datetime import datetime, timedelta, timezone
def t(v):  # 2026-10-17T16:01:22.123456789Z, or 2026-10-17 18:01:25.1234 +0200 CEST
    m = re.match(r"(\d{4}-\d\d-\d\d)[T ](\d\d:\d\d:\d\d)(?:\.(\d+))?\s*(Z|[+-]\d{4})?", v.strip())
    day, clock, frac, off = m.groups()
    when = datetime.fromisoformat(f"{day}T{clock}.{(frac or '0')[:6]:0<6}")
    if off and off != "Z":
        sign = 1 if off[0] == "+" else -1
        when -= sign * timedelta(hours=int(off[1:3]), minutes=int(off[3:]))
    return when.replace(tzinfo=timezone.utc)
print(f"  {sys.argv[1]}: ready {(t(sys.argv[3]) - t(sys.argv[2])).total_seconds():.1f} s after its start (first passing /ready)")
EOF
done

echo "== 2. Memory at rest"
docker stats --no-stream --format '  {{.Name}} {{.MemUsage}}' $(docker compose ps -q) | sed "s/$COMPOSE_PROJECT_NAME-//"

echo "== 3. Checks"
docker compose --profile check run --rm --no-deps check || failed=1

echo "== 4. Memory after the checks"
docker stats --no-stream --format '  {{.Name}} {{.MemUsage}}' $(docker compose ps -q mcp-jwt mcp-impersonation mcp-service) | sed "s/$COMPOSE_PROJECT_NAME-//"

echo "== 5. Logs"
logs=$(docker compose logs mcp-jwt mcp-impersonation mcp-service)
if grep -q 'eyJ' <<<"$logs"; then echo "  FAIL  a token is in the server logs"; failed=1; else echo "  OK    no token in the server logs"; fi
if grep -q "$MCP_CLIENT_SECRET" <<<"$logs"; then echo "  FAIL  the client secret is in the logs"; failed=1; else echo "  OK    no secret in the server logs"; fi
grep -E "WARNING|serving" <<<"$logs" | sed 's/^/  /'
grep -c "token refused" <<<"$logs" | sed 's/^/  refusals logged by the servers: /'

[ $failed -eq 0 ] && echo "== INTEGRATION OK ==" || { echo "== INTEGRATION FAILED =="; exit 1; }
