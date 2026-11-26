#!/usr/bin/env bash
# The chart's render refusals and warnings, with helm only (no cluster): each setting that
# cannot work must fail `helm template` with the message that names it; each one that works
# but leaves something open must render with its warning in the notes; the consistent ones must
# render and lint. Run by CI and by run.sh.
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
refuses "jwt mode with the door off" "requires auth.enabled=true and auth.required=true" -f values.yaml --set trino.identityMode=jwt,trino.httpScheme=https,auth.required=false
refuses "a door that requires tokens it cannot verify" "a door that requires a token it cannot verify" -f values.yaml --set auth.enabled=false
refuses "impersonation over http" "requires trino.httpScheme=https" -f values.yaml --set trino.identityMode=impersonation,trino.credential=password,existingSecret.name=s
refuses "stdio" "cannot be served by a pod" -f values.yaml --set transport=stdio
refuses "a resource URL off the endpoint" "the published resource must be the URL hosts call" -f values.yaml --set auth.resourceUrl=https://mcp.example.com/other
refuses "reencrypt to the plain HTTP port" "the server listens in plain HTTP" -f values.yaml --set route.enabled=true,route.tls.termination=reencrypt
refuses "impersonation without a credential" "requires trino.credential=password or clientCertificate" -f values.yaml --set trino.identityMode=impersonation,trino.httpScheme=https
refuses "the door on without an issuer" "auth.issuer is required" -f values.yaml --set auth.issuer=
refuses "helm test without the health Service" "keep service.exposeHealthPort=true" -f values.yaml --set service.exposeHealthPort=false
refuses "a resource URL without a path" "the published resource must be the URL hosts call" -f values.yaml --set auth.resourceUrl=https://mcp.example.com
refuses "an Ingress the NetworkPolicy admits no one to" "requires networkPolicy.ingress.from" -f values.yaml --set 'networkPolicy.ingress.from=null,ingress.enabled=true,ingress.hosts[0].host=mcp.example.com'
refuses "an inline Route key" "additional properties 'key' not allowed" -f values.yaml --set route.tls.key=x
refuses "a profile budget past the query budget" "is past trino.queryTimeoutSeconds" -f values.yaml --set limits.profileTimeoutSeconds=90,trino.queryTimeoutSeconds=60
refuses "a profile budget past the call budget" "is past limits.callTimeoutSeconds" -f values.yaml --set limits.profileTimeoutSeconds=90,limits.callTimeoutSeconds=60
refuses "a value the schema does not know" "additional properties" -f values.yaml --set trino.hots=x
renders() {  # renders <what> <template> <expected text...> -- <helm template arguments...>
  local what="$1" template="$2"; shift 2
  local expected=(); while [ "$1" != "--" ]; do expected+=("$1"); shift; done; shift
  local out; out="$(helm template t "$CHART" -s "$template" "$@" 2>&1)" || { fail "$what: $(grep -m1 Error <<<"$out")"; return; }
  for e in "${expected[@]}"; do grep -qF -- "$e" <<<"$out" || { fail "$what: no '$e' in $template"; return; }; done
  pass "$what"
}
renders "the Ingress routes the endpoint and the protected resource metadata" templates/ingress.yaml \
  'path: "/mcp"' 'path: "/.well-known/oauth-protected-resource"' -- -f values.yaml \
  --set 'ingress.enabled=true,ingress.hosts[0].host=mcp.example.com,auth.resourceUrl=https://mcp.example.com/mcp,networkPolicy.enabled=false'
renders "the Ingress routes /sse and /messages/ with sse" templates/ingress.yaml \
  'path: "/sse"' 'path: "/messages/"' -- -f values.yaml --set 'transport=sse,ingress.enabled=true,ingress.hosts[0].host=mcp.example.com,networkPolicy.enabled=false'
out="$(helm template t "$CHART" -s templates/route.yaml -f values.yaml --set route.enabled=true,route.host=mcp.example.com,route.tls.externalCertificate=mcp-tls 2>&1)"
if ! grep -q '^  path:' <<<"$out" && grep -q 'name: "mcp-tls"' <<<"$out"; then pass "the Route routes every path of the MCP port, certificate from a Secret"
else fail "the Route: $out"; fi
renders "the health port on its own ClusterIP Service" templates/service.yaml \
  'name: t-akko-mcp-trino-health' 'type: ClusterIP' -- -f values.yaml --set service.type=LoadBalancer,service.acknowledgePlainHttp=true
renders "the router may read the Route's certificate Secret" templates/route.yaml \
  'kind: RoleBinding' 'resourceNames: ["mcp-tls"]' 'namespace: openshift-ingress' -- -f values.yaml \
  --set route.enabled=true,route.tls.externalCertificate=mcp-tls
long="$(printf 'm%.0s' $(seq 63))"
renders "a 63-character name keeps the health Service within 63" templates/service.yaml \
  "name: $(printf 'm%.0s' $(seq 56))-health" -- -f values.yaml --set fullnameOverride="$long"
renders "an explicit minAvailable of 0 stays 0" templates/pdb.yaml 'minAvailable: 0' -- -f values.yaml \
  --set podDisruptionBudget.enabled=true,podDisruptionBudget.minAvailable=0
renders "maxUnavailable alone" templates/pdb.yaml 'maxUnavailable: 1' -- -f values.yaml --set podDisruptionBudget.enabled=true,podDisruptionBudget.maxUnavailable=1
# A setting that works but leaves something open renders, and the install's notes say the risk
# and the safe setting, one line each. NOTES.txt prints mcp-trino.warnings; helm template does
# not render NOTES, and helm install --dry-run needs a cluster with Helm 3, so a copy of the
# chart renders that helper in a template of its own.
renders "helm test says why when no pod is ready" templates/tests/test-check.yaml \
  'no pod is ready behind the Service t-akko-mcp-trino-health' 'port-forward deployment/t-akko-mcp-trino 3001:3001' -- -f values.yaml
grep -q 'port-forward deployment/{{ $fullName }} {{ .Values.ports.mcp }}:{{ .Values.ports.mcp }} {{ .Values.ports.health }}:{{ .Values.ports.health }}' \
  "$CHART/templates/NOTES.txt" && pass "NOTES gives the port-forward of both ports" || fail "NOTES gives no port-forward of both ports"
grep -q 'include "mcp-trino.warnings"' "$CHART/templates/NOTES.txt" \
  && pass "NOTES.txt prints the warnings" || fail "NOTES.txt does not print mcp-trino.warnings"
probe="$(mktemp -d)"
cp -R "$CHART" "$probe/chart"
printf '%s\n' '# {{ include "mcp-trino.warnings" . | trim | replace "\n" "\n# " }}' >"$probe/chart/templates/warnings.yaml"
warns() {  # warns <what> <expected line> <helm template arguments...>
  local what="$1" expected="$2"; shift 2
  local out; out="$(helm template t "$probe/chart" -s templates/warnings.yaml "$@" 2>&1)" \
    || { fail "$what: refused: $(grep -m1 Error <<<"$out")"; return; }
  if grep -q "WARNING: .*$expected" <<<"$out"; then pass "$what renders, with: $(grep -m1 -o "WARNING: .*$expected" <<<"$out" | cut -c1-110)"
  else fail "$what: no warning '$expected'"; fi
}
warns "the door off (service mode)" "the door is open" -f values.yaml --set auth.enabled=false,auth.required=false
warns "a JWKS over http" "the JWKS is fetched over http" -f values.yaml
warns "a NodePort Service" "serves the MCP port in plain HTTP" -f values.yaml --set service.type=NodePort
warns "the system catalog" "trino.catalog=system" -f values.yaml --set trino.catalog=system
warns "a disruption budget that blocks every drain" "blocks every node drain" -f values.yaml --set podDisruptionBudget.enabled=true
warns "Trino's certificate not verified" "trino.verify=false" -f values.yaml --set trino.httpScheme=https,trino.verify=false
warns "the token endpoint's certificate not verified" "trino.tokenExchange.verify=false" -f values-jwt.yaml --set trino.tokenExchange.verify=false
warns "extraEnv overriding the door" "extraEnv sets MCP_AUTH_ENABLED" -f values.yaml --set 'extraEnv[0].name=MCP_AUTH_ENABLED,extraEnv[0].value=false'
# The acknowledge* values of earlier versions are accepted, without effect: the warning stays.
warns "the deprecated acknowledgements" "the door is open" -f values.yaml \
  --set auth.enabled=false,auth.required=false,auth.acknowledgeNoAuth=true,auth.acknowledgeInsecureJwks=true \
  --set service.type=NodePort,service.acknowledgePlainHttp=true
if out="$(helm template t "$probe/chart" -s templates/warnings.yaml -f values-jwt.yaml 2>&1)" && ! grep -q 'WARNING:' <<<"$out"; then
  pass "the safe values of the jwt proof render with no warning"
else fail "the jwt proof's values: $(grep -m1 -E 'Error|WARNING' <<<"$out")"; fi
rm -rf "$probe"
if helm lint "$CHART" -f values.yaml >"$lint" 2>&1; then pass "helm lint: $(tail -1 "$lint")"; else cat "$lint"; fail "helm lint"; fi


exit $failed
