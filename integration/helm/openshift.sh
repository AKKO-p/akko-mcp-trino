#!/usr/bin/env bash
# The chart's live proof on OpenShift, in the current project only: no namespace and no
# cluster-scoped object is created, so it runs with a project member's rights (a
# developer sandbox, for example) and within a quota (about 0.8 CPU of requests and
# 2.5 GiB of memory at most, one pass at a time). The machine that runs it needs bash,
# helm, oc or kubectl, openssl and curl; no python.
#
# Three passes, each removed before the next. Every MCP call goes through the edge
# Route over https, with auth.resourceUrl set to the Route's URL:
#  1. service mode, a disposable Trino and a static JWKS: the pod under restricted-v2
#     with a UID from the project's range, the 401 pointing to the protected resource
#     metadata, the metadata, MCP calls with a test token, no health path through the
#     Route, helm test;
#  2. jwt mode (the default), the repository's integration Keycloak (token exchange) and
#     Trino over https with JWT: 401 without a token and with a token issued for Trino,
#     alice then bob as Trino sees them, bob's row filter, no token in the server's log;
#  3. impersonation with a client certificate over https, on the same Keycloak and Trino.
# Everything it created is removed at the end, even on failure, unless KEEP=1. It refuses
# to run beside an object of the same name it did not create.
#
# Where the Route API is absent (plain Kubernetes), the OpenShift checks print SKIP and
# the MCP calls go through the Service instead.
#
#   bash integration/helm/openshift.sh
#   MCP_IMAGE=akkoai/akko-mcp-trino:0.8.1 CURL_OPTS=-k bash integration/helm/openshift.sh
#
# Nothing secret is printed: the test key and passwords stay in a temporary directory
# and in Secrets, all removed at the end.
set -uo pipefail
cd "$(dirname "$0")"
RELEASE="${RELEASE:-mcp-proof}"
MCP_IMAGE="${MCP_IMAGE:-akkoai/akko-mcp-trino:0.8.1}"
CHART=../../charts/akko-mcp-trino
FULL="$RELEASE-akko-mcp-trino"
ISSUER=https://idp.example.internal/realms/test
TOKEN_URL=https://keycloak:8443/realms/integration/protocol/openid-connect/token
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

for tool in helm openssl curl sed grep; do
  command -v "$tool" >/dev/null || { echo "$tool is missing" >&2; exit 1; }
done
openshift=false
"$KUBECTL" api-resources --api-group=route.openshift.io -o name 2>/dev/null | grep -q '^routes' && openshift=true
echo "== project $NS, OpenShift Route API: $openshift, image $MCP_IMAGE"

OBJECTS="deployment/trino-test deployment/jwks-test service/trino-test service/jwks-test
  deployment/keycloak deployment/trino service/keycloak service/trino
  configmap/jwks-test configmap/client configmap/keycloak-realm configmap/trino-etc
  secret/client-key secret/it-secrets secret/it-certs secret/mcp-credentials secret/mcp-service-tls
  pod/client-service pod/client-jwt pod/client-impersonation"
if helm status "$RELEASE" -n "$NS" >/dev/null 2>&1; then echo "release $RELEASE exists in $NS: RELEASE=... to choose another" >&2; exit 1; fi
for object in $OBJECTS; do
  if k get "$object" >/dev/null 2>&1; then echo "$object exists in $NS: this script only creates its own" >&2; exit 1; fi
done

work="$(mktemp -d)"
remove_release() {
  helm uninstall "$RELEASE" -n "$NS" --wait >/dev/null 2>&1
  k delete pod "$FULL-test" --ignore-not-found --wait >/dev/null 2>&1  # left by helm uninstall
}
cleanup() {
  if [ "${KEEP:-0}" = "1" ]; then echo "KEEP=1: release $RELEASE and fixtures left in $NS"; rm -rf "$work"; return; fi
  remove_release
  for object in $OBJECTS; do k delete "$object" --ignore-not-found --wait=false >/dev/null 2>&1; done
  rm -rf "$work"
  names="$(for object in $OBJECTS; do printf '%s|' "${object#*/}"; done)"
  pattern="$FULL|/(${names%|})(-[a-z0-9]{5,10}){0,2}$"
  for _ in $(seq 90); do  # the pods of deleted Deployments terminate in their own time
    left="$(k get all,configmap,secret,networkpolicy,role,rolebinding -o name 2>/dev/null | grep -cE "$pattern")"
    [ "$left" -eq 0 ] && break; sleep 2
  done
  echo "== removed: $left of this script's objects left in $NS"
}
trap cleanup EXIT

image=(--set image.repository="${MCP_IMAGE%:*}",image.tag="${MCP_IMAGE##*:}")
route=(); $openshift && route=(--set route.enabled=true)
url=""
host=""

install() {  # install <what> <helm arguments...>: install, then name the Route's URL as the resource
  local what="$1"; shift
  if helm upgrade --install "$RELEASE" "$CHART" -n "$NS" "${image[@]}" "${route[@]}" "$@" \
      --wait --timeout 420s >"$work/install" 2>&1; then
    pass "$what: installed, every pod ready"
  else cat "$work/install"; k get pods; k get events --sort-by=.lastTimestamp | tail -15; fail "$what: helm install"; return 1; fi
  url="http://$FULL:3000/mcp"
  if $openshift; then
    host="$(k get route "$FULL" -o jsonpath='{.spec.host}')"
    url="https://$host/mcp"
    if helm upgrade "$RELEASE" "$CHART" -n "$NS" "${image[@]}" "${route[@]}" "$@" \
        --set route.host="$host",auth.resourceUrl="$url" --wait --timeout 420s >"$work/upgrade" 2>&1; then
      pass "$what: Route https://$host, auth.resourceUrl=$url"
    else cat "$work/upgrade"; fail "$what: helm upgrade with the resource URL"; return 1; fi
  fi
}

server_pod() {
  k get pod -l app.kubernetes.io/instance="$RELEASE",app.kubernetes.io/component=server \
    -o go-template='{{range .items}}{{if not .metadata.deletionTimestamp}}{{.metadata.name}}{{"\n"}}{{end}}{{end}}' | head -1
}

scc_checks() {
  local pod uid pinned scc range first size
  pod="$(server_pod)"
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
}

route_checks() {  # the door and the discovery through the Route, and no health path
  local what="$1" code metadata path
  if ! $openshift; then skip "$what: Route checks, no Route API here"; return; fi
  code="$("${CURL[@]}" -o "$work/body" -D "$work/headers" -w '%{http_code}' -X POST "$url" \
    -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' -d '{}')"
  metadata="https://$host/.well-known/oauth-protected-resource/mcp"
  if [ "$code" = 401 ] && grep -qiF "resource_metadata=\"$metadata\"" "$work/headers"; then
    pass "$what: POST /mcp without a token through the Route: 401, $(grep -i '^www-authenticate' "$work/headers" | tr -d '\r')"
  else fail "$what: POST /mcp without a token: $code, $(grep -i '^www-authenticate' "$work/headers" | tr -d '\r')"; fi
  code="$("${CURL[@]}" -o "$work/meta" -w '%{http_code}' "$metadata")"
  if [ "$code" = 200 ] && grep -qE "\"resource\": ?\"$url\"" "$work/meta"; then
    pass "$what: GET $metadata: 200, $(tr -d '\n' <"$work/meta" | head -c 200)"
  else fail "$what: GET $metadata: $code, $(head -c 200 "$work/meta")"; fi
  for path in /health /ready /metrics; do
    code="$("${CURL[@]}" -o "$work/h" -w '%{http_code}' "https://$host$path")"
    if grep -qE '"status": ?"(ok|ready)"|mcp_trino_queries_total' "$work/h"; then fail "$what: $path answered through the Route ($code)"
    else pass "$what: $path not served through the Route ($code)"; fi
  done
}

client() {  # client <name> <mode> [label]: client.py in a pod of the project, against $url
  local name="$1" mode="$2" label="${3:-$2}" uid_line="" phase=""
  $openshift || uid_line="runAsUser: 10001"  # the image's user by number, outside OpenShift
  k apply -f - >/dev/null <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: $name
  labels: {mcp-chart-test/client: "true"}
spec:
  restartPolicy: Never
  automountServiceAccountToken: false
  securityContext:
    runAsNonRoot: true
    $uid_line
    seccompProfile: {type: RuntimeDefault}
  containers:
    - name: client
      image: $MCP_IMAGE
      command: [python, /client/client.py]
      env:
        - {name: MODE, value: "$mode"}
        - {name: MCP_URL, value: "$url"}
        - {name: ISSUER, value: "$ISSUER"}
        - {name: AUDIENCE, value: mcp-trino}
        - {name: TOKEN_URL, value: "$TOKEN_URL"}
        - name: INTEGRATION_PASSWORD
          valueFrom: {secretKeyRef: {name: it-secrets, key: INTEGRATION_PASSWORD, optional: true}}
      securityContext:
        allowPrivilegeEscalation: false
        readOnlyRootFilesystem: true
        capabilities: {drop: [ALL]}
      resources:
        requests: {cpu: 50m, memory: 96Mi}
        limits: {cpu: 500m, memory: 256Mi}
      volumeMounts:
        - {name: client, mountPath: /client}
        - {name: key, mountPath: /keys}
        - {name: ca, mountPath: /ca}
  volumes:
    - name: client
      configMap: {name: client}
    - name: key
      secret: {secretName: client-key, optional: true}
    - name: ca
      secret: {secretName: it-certs, optional: true, items: [{key: ca.crt, path: ca.crt}]}
EOF
  for _ in $(seq 150); do
    phase="$(k get pod "$name" -o jsonpath='{.status.phase}')"
    case "$phase" in Succeeded|Failed) break ;; esac
    sleep 2
  done
  k logs "$name" | sed 's/^/  /'
  k delete pod "$name" --wait=false >/dev/null 2>&1
  [ "$phase" = "Succeeded" ] && pass "$label: the MCP client's checks through $url" || fail "$label: the MCP client's checks ($phase)"
}

helm_test() {
  if helm test "$RELEASE" -n "$NS" --logs --timeout 300s >"$work/test" 2>&1; then pass "$1: helm test"; else fail "$1: helm test"; fi
  sed -n '/POD LOGS/,$p' "$work/test" | grep -E 'http|WARNING|trino identity' | sed 's/^/  /'
}

usage() { k top pod 2>/dev/null | grep -E 'NAME|trino|jwks-test|keycloak|akko-mcp' | sed 's/^/  /' || true; }

echo "== 1. service mode: a disposable Trino and a static JWKS"
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out "$work/private.pem" 2>/dev/null
modulus="$(openssl rsa -in "$work/private.pem" -noout -modulus | cut -d= -f2)"
# The modulus, from hexadecimal to base64url, with the shell and openssl only.
n="$(printf "$(sed 's/../\\x&/g' <<<"$modulus")" | openssl base64 -A | tr '+/' '-_' | tr -d '=')"
printf '{"keys": [{"kty": "RSA", "kid": "test", "use": "sig", "alg": "RS256", "n": "%s", "e": "AQAB"}]}\n' "$n" >"$work/jwks.json"
k create configmap jwks-test --from-file=jwks.json="$work/jwks.json" >/dev/null || exit 1
k create secret generic client-key --from-file=private.pem="$work/private.pem" >/dev/null || exit 1
rm -f "$work/private.pem"
k create configmap client --from-file=client.py=client.py >/dev/null || exit 1
# On OpenShift the SCC assigns the UID: the fixtures must not pin one.
if $openshift; then sed "s#MCP_IMAGE#$MCP_IMAGE#; /runAsUser: 10001/d" fixtures.yaml
else sed "s#MCP_IMAGE#$MCP_IMAGE#" fixtures.yaml; fi >"$work/fixtures.yaml"
k apply -f "$work/fixtures.yaml" >/dev/null || exit 1
start=$(date +%s)
if k rollout status deployment/trino-test --timeout 600s >/dev/null && k rollout status deployment/jwks-test --timeout 180s >/dev/null; then
  pass "service: Trino and JWKS ready in $(( $(date +%s) - start )) s"
else fail "service: fixtures not ready"; k get pods; k get events --sort-by=.lastTimestamp | tail -15; exit 1; fi
install service -f values.yaml || exit 1
scc_checks
route_checks service
client client-service service
helm_test service
usage
remove_release
k delete -f "$work/fixtures.yaml" --wait >/dev/null 2>&1

echo "== 2. jwt mode, the default: Keycloak (token exchange) and Trino over https (JWT)"
secret() { head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n'; }
mkdir -p "$work/certs"
CERTS_DIR="$work/certs" sh ../certs.sh >/dev/null 2>&1 || { fail "test certificates"; exit 1; }
client_secret="$(secret)"
k create secret generic it-secrets --from-literal=MCP_CLIENT_SECRET="$client_secret" \
  --from-literal=INTEGRATION_PASSWORD="$(secret)" --from-literal=TRINO_SHARED_SECRET="$(secret)" \
  --from-literal=KEYCLOAK_ADMIN_PASSWORD="$(secret)" >/dev/null
k create secret generic mcp-credentials --from-literal=token-exchange-client-secret="$client_secret" >/dev/null
unset client_secret
k create secret generic it-certs --from-file="$work/certs/ca.crt" --from-file="$work/certs/trino.pem" \
  --from-file="$work/certs/keycloak.crt" --from-file="$work/certs/keycloak.key" >/dev/null
k create secret tls mcp-service-tls --cert="$work/certs/mcp-service.crt" --key="$work/certs/mcp-service.key" >/dev/null
rm -rf "$work/certs"
k create configmap keycloak-realm --from-file=integration.json=../keycloak-realm.json >/dev/null
k create configmap trino-etc $(for f in ../trino/*.properties ../trino/jvm.config ../trino/rules.json ../trino/catalog/tpch.properties; do printf -- '--from-file=%s ' "$f"; done) >/dev/null
k apply -f fixtures-jwt.yaml >/dev/null || exit 1
start=$(date +%s)
if k rollout status deployment/keycloak --timeout 600s >/dev/null && k rollout status deployment/trino --timeout 600s >/dev/null; then
  pass "jwt: Keycloak and Trino (https, JWT) ready in $(( $(date +%s) - start )) s"
else fail "jwt: fixtures not ready"; k get pods; k get events --sort-by=.lastTimestamp | tail -15; exit 1; fi
if install jwt -f values-jwt.yaml; then
  route_checks jwt
  client client-jwt jwt
  helm_test jwt
  if k logs "deployment/$FULL" 2>&1 | grep -qE 'eyJ[A-Za-z0-9_-]{20,}'; then fail "jwt: a token in the server's log"; else pass "jwt: no token in the server's log"; fi
  usage
fi

echo "== 3. impersonation: the server's account by client certificate (mTLS), Trino impersonates the caller"
remove_release
if install impersonation -f values-jwt.yaml --set trino.identityMode=impersonation,trino.user=mcp-service \
    --set trino.credential=clientCertificate,trino.clientCertificate.secretName=mcp-service-tls; then
  scc_checks
  route_checks impersonation
  client client-impersonation jwt impersonation
  helm_test impersonation
  if k logs "deployment/$FULL" 2>&1 | grep -qE 'eyJ[A-Za-z0-9_-]{20,}'; then fail "impersonation: a token in the server's log"; else pass "impersonation: no token in the server's log"; fi
fi

echo "== $( [ $failed -eq 0 ] && echo 'all checks passed' || echo 'SOME CHECKS FAILED')"
exit $failed
