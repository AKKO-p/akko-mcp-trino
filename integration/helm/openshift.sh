#!/usr/bin/env bash
# The chart's live proof on OpenShift, in the current project only: no namespace and no
# cluster-scoped object is created, so it runs with a project member's rights (a
# developer sandbox, for example) and within a quota (about 1.6 GiB of memory).
#
# Service mode against a disposable Trino and a static JWKS (the fixtures of run.sh), the
# chart with an edge Route and the NetworkPolicy on, then: the pod under the
# restricted-v2 SCC with a UID from the project's range; through the Route, over https,
# a 401 that points to the protected resource metadata, the metadata itself, MCP calls
# with a test token, and no health or metrics port; helm test. Everything it created is
# removed at the end, even on failure, unless KEEP=1. It refuses to run if a release or a
# fixture of the same name is already in the project.
#
# Where the Route API is absent (plain Kubernetes), the OpenShift checks print SKIP and
# the MCP calls go through the Service instead.
#
#   bash integration/helm/openshift.sh
#   MCP_IMAGE=akkoai/akko-mcp-trino:0.7.2 CURL_OPTS=--cacert=ca.pem bash integration/helm/openshift.sh
#
# Needs helm 3, kubectl (or oc), openssl, curl and python3. Nothing secret is printed: the
# test key stays in a temporary directory and in a Secret, both removed at the end.
set -uo pipefail
cd "$(dirname "$0")"
RELEASE="${RELEASE:-mcp-proof}"
MCP_IMAGE="${MCP_IMAGE:-akkoai/akko-mcp-trino:0.7.2}"
CHART=../../charts/akko-mcp-trino
FULL="$RELEASE-akko-mcp-trino"
ISSUER=https://idp.example.internal/realms/test
read -r -a CURL <<<"curl -s --max-time 20 ${CURL_OPTS:-}"
KUBECTL="$(command -v oc || command -v kubectl)"
if [ -z "${NS:-}" ] && command -v oc >/dev/null; then NS="$(oc project -q 2>/dev/null)"; fi
NS="${NS:-$("$KUBECTL" config view --minify -o jsonpath='{..namespace}' 2>/dev/null)}"
NS="${NS:-default}"
k() { "$KUBECTL" -n "$NS" "$@"; }
failed=0
pass() { echo "PASS $*"; }
fail() { echo "FAIL $*"; failed=1; }
skip() { echo "SKIP $*"; }

for tool in helm openssl curl python3; do
  command -v "$tool" >/dev/null || { echo "$tool is missing" >&2; exit 1; }
done
openshift=false
"$KUBECTL" api-resources --api-group=route.openshift.io -o name 2>/dev/null | grep -q '^routes' && openshift=true
echo "== project $NS, OpenShift Route API: $openshift, image $MCP_IMAGE"

# Refuse to touch what this script did not create.
if helm status "$RELEASE" -n "$NS" >/dev/null 2>&1; then echo "release $RELEASE exists in $NS: RELEASE=... to choose another" >&2; exit 1; fi
for object in deployment/trino-test deployment/jwks-test service/trino-test service/jwks-test \
    configmap/jwks-test configmap/client secret/client-key pod/client pod/outsider; do
  if k get "$object" >/dev/null 2>&1; then echo "$object exists in $NS: this script only creates its own" >&2; exit 1; fi
done

work="$(mktemp -d)"
cleanup() {
  if [ "${KEEP:-0}" = "1" ]; then echo "KEEP=1: release $RELEASE and fixtures left in $NS"; rm -rf "$work"; return; fi
  helm uninstall "$RELEASE" -n "$NS" --wait >/dev/null 2>&1
  # helm uninstall leaves the test hook's pod.
  k delete pod client outsider "$FULL-test" --ignore-not-found --wait >/dev/null 2>&1
  k delete -f fixtures.yaml --ignore-not-found --wait >/dev/null 2>&1
  k delete configmap jwks-test client --ignore-not-found >/dev/null 2>&1
  k delete secret client-key --ignore-not-found >/dev/null 2>&1
  rm -rf "$work"
  pattern="$FULL|trino-test|jwks-test|client-key|configmap/client$|pod/client$|pod/outsider$"
  for _ in $(seq 60); do  # the pods of deleted Deployments terminate in their own time
    left="$(k get all,configmap,secret,networkpolicy -o name 2>/dev/null | grep -cE "$pattern")"
    [ "$left" -eq 0 ] && break; sleep 2
  done
  echo "== removed: $left of this script's objects left in $NS"
}
trap cleanup EXIT

echo "== 1. Test key, Trino and JWKS"
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out "$work/private.pem" 2>/dev/null
openssl rsa -in "$work/private.pem" -noout -modulus | cut -d= -f2 | python3 -c '
import base64, json, sys
n = bytes.fromhex(sys.stdin.read().strip())
b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()
print(json.dumps({"keys": [{"kty": "RSA", "kid": "test", "use": "sig", "alg": "RS256", "n": b64(n), "e": "AQAB"}]}))
' >"$work/jwks.json"
k create configmap jwks-test --from-file=jwks.json="$work/jwks.json" >/dev/null || exit 1
k create secret generic client-key --from-file=private.pem="$work/private.pem" >/dev/null || exit 1
rm -f "$work/private.pem"
k create configmap client --from-file=client.py=client.py >/dev/null || exit 1
# On OpenShift the SCC assigns the UID: the fixtures must not pin one.
if $openshift; then sed "s#MCP_IMAGE#$MCP_IMAGE#; /runAsUser: 10001/d" fixtures.yaml
else sed "s#MCP_IMAGE#$MCP_IMAGE#" fixtures.yaml; fi | k apply -f - >/dev/null || exit 1
start=$(date +%s)
if k rollout status deployment/trino-test --timeout 600s >/dev/null && k rollout status deployment/jwks-test --timeout 180s >/dev/null; then
  pass "Trino and JWKS ready in $(( $(date +%s) - start )) s"
else fail "fixtures not ready"; k get pods; k get events --sort-by=.lastTimestamp | tail -15; exit 1; fi

echo "== 2. helm install (edge Route, NetworkPolicy on)"
image=(--set image.repository="${MCP_IMAGE%:*}",image.tag="${MCP_IMAGE##*:}")
route=(); $openshift && route=(--set route.enabled=true)
if helm install "$RELEASE" "$CHART" -n "$NS" -f values.yaml "${image[@]}" "${route[@]}" --wait --timeout 300s >"$work/install" 2>&1; then
  pass "installed, every pod ready"
else cat "$work/install"; k get pods; k get events --sort-by=.lastTimestamp | tail -15; fail "helm install"; exit 1; fi
url="http://$FULL:3000/mcp"
if $openshift; then
  host="$(k get route "$FULL" -o jsonpath='{.spec.host}')"
  url="https://$host/mcp"
  # The resource URL can only be named once OpenShift has assigned the host.
  if helm upgrade "$RELEASE" "$CHART" -n "$NS" -f values.yaml "${image[@]}" "${route[@]}" \
      --set route.host="$host",auth.resourceUrl="$url" --wait --timeout 300s >"$work/upgrade" 2>&1; then
    pass "Route https://$host, auth.resourceUrl=$url"
  else cat "$work/upgrade"; fail "helm upgrade with the resource URL"; exit 1; fi
fi

echo "== 3. The pod under the SCC"
pod="$(k get pod -l app.kubernetes.io/instance="$RELEASE",app.kubernetes.io/component=server \
  -o go-template='{{range .items}}{{if not .metadata.deletionTimestamp}}{{.metadata.name}}{{"\n"}}{{end}}{{end}}' | head -1)"
uid="$(k exec "$pod" -- id -u 2>/dev/null)"
pinned="$(k get deployment "$FULL" -o jsonpath='{.spec.template.spec.securityContext.runAsUser}')"
if $openshift; then
  scc="$(k get pod "$pod" -o jsonpath='{.metadata.annotations.openshift\.io/scc}')"
  [ "$scc" = "restricted-v2" ] && pass "the pod runs under the $scc SCC" || fail "the pod's SCC: '$scc'"
  [ -z "$pinned" ] && pass "the chart sets no runAsUser (OpenShift detected)" || fail "the chart pins runAsUser=$pinned"
  range="$("$KUBECTL" get project "$NS" -o jsonpath='{.metadata.annotations.openshift\.io/sa\.scc\.uid-range}' 2>/dev/null)"
  first="${range%%/*}"; size="${range##*/}"
  if [ -n "$range" ] && [ "$uid" -ge "$first" ] && [ "$uid" -lt $(( first + size )) ]; then pass "UID $uid, in the project's range $range"
  elif [ -z "$range" ] && [ -n "$uid" ] && [ "$uid" != 1000 ] && [ "$uid" != 10001 ]; then pass "UID $uid, assigned by the SCC (range not readable)"
  else fail "UID '$uid', project range '$range'"; fi
else
  skip "SCC and UID range: no OpenShift here (UID $uid, runAsUser ${pinned:-none})"
fi
if k exec "$pod" -- sh -c 'touch /probe' 2>/dev/null; then fail "the root filesystem is writable"; else pass "the root filesystem is read-only"; fi

echo "== 4. Through the Route, over https"
if $openshift; then
  code="$("${CURL[@]}" -o "$work/body" -D "$work/headers" -w '%{http_code}' -X POST "$url" \
    -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' -d '{}')"
  metadata="https://$host/.well-known/oauth-protected-resource/mcp"
  if [ "$code" = 401 ] && grep -qiF "resource_metadata=\"$metadata\"" "$work/headers"; then
    pass "POST /mcp without a token: 401, $(grep -i '^www-authenticate' "$work/headers" | tr -d '\r')"
  else fail "POST /mcp without a token: $code, $(grep -i '^www-authenticate' "$work/headers" | tr -d '\r')"; fi
  code="$("${CURL[@]}" -o "$work/meta" -w '%{http_code}' "$metadata")"
  if [ "$code" = 200 ] && python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); sys.exit(d.get("resource") != sys.argv[2])' "$work/meta" "$url"; then
    pass "GET $metadata: 200, $(tr -d '\n' <"$work/meta" | head -c 200)"
  else fail "GET $metadata: $code, $(head -c 200 "$work/meta")"; fi
  for path in /health /ready /metrics; do
    code="$("${CURL[@]}" -o "$work/h" -w '%{http_code}' "https://$host$path")"
    if grep -qE '"status": ?"(ok|ready)"|mcp_trino_queries_total' "$work/h"; then fail "$path answered through the Route ($code)"
    else pass "$path not served through the Route ($code)"; fi
  done
else
  skip "Route checks: no Route API here"
fi

echo "== 5. MCP calls with a test token ($url)"
overrides="$(python3 - "$MCP_IMAGE" "$url" "$ISSUER" "$openshift" <<'EOF'
import json, sys
image, url, issuer, openshift = sys.argv[1:]
security = {"runAsNonRoot": True, "seccompProfile": {"type": "RuntimeDefault"}}
if openshift != "true":
    security["runAsUser"] = 10001  # the image user, by number: Kubernetes needs one
print(json.dumps({"spec": {
    "automountServiceAccountToken": False,
    "securityContext": security,
    "containers": [{
        "name": "client", "image": image, "command": ["python", "/client/client.py"],
        "env": [{"name": "MODE", "value": "service"}, {"name": "MCP_URL", "value": url},
                {"name": "ISSUER", "value": issuer}, {"name": "AUDIENCE", "value": "mcp-trino"}],
        "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                            "capabilities": {"drop": ["ALL"]}},
        "resources": {"requests": {"cpu": "50m", "memory": "96Mi"}, "limits": {"cpu": "500m", "memory": "256Mi"}},
        "volumeMounts": [{"name": "client", "mountPath": "/client"}, {"name": "key", "mountPath": "/keys"}]}],
    "volumes": [{"name": "client", "configMap": {"name": "client"}},
                {"name": "key", "secret": {"secretName": "client-key"}}]}}))
EOF
)"
k run client --image="$MCP_IMAGE" --restart=Never --quiet --labels=mcp-chart-test/client=true --overrides="$overrides" >/dev/null
phase=""
for _ in $(seq 120); do
  phase="$(k get pod client -o jsonpath='{.status.phase}')"
  case "$phase" in Succeeded|Failed) break ;; esac
  sleep 2
done
k logs client | sed 's/^/  /'
[ "$phase" = "Succeeded" ] && pass "the MCP client's checks" || fail "the MCP client's checks ($phase)"

echo "== 6. helm test"
if helm test "$RELEASE" -n "$NS" --logs --timeout 300s >"$work/test" 2>&1; then pass "helm test"; else fail "helm test"; fi
sed -n '/POD LOGS/,$p' "$work/test" | grep -E 'http|WARNING|trino identity' | sed 's/^/  /'
k top pod 2>/dev/null | grep -E 'NAME|trino-test|jwks-test|akko-mcp' | sed 's/^/  /' || true

echo "== $( [ $failed -eq 0 ] && echo 'all checks passed' || echo 'SOME CHECKS FAILED')"
exit $failed
