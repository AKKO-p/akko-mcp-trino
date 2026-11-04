#!/usr/bin/env bash
# The live proof of the Helm chart on a Kubernetes cluster (the current kubectl context):
# the render refusals (refusals.sh), then, in a new namespace, a disposable Trino and JWKS endpoint, the
# chart installed with the door and the NetworkPolicy on, helm test, --check in the pod,
# an MCP client through the Service, a pod the NetworkPolicy keeps out, the pod's
# security, an arbitrary UID as OpenShift assigns one; then everything removed unless
# KEEP=1. The namespace must not exist: the script never touches an existing one.
#
#   integration/helm/run.sh
#   NS=my-test MCP_IMAGE=akkoai/akko-mcp-trino:0.7.2 integration/helm/run.sh
#
# Needs kubectl, helm 3, openssl and python3 on the machine that runs it.
set -uo pipefail
cd "$(dirname "$0")"
NS="${NS:-mcp-chart-test}"
RELEASE="${RELEASE:-mcp}"
MCP_IMAGE="${MCP_IMAGE:-akkoai/akko-mcp-trino:0.7.2}"
CHART=../../charts/akko-mcp-trino
FULL="$RELEASE-akko-mcp-trino"
ISSUER=https://idp.example.internal/realms/test
k() { kubectl -n "$NS" "$@"; }
failed=0
pass() { echo "PASS $*"; }
fail() { echo "FAIL $*"; failed=1; }

if kubectl get namespace "$NS" >/dev/null 2>&1; then
  echo "namespace $NS exists: choose another (NS=...), this script only works in a new one" >&2
  exit 1
fi
work="$(mktemp -d)"
cleanup() {
  if [ "${KEEP:-0}" = "1" ]; then echo "KEEP=1: namespace $NS left as is"; return; fi
  helm uninstall "$RELEASE" -n "$NS" --wait >/dev/null 2>&1
  kubectl delete namespace "$NS" --wait --timeout 180s >/dev/null 2>&1
  rm -rf "$work"
  echo "== namespace $NS removed: $(kubectl get namespace "$NS" --no-headers 2>/dev/null | wc -l | tr -d ' ') left"
}
trap cleanup EXIT

echo "== 1. Render refusals (helm template, no cluster object)"
bash refusals.sh || failed=1

echo "== 2. Namespace $NS, test key, Trino and JWKS"
kubectl create namespace "$NS" >/dev/null || exit 1
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out "$work/private.pem" 2>/dev/null
openssl rsa -in "$work/private.pem" -noout -modulus | cut -d= -f2 | python3 -c '
import base64, json, sys
n = bytes.fromhex(sys.stdin.read().strip())
b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()
print(json.dumps({"keys": [{"kty": "RSA", "kid": "test", "use": "sig", "alg": "RS256", "n": b64(n), "e": "AQAB"}]}))
' >"$work/jwks.json"
k create configmap jwks-test --from-file=jwks.json="$work/jwks.json" >/dev/null
k create secret generic client-key --from-file=private.pem="$work/private.pem" >/dev/null
k create configmap client --from-file=client.py=client.py >/dev/null
sed "s#MCP_IMAGE#$MCP_IMAGE#" fixtures.yaml | k apply -f - >/dev/null
start=$(date +%s)
k rollout status deployment/trino-test --timeout 420s >/dev/null && k rollout status deployment/jwks-test --timeout 120s >/dev/null \
  && pass "Trino and JWKS ready in $(( $(date +%s) - start )) s" || { fail "fixtures not ready"; k get pods; exit 1; }

echo "== 3. helm install"
start=$(date +%s)
if helm install "$RELEASE" "$CHART" -n "$NS" -f values.yaml \
    --set image.repository="${MCP_IMAGE%:*}",image.tag="${MCP_IMAGE##*:}" --wait --timeout 300s >"$work/install" 2>&1; then
  pass "installed, every pod ready, in $(( $(date +%s) - start )) s"
  sed -n '/^NOTES:/,$p' "$work/install" | sed 's/^/  /'
else cat "$work/install"; k get pods; k describe pods -l app.kubernetes.io/component=server | tail -30; fail "helm install"; exit 1; fi
k get pods -l app.kubernetes.io/instance="$RELEASE" -o wide

echo "== 4. helm test"
if helm test "$RELEASE" -n "$NS" --logs --timeout 180s >"$work/test" 2>&1; then pass "helm test"; else fail "helm test"; fi
sed 's/^/  /' "$work/test"

echo "== 5. --check in the server's pod"
if k exec "deployment/$FULL" -- akko-mcp-trino --check >"$work/check" 2>&1; then pass "--check exits 0"; else fail "--check exits $?"; fi
sed 's/^/  /' "$work/check"

echo "== 6. An MCP client through the Service (a pod the NetworkPolicy admits)"
client_pod() {  # client_pod <name> <labels as JSON> <command...>
  local name="$1" labels="$2"; shift 2
  k run "$name" --image="$MCP_IMAGE" --restart=Never --quiet --labels="$labels" --overrides='{"spec":{"automountServiceAccountToken":false,
      "securityContext":{"runAsNonRoot":true,"runAsUser":10001,"seccompProfile":{"type":"RuntimeDefault"}},
      "containers":[{"name":"'"$name"'","image":"'"$MCP_IMAGE"'","command":'"$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1:]))' "$@")"',
        "env":[{"name":"MCP_URL","value":"http://'"$FULL"':3000/mcp"},{"name":"ISSUER","value":"'"$ISSUER"'"},{"name":"AUDIENCE","value":"mcp-trino"}],
        "securityContext":{"allowPrivilegeEscalation":false,"readOnlyRootFilesystem":true,"capabilities":{"drop":["ALL"]}},
        "volumeMounts":[{"name":"client","mountPath":"/client"},{"name":"key","mountPath":"/keys"}]}],
      "volumes":[{"name":"client","configMap":{"name":"client"}},{"name":"key","secret":{"secretName":"client-key"}}]}}' >/dev/null
  local phase=""
  for _ in $(seq 90); do
    phase="$(k get pod "$name" -o jsonpath='{.status.phase}')"
    case "$phase" in Succeeded|Failed) break ;; esac
    sleep 2
  done
  k logs "$name" | sed 's/^/  /'
  [ "$phase" = "Succeeded" ]
}
if client_pod client mcp-chart-test/client=true python /client/client.py; then pass "the MCP client's checks"; else fail "the MCP client's checks"; fi

echo "== 7. A pod the NetworkPolicy does not admit"
# Tried for 30 s, past the delay a NetworkPolicy takes to admit a new pod: never reached.
probe='import socket, sys, time
for attempt in range(15):
    for port in (3000, 3001):
        try:
            socket.create_connection((sys.argv[1], port), timeout=2).close(); print(port, "reached"); sys.exit(1)
        except OSError as exc: last = type(exc).__name__
    time.sleep(2)
print("3000 and 3001 never reached in 30 s:", last)'
if client_pod outsider app=outsider python -c "$probe" "$FULL"; then pass "an unlisted pod reaches neither port"; else fail "an unlisted pod reached the server"; fi

echo "== 8. The server's pod"
pod="$(k get pod -l app.kubernetes.io/instance="$RELEASE",app.kubernetes.io/component=server -o jsonpath='{.items[0].metadata.name}')"
echo "  id: $(k exec "$pod" -- id)"
if k exec "$pod" -- sh -c 'touch /probe' 2>/dev/null; then fail "the root filesystem is writable"; else pass "the root filesystem is read-only"; fi
k exec "$pod" -- python -c 'import os; print("  capabilities:", [l.split()[1] for l in open("/proc/self/status") if l.startswith("CapEff")][0], "no_new_privs:", [l.split()[1] for l in open("/proc/self/status") if l.startswith("NoNewPrivs")][0])'
kubectl top pod -n "$NS" 2>/dev/null | sed 's/^/  /' || true

echo "== 9. An arbitrary UID with group 0, as OpenShift's restricted-v2 assigns"
if helm upgrade "$RELEASE" "$CHART" -n "$NS" -f values.yaml --set image.repository="${MCP_IMAGE%:*}",image.tag="${MCP_IMAGE##*:}" \
    --set podSecurityContext.runAsUser=1000680000,podSecurityContext.runAsGroup=0,podSecurityContext.fsGroup=1000680000 \
    --wait --timeout 300s >/dev/null 2>&1; then
  pod="$(k get pod -l app.kubernetes.io/instance="$RELEASE",app.kubernetes.io/component=server \
    -o go-template='{{range .items}}{{if not .metadata.deletionTimestamp}}{{.metadata.name}}{{"\n"}}{{end}}{{end}}' | head -1)"
  id="$(k exec "$pod" -- id 2>&1)"
  case "$id" in uid=1000680000*) pass "ready as $id" ;; *) fail "the new pod's identity: $id" ;; esac
  if helm test "$RELEASE" -n "$NS" --timeout 180s >/dev/null 2>&1; then pass "helm test with the arbitrary UID"; else fail "helm test with the arbitrary UID"; fi
else fail "upgrade with an arbitrary UID"; k get pods; fi

echo "== $( [ $failed -eq 0 ] && echo 'all checks passed' || echo 'SOME CHECKS FAILED')"
exit $failed
