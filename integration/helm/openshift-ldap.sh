#!/usr/bin/env bash
# The chart's live proof of the ldap case on OpenShift, in the current project only, with a
# project member's rights: no namespace and no cluster-scoped object is created. The cluster
# version of integration/ldap, and a fourth pass beside openshift.sh:
#  - the UBI 9 image built in the project by a BuildConfig (Docker strategy) from
#    Dockerfile.ubi9 and the wheelhouse prepared beforehand (bash scripts/wheelhouse.sh):
#    the build reads no package index;
#  - the directory (three accounts, LDAPS), Keycloak federated on it, Trino over https with
#    password authentication against it and an impersonation rule for the server's account
#    only (fixtures-ldap.yaml);
#  - the chart (values-ldap.yaml): the image from the project's registry by digest, the
#    server in impersonation mode with its directory password from a Secret, the run's
#    certificate authority for Trino and for the JWKS, the NetworkPolicy on;
#  - the pod under restricted-v2 with a UID from the project's range, read-only, on UBI 9;
#    integration/ldap/check.py in a pod of the project, through the Service (401 without a
#    token, each user as Trino sees them, their rights, no usurpation, no write); helm test;
#    no token and no password in the server's log.
# The build in the cluster has whatever network the cluster gives builds (Dockerfile.ubi9 needs
# none: integration/ldap/run.sh and CI build it with --network=none), and the fixtures pull
# their images from public registries.
# Everything it created is removed at the end, even on failure, unless KEEP=1. It refuses to
# run beside an object of the same name it did not create. About 1 CPU and 3.75 GiB of memory
# of requests while the fixtures run, 2 GiB more during the build.
#
# Where the Build API is absent (plain Kubernetes), MCP_IMAGE names an image built from
# Dockerfile.ubi9 instead, and the OpenShift checks print SKIP.
#
#   bash scripts/wheelhouse.sh && bash integration/helm/openshift-ldap.sh
#   MCP_IMAGE=registry.example.internal/akko-mcp-trino:ubi9 bash integration/helm/openshift-ldap.sh
#   BUILD_BASE=... BASE=...   the base images of Dockerfile.ubi9 from another registry, by digest
#   BUILD_PULL_SECRET=name    the Secret the build pulls them with (dockerStrategy.pullSecret)
#
# Needs bash, helm, oc (or kubectl with MCP_IMAGE) and openssl. Nothing secret is printed.
set -uo pipefail
cd "$(dirname "$0")"
ROOT=../..
RELEASE="${RELEASE:-mcp-ldap}"
CHART=$ROOT/charts/akko-mcp-trino
# The chart's full name: the release's name alone when it holds the chart's name.
case "$RELEASE" in *akko-mcp-trino*) FULL="$RELEASE" ;; *) FULL="$RELEASE-akko-mcp-trino" ;; esac
KUBECTL="$(command -v oc || command -v kubectl)"
if [ -z "${NS:-}" ] && command -v oc >/dev/null; then NS="$(oc project -q 2>/dev/null)"; fi
NS="${NS:-$("$KUBECTL" config view --minify -o jsonpath='{..namespace}' 2>/dev/null)}"
NS="${NS:-default}"
k() { "$KUBECTL" -n "$NS" "$@"; }
failed=0
pass() { echo "PASS $*"; }
fail() { echo "FAIL $*"; failed=1; }
skip() { echo "SKIP $*"; }

for tool in helm openssl sed grep; do
  command -v "$tool" >/dev/null || { echo "$tool is missing" >&2; exit 1; }
done
openshift=false
"$KUBECTL" api-resources --api-group=security.openshift.io -o name 2>/dev/null | grep -q . && openshift=true
build=false
"$KUBECTL" api-resources --api-group=build.openshift.io -o name 2>/dev/null | grep -q '^buildconfigs' && build=true
# Whether this run builds the image in the project, and so owns the BuildConfig and the
# ImageStream: fixed here, before MCP_IMAGE names the built image.
in_project=false
[ -z "${MCP_IMAGE:-}" ] && in_project=true
if $in_project; then
  $build || { echo "no Build API here: set MCP_IMAGE to an image built from Dockerfile.ubi9" >&2; exit 1; }
  command -v oc >/dev/null || { echo "a binary build needs oc" >&2; exit 1; }
  [ -d "$ROOT/wheelhouse" ] || { echo "no wheelhouse: run bash scripts/wheelhouse.sh first" >&2; exit 1; }
fi
echo "== project $NS, OpenShift: $openshift, Build API: $build, image ${MCP_IMAGE:-built in the project}"

OBJECTS="deployment/ldap deployment/keycloak deployment/trino job/ldap-seed
  service/ldap service/keycloak service/trino
  configmap/ldap-seed configmap/keycloak-realm-ldap configmap/trino-etc-ldap configmap/ldap-check
  secret/it-ldap-secrets secret/it-certs secret/mcp-credentials pod/client-ldap"
BUILD_OBJECTS="buildconfig/akko-mcp-trino-ubi9 imagestream/akko-mcp-trino"
if helm status "$RELEASE" -n "$NS" >/dev/null 2>&1; then echo "release $RELEASE exists in $NS: RELEASE=... to choose another" >&2; exit 1; fi
for object in $OBJECTS $($in_project && echo "$BUILD_OBJECTS"); do
  if k get "$object" >/dev/null 2>&1; then echo "$object exists in $NS: this script only creates its own" >&2; exit 1; fi
done

work="$(mktemp -d)"
cleanup() {
  if [ "${KEEP:-0}" = "1" ]; then echo "KEEP=1: release $RELEASE and fixtures left in $NS"; rm -rf "$work"; return; fi
  helm uninstall "$RELEASE" -n "$NS" --wait >/dev/null 2>&1
  k delete pod "$FULL-test" --ignore-not-found --wait >/dev/null 2>&1  # left by helm uninstall
  for object in $OBJECTS $($in_project && echo "$BUILD_OBJECTS"); do
    k delete "$object" --ignore-not-found --wait=false >/dev/null 2>&1
  done
  k delete pod -l job-name=ldap-seed --ignore-not-found --wait=false >/dev/null 2>&1
  rm -rf "$work"
  for _ in $(seq 90); do  # the pods of deleted Deployments terminate in their own time
    left="$(k get all,configmap,secret,networkpolicy,build,buildconfig,imagestream -o name 2>/dev/null \
      | grep -cE "$FULL|/(ldap|keycloak|trino|ldap-seed|client-ldap|akko-mcp-trino-ubi9|akko-mcp-trino)(-[a-z0-9]{1,10}){0,3}$")"
    [ "$left" -eq 0 ] && break; sleep 2
  done
  echo "== removed: $left of this script's objects left in $NS"
}
trap cleanup EXIT

echo "== 1. The UBI 9 image"
if $in_project; then
  # The build context: what Dockerfile.ubi9 copies, and the wheelhouse; nothing else.
  context="$work/context"
  mkdir -p "$context/providers" "$context/requirements"
  cp "$ROOT"/{Dockerfile.ubi9,pyproject.toml,README.md,LICENSE,NOTICE} "$context/"
  cp -R "$ROOT/akko_mcp_trino" "$ROOT/wheelhouse" "$context/"
  cp -R "$ROOT/providers/atlas" "$context/providers/"
  cp "$ROOT"/requirements/*.txt "$context/requirements/"
  find "$context" -name __pycache__ -prune -exec rm -rf {} +
  args=""
  for arg in BUILD_BASE BASE; do
    [ -n "${!arg:-}" ] && args+="        - {name: $arg, value: \"${!arg}\"}"$'\n'
  done
  k apply -f - >/dev/null <<EOF || exit 1
apiVersion: image.openshift.io/v1
kind: ImageStream
metadata:
  name: akko-mcp-trino
---
apiVersion: build.openshift.io/v1
kind: BuildConfig
metadata:
  name: akko-mcp-trino-ubi9
spec:
  source:
    type: Binary
    binary: {}
  strategy:
    type: Docker
    dockerStrategy:
      dockerfilePath: Dockerfile.ubi9
$( [ -n "$args" ] && printf '      buildArgs:\n%s' "$args")
$( [ -n "${BUILD_PULL_SECRET:-}" ] && printf '      pullSecret:\n        name: %s' "$BUILD_PULL_SECRET")
  output:
    to:
      kind: ImageStreamTag
      name: akko-mcp-trino:ubi9
  resources:
    requests: {cpu: 500m, memory: 1Gi}
    limits: {cpu: "2", memory: 2Gi}
EOF
  echo "  build context: $(du -sh "$context" | cut -f1) (the wheelhouse: $(du -sh "$context/wheelhouse" | cut -f1))"
  # The build pod's memory, sampled while it runs (when the cluster serves pod metrics).
  top=(top); [ "$(basename "$KUBECTL")" = oc ] && top=(adm top)  # oc has no top of its own
  ( while :; do k "${top[@]}" pod -l openshift.io/build.name --no-headers 2>/dev/null; sleep 10; done ) >"$work/build-top" &
  sampler=$!
  start=$(date +%s)
  if oc -n "$NS" start-build akko-mcp-trino-ubi9 --from-dir="$context" --wait --follow >"$work/build" 2>&1; then
    pass "BuildConfig (Docker strategy, Dockerfile.ubi9): image built in $(( $(date +%s) - start )) s"
  else kill "$sampler" 2>/dev/null; tail -40 "$work/build"; fail "the build of the UBI 9 image"; exit 1; fi
  kill "$sampler" 2>/dev/null; wait "$sampler" 2>/dev/null
  peak="$(awk '{print $3}' "$work/build-top" | sort -h | tail -1)"
  echo "  build pod memory at most ${peak:-not measured} (limit 2Gi)"
  # The mounts the build stages read: the wheelhouse from the context, the wheels of the other
  # stage. buildah ran them, or the build would have failed.
  grep -E 'RUN --mount' "$work/build" | sed 's/^/  /' | cut -c1-160
  grep -E 'STEP|Successfully|pushed|Push successful' "$work/build" | tail -5 | sed 's/^/  /'
  reference="$(k get istag akko-mcp-trino:ubi9 -o jsonpath='{.image.dockerImageReference}')"
  repository="${reference%@*}"; digest="${reference#*@}"
  image=(--set image.repository="$repository",image.digest="$digest")
  MCP_IMAGE="$reference"
  pass "image $MCP_IMAGE, from the project's registry, by digest"
else
  image=(--set image.repository="${MCP_IMAGE%:*}",image.tag="${MCP_IMAGE##*:}")
fi

echo "== 2. The directory, Keycloak and Trino (password authentication against the directory)"
secret() { head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n'; }
mkdir -p "$work/certs"
CERTS_DIR="$work/certs" sh ../certs.sh >/dev/null 2>&1 || { fail "test certificates"; exit 1; }
# The passwords go to files of the temporary directory, never to a command's arguments.
( umask 077
  for key in LDAP_ADMIN_PASSWORD LDAP_JWT_SECRET LDAP_KEY_SEED MCP_SERVER_PASSWORD ALICE_PASSWORD \
      BOB_PASSWORD TRINO_SHARED_SECRET KEYCLOAK_ADMIN_PASSWORD; do
    printf '%s=%s\n' "$key" "$(secret)"
  done >"$work/secrets.env"
  sed -n 's/^MCP_SERVER_PASSWORD=//p' "$work/secrets.env" | tr -d '\n' >"$work/trino-password" )
k create secret generic it-ldap-secrets --from-env-file="$work/secrets.env" >/dev/null || exit 1
# The server's own password, in the Secret the chart reads (existingSecret).
k create secret generic mcp-credentials --from-file=trino-password="$work/trino-password" >/dev/null || exit 1
k create secret generic it-certs --from-file="$work/certs/ca.crt" --from-file="$work/certs/trino.pem" \
  --from-file="$work/certs/keycloak.crt" --from-file="$work/certs/keycloak.key" \
  --from-file="$work/certs/ldap.crt" --from-file="$work/certs/ldap.key" >/dev/null || exit 1
rm -rf "$work/certs"
k create configmap ldap-seed --from-file=seed.sh=../ldap/seed.sh >/dev/null || exit 1
k create configmap ldap-check --from-file=check.py=../ldap/check.py >/dev/null || exit 1
k create configmap keycloak-realm-ldap --from-file=ldap.json=../ldap/keycloak-realm.json >/dev/null || exit 1
# integration/trino with the ldap scenario's files over it.
mkdir -p "$work/trino"
cp ../trino/*.properties ../trino/jvm.config ../trino/catalog/tpch.properties "$work/trino/"
cp ../ldap/trino/* "$work/trino/"
k create configmap trino-etc-ldap $(for f in "$work"/trino/*; do printf -- '--from-file=%s ' "$f"; done) >/dev/null || exit 1
k apply -f fixtures-ldap.yaml >/dev/null || exit 1
start=$(date +%s)
if k rollout status deployment/ldap --timeout 300s >/dev/null \
    && k wait --for=condition=complete job/ldap-seed --timeout 300s >/dev/null \
    && k rollout status deployment/keycloak --timeout 600s >/dev/null \
    && k rollout status deployment/trino --timeout 600s >/dev/null; then
  pass "directory (3 accounts), Keycloak and Trino ready in $(( $(date +%s) - start )) s"
  k logs job/ldap-seed | sed 's/^/  /'
else fail "fixtures not ready"; k get pods; k get events --sort-by=.lastTimestamp | tail -15; exit 1; fi

echo "== 3. The chart: impersonation by password, the UBI 9 image"
if helm upgrade --install "$RELEASE" "$CHART" -n "$NS" "${image[@]}" -f values-ldap.yaml \
    --wait --timeout 420s >"$work/install" 2>&1; then
  pass "installed, every pod ready"
else cat "$work/install"; k get pods; k get events --sort-by=.lastTimestamp | tail -15; fail "helm install"; exit 1; fi

pod="$(k get pod -l app.kubernetes.io/instance="$RELEASE",app.kubernetes.io/component=server \
  -o go-template='{{range .items}}{{if not .metadata.deletionTimestamp}}{{.metadata.name}}{{"\n"}}{{end}}{{end}}' | head -1)"
uid="$(k exec "$pod" -- id -u 2>/dev/null)"
if $openshift; then
  scc="$(k get pod "$pod" -o jsonpath='{.metadata.annotations.openshift\.io/scc}')"
  [ "$scc" = "restricted-v2" ] && pass "the pod runs under the $scc SCC" || fail "the pod's SCC: '$scc'"
  range="$("$KUBECTL" get project "$NS" -o jsonpath='{.metadata.annotations.openshift\.io/sa\.scc\.uid-range}' 2>/dev/null)"
  first="${range%%/*}"; size="${range##*/}"
  if [ -n "$range" ] && [ "$uid" -ge "$first" ] && [ "$uid" -lt $(( first + size )) ]; then pass "UID $uid, in the project's range $range"
  elif [ -z "$range" ] && [ -n "$uid" ] && [ "$uid" != 1001 ]; then pass "UID $uid, assigned by the SCC (range not readable)"
  else fail "UID '$uid', project range '$range'"; fi
else
  skip "SCC and UID range: no OpenShift here (UID $uid)"
fi
# Read-only by the mount, not by the rights on /: HOME is writable by group 0, yet refuses a file.
if k exec "$pod" -- awk '$2 == "/" {print $4}' /proc/mounts | grep -q '^ro,' \
    && ! k exec "$pod" -- touch /opt/app-root/src/probe 2>/dev/null; then
  pass "the root filesystem is mounted read-only, a write in HOME fails"
else fail "the root filesystem is writable"; fi
if k exec "$pod" -- sh -c 'test ! -w /opt/app-root/bin && test ! -w /opt/app-root/lib/python3.12/site-packages/akko_mcp_trino && [ "$(pwd)" = / ]'; then
  pass "the code is not writable by UID $uid, and the working directory is /"
else fail "the code or the working directory is writable by UID $uid"; fi
os="$(k exec "$pod" -- sh -c '. /etc/os-release && echo "$NAME $VERSION_ID"' 2>/dev/null)"
case "$os" in "Red Hat Enterprise Linux 9"*) pass "the server runs on $os (UBI 9)" ;; *) fail "the server's system: '$os'" ;; esac

echo "== 4. The checks of integration/ldap, from a pod of the project, through the Service"
k apply -f - >/dev/null <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: client-ldap
  labels: {mcp-chart-test/client: "true"}
spec:
  restartPolicy: Never
  automountServiceAccountToken: false
  securityContext:
    runAsNonRoot: true
    seccompProfile: {type: RuntimeDefault}
  containers:
    - name: client
      image: $MCP_IMAGE
      command: [python, /integration/check.py]
      env:
        - {name: MCP_URL, value: "http://$FULL:3000/mcp"}
        - name: MCP_SERVER_PASSWORD
          valueFrom: {secretKeyRef: {name: it-ldap-secrets, key: MCP_SERVER_PASSWORD}}
        - name: ALICE_PASSWORD
          valueFrom: {secretKeyRef: {name: it-ldap-secrets, key: ALICE_PASSWORD}}
        - name: BOB_PASSWORD
          valueFrom: {secretKeyRef: {name: it-ldap-secrets, key: BOB_PASSWORD}}
      securityContext:
        allowPrivilegeEscalation: false
        readOnlyRootFilesystem: true
        capabilities: {drop: [ALL]}
      resources:
        requests: {cpu: 50m, memory: 96Mi}
        limits: {cpu: 500m, memory: 256Mi}
      volumeMounts:
        - {name: check, mountPath: /integration}
        - {name: ca, mountPath: /certs}
  volumes:
    - name: check
      configMap: {name: ldap-check}
    - name: ca
      secret: {secretName: it-certs, items: [{key: ca.crt, path: ca.crt}]}
EOF
phase=""
for _ in $(seq 150); do
  phase="$(k get pod client-ldap -o jsonpath='{.status.phase}')"
  case "$phase" in Succeeded|Failed) break ;; esac
  sleep 2
done
k logs client-ldap | sed 's/^/  /'
[ "$phase" = "Succeeded" ] && pass "the checks of integration/ldap through http://$FULL:3000/mcp" || fail "the checks of integration/ldap ($phase)"

echo "== 5. helm test and the server's log"
if helm test "$RELEASE" -n "$NS" --logs --timeout 300s >"$work/test" 2>&1; then pass "helm test"; else fail "helm test"; fi
sed -n '/POD LOGS/,$p' "$work/test" | grep -E 'http|WARNING|trino identity' | sed 's/^/  /'
logs="$(k logs "deployment/$FULL" 2>&1)"
if grep -qE 'eyJ[A-Za-z0-9_-]{20,}' <<<"$logs"; then fail "a token in the server's log"; else pass "no token in the server's log"; fi
# The passwords reach grep as patterns on its standard input, never as arguments.
leaked=0
grep -E '^(MCP_SERVER|ALICE|BOB|LDAP_ADMIN)_PASSWORD=' "$work/secrets.env" | cut -d= -f2- \
  | grep -qF -f - <(printf '%s\n' "$logs") && leaked=1
[ $leaked -eq 0 ] && pass "no password in the server's log" || fail "a password in the server's log"
k top pod 2>/dev/null | grep -E 'NAME|ldap|keycloak|trino|akko-mcp' | sed 's/^/  /' || true

echo "== $( [ $failed -eq 0 ] && echo 'all checks passed' || echo 'SOME CHECKS FAILED')"
exit $failed
