#!/usr/bin/env bash
# The chart's render refusals, with helm only (no cluster): each inconsistent or open
# setting must fail `helm template` with the message that names it, and the consistent
# ones must render and lint. Run by CI and by run.sh.
#
#   integration/helm/refusals.sh
set -uo pipefail
cd "$(dirname "$0")"
CHART=../../charts/akko-mcp-trino
failed=0
pass() { echo "PASS $*"; }
fail() { echo "FAIL $*"; failed=1; }
lint="$(mktemp)"
trap 'rm -f "$lint"' EXIT

refuses() {  # refuses <what> <expected message> <helm template arguments...>
  local what="$1" expected="$2"; shift 2
  local out; out="$(helm template t "$CHART" "$@" 2>&1)"
  if [ $? -ne 0 ] && grep -qF -- "$expected" <<<"$out"; then pass "$what: $(grep -m1 -E 'execution error|^- at' <<<"$out" | sed 's/.*execution error at ([^)]*): //')"
  else fail "$what: rendered, or refused for another reason: $(grep -m1 -E 'Error' <<<"$out")"; fi
}
refuses "the defaults alone" "trino.host is required"
refuses "the door off without acknowledgeNoAuth" "auth.acknowledgeNoAuth=true" -f values.yaml --set auth.enabled=false,auth.required=false
refuses "jwt mode with the door off" "requires auth.enabled=true and auth.required=true" -f values.yaml --set trino.identityMode=jwt,trino.httpScheme=https,auth.required=false,auth.acknowledgeNoAuth=true
refuses "impersonation over http" "requires trino.httpScheme=https" -f values.yaml --set trino.identityMode=impersonation,trino.credential=password,existingSecret.name=s
refuses "the system catalog" "trino.catalog=system" -f values.yaml --set trino.catalog=system
refuses "stdio" "cannot be served by a pod" -f values.yaml --set transport=stdio
refuses "a resource URL off the endpoint" "the published resource must be the URL hosts call" -f values.yaml --set auth.resourceUrl=https://mcp.example.com/other
refuses "reencrypt to the plain HTTP port" "the server listens in plain HTTP" -f values.yaml --set route.enabled=true,route.tls.termination=reencrypt
refuses "impersonation without a credential" "requires trino.credential=password or clientCertificate" -f values.yaml --set trino.identityMode=impersonation,trino.httpScheme=https
refuses "a disruption budget that blocks every drain" "blocks every node drain" -f values.yaml --set podDisruptionBudget.enabled=true
refuses "a value the schema does not know" "additional properties" -f values.yaml --set trino.hots=x
if helm template t "$CHART" -f values.yaml --set auth.enabled=false,auth.required=false,auth.acknowledgeNoAuth=true >/dev/null 2>&1; then
  pass "the door off renders with acknowledgeNoAuth=true (service mode)"
else fail "the door off with acknowledgeNoAuth=true does not render"; fi
if helm lint "$CHART" -f values.yaml >"$lint" 2>&1; then pass "helm lint: $(tail -1 "$lint")"; else cat "$lint"; fail "helm lint"; fi


exit $failed
