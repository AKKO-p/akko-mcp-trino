#!/usr/bin/env bash
# The live proof of the identity modes and of the catalogue context: builds the image from
# this repository (or uses MCP_IMAGE), starts a disposable Keycloak and Trino with the servers,
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
began=$(date +%s)
SERVERS="mcp-jwt mcp-paged mcp-impersonation mcp-service mcp-context-jwt mcp-context-impersonation mcp-context-canary mcp-modules mcp-scoped"
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
for s in $SERVERS; do
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
# Trino's anonymous memory (heap and native, never the page cache), sampled every second.
samples=$(mktemp)
trino=$(docker compose ps -q trino)
( while :; do docker exec "$trino" grep '^anon ' /sys/fs/cgroup/memory.stat 2>/dev/null; sleep 1; done ) >"$samples" &
sampler=$!
docker compose --profile check run --rm --no-deps check || failed=1
kill "$sampler" 2>/dev/null; wait "$sampler" 2>/dev/null

echo "== 4. Memory after the checks"
docker stats --no-stream --format '  {{.Name}} {{.MemUsage}}' $(docker compose ps -q $SERVERS) | sed "s/$COMPOSE_PROJECT_NAME-//"

# The cgroup's own record of the most memory Trino used since it started (cgroup v2).
peak=$(docker compose exec -T trino cat /sys/fs/cgroup/memory.peak 2>/dev/null)
limit=$(docker compose exec -T trino cat /sys/fs/cgroup/memory.max 2>/dev/null)
# memory.max reads "max" for a container without a memory limit.
case "$limit" in ''|*[!0-9]*) of="no limit" ;; *) of="$(( limit / 1048576 )) MiB" ;; esac
[ -n "$peak" ] && echo "  trino peak, page cache included: $(( peak / 1048576 )) MiB of $of"
anon=$(awk '{print $2}' "$samples" | sort -n | tail -1); count=$(wc -l <"$samples" | tr -d ' ')
rm -f "$samples"
[ -n "$anon" ] && echo "  trino anonymous memory at most $(( anon / 1048576 )) MiB during the checks ($count samples)"

echo "== 5. Logs"
logs=$(docker compose logs $SERVERS)
if grep -q 'eyJ' <<<"$logs"; then echo "  FAIL  a token is in the server logs"; failed=1; else echo "  OK    no token in the server logs"; fi
if grep -q "$MCP_CLIENT_SECRET" <<<"$logs"; then echo "  FAIL  the client secret is in the logs"; failed=1; else echo "  OK    no secret in the server logs"; fi
grep -E "WARNING|serving" <<<"$logs" | sed 's/^/  /'
grep -c "token refused" <<<"$logs" | sed 's/^/  refusals logged by the servers: /'
# The server whose canary alice sees serves her no context and says so, never naming her.
canary=$(docker compose logs mcp-context-canary | grep "the context canary is listed to a caller")
if [ -n "$canary" ] && ! grep -qE "alice|bob" <<<"$canary"; then
  echo "  OK    the canary seen by a caller is logged as a WARNING, without the caller's name"
else
  echo "  FAIL  the canary seen by a caller is not logged, or the line names the caller"; failed=1
fi
# fingerprint-audit: the line of alice's read of the nations on mcp-modules carries the SHA-256
# of that statement and her token id, never the statement itself.
nations_sha=$(printf '%s' "SELECT name FROM tpch.tiny.nation ORDER BY nationkey" | sha256sum | cut -d' ' -f1)
fingerprint=$(docker compose logs mcp-modules | grep '"tool":"execute_query"' | grep "\"sql_sha256\":\"$nations_sha\"" | head -1)
if [ -n "$fingerprint" ] && grep -q '"token_id":"[^"]' <<<"$fingerprint" && ! grep -q "tpch.tiny.nation ORDER BY" <<<"$fingerprint"; then
  echo "  OK    fingerprint-audit: the line carries the statement's SHA-256 and the token id, never the statement"
else
  echo "  FAIL  fingerprint-audit: no line with the statement's SHA-256 and a token id"; failed=1
fi
echo "  the run took $(( $(date +%s) - began )) s"

[ $failed -eq 0 ] && echo "== INTEGRATION OK ==" || { echo "== INTEGRATION FAILED =="; exit 1; }
