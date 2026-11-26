# Deploy on OpenShift without internet access

**English** · [Français](../fr/openshift-air-gapped.md)

This page covers the most constrained deployment: an OpenShift cluster that reaches no package
index and no public registry, a Trino that authenticates users by password against a directory
(LDAP), over https with a certificate from an internal certificate authority, and no OAuth 2.0 on
the Trino side. Trino can then neither verify nor exchange a token, so the server runs in
`impersonation` mode: it signs in to Trino with its own directory account and asks Trino to run each
query as the verified caller. The callers bring a token from the identity provider, which the
server verifies by JWKS.

The steps:

1. build the image on Red Hat Universal Base Image 9 (UBI 9), on a machine with access to the
   package index, or inside with no network at all;
2. move it to the internal registry;
3. let the server's account impersonate the callers in Trino, and that account only;
4. install the chart with the internal registry, the server's password in a Secret, the internal
   certificate authority and a NetworkPolicy;
5. check the release with `helm test`.

## Build the UBI 9 image

[`Dockerfile.ubi9`](../../Dockerfile.ubi9) builds the image on two Red Hat images, both pinned by
digest: `ubi9/python-312` for the build stage, which brings the compiler and the MIT Kerberos
headers, and `ubi9/python-312-minimal` for the image itself. It installs every Python package from
the locks of `requirements/`, at exact versions and with their hashes, from a local wheelhouse
only, and no system package: the build reads no index and runs with `--network=none`. The image
runs as UID 1001 or as any UID of group 0, on a read-only root filesystem.

On a machine with access to the package index and Docker (or Podman, with
`CONTAINER_ENGINE=podman`), from a clone of the repository, write the wheelhouse, then build:

```bash
bash scripts/wheelhouse.sh
docker build --network=none -f Dockerfile.ubi9 -t akko-mcp-trino:ubi9 .
docker run --rm akko-mcp-trino:ubi9 akko-mcp-trino --version
```

```text
wheelhouse.sh: wheelhouse holds 60 wheels and 2 sdists for x86_64
akko-mcp-trino 0.8.3
```

`scripts/wheelhouse.sh` runs in the build stage's base image, so that pip picks the wheels of its
Python and of RHEL 9's C library, and fails on a pin that has none. The wheelhouse and the image are
those of the machine's platform; `linux/amd64` is the one built and checked.

To build inside instead, copy the repository with its `wheelhouse/` folder, and copy the two base
images, by digest, into a registry the build reaches. `MIRROR` is that registry and its path, such
as `registry.example.internal/mirror`:

```bash
skopeo copy --all docker://registry.access.redhat.com/ubi9/python-312@sha256:5a0471c98fc10017d011c16b7615cd1d7f157d796f89eb4900ca0dc6c87384cf docker://$MIRROR/ubi9/python-312:1-1795522683
skopeo copy --all docker://registry.access.redhat.com/ubi9/python-312-minimal@sha256:3c3413652c3cf6aab00587926949ca8884f06b7d28dbbe69508e29aade34e2fe docker://$MIRROR/ubi9/python-312-minimal:9.9-1795231781
docker build --network=none -f Dockerfile.ubi9 \
  --build-arg BUILD_BASE="$MIRROR/ubi9/python-312@sha256:5a0471c98fc10017d011c16b7615cd1d7f157d796f89eb4900ca0dc6c87384cf" \
  --build-arg BASE="$MIRROR/ubi9/python-312-minimal@sha256:3c3413652c3cf6aab00587926949ca8884f06b7d28dbbe69508e29aade34e2fe" \
  -t akko-mcp-trino:ubi9 .
```

`skopeo copy --all` keeps every platform of the image, so the digests stay those of
`Dockerfile.ubi9`.

In an OpenShift project, a BuildConfig with the Docker strategy can build the same image, with these
two values as `buildArgs`. [`integration/helm/openshift-ldap.sh`](../../integration/helm/openshift-ldap.sh)
creates one and sends it, with `oc start-build --from-dir`, a folder that holds only the files
`Dockerfile.ubi9` copies and the wheelhouse, not the whole clone. Its conditions:

- the build pulls the base images: name the registry's pull secret in `dockerStrategy.pullSecret`,
  unless the cluster mirrors `registry.access.redhat.com` (an `ImageDigestMirrorSet`, which an
  administrator sets);
- the project's users may use the Docker strategy (`system:build-strategy-docker`), which hardened
  clusters often withdraw;
- the cluster's builder supports `RUN --mount=type=bind` from another stage and from the context.
  OpenShift 4.22 does: there, the build of a 27 MB context (26 MB of wheels) took 256 s and pushed
  the image to the project's registry; the build pod used at most 274 MiB, under a 2 GiB limit.
  The OpenShift documentation does not say from which version; on an older cluster, try it.

When one of them does not hold, build the image elsewhere and push it, as the next section does.

Compared with the Debian image, the UBI 9 image carries glibc 2.34 and MIT Kerberos 1.21.1 (RHEL 9)
instead of glibc 2.41 and MIT Kerberos 1.21.3 (Debian 13), with the same Python 3.12.16 and the same
locked packages. It is about 110 MB compressed (465 MB unpacked), against 90 MB (384 MB).

## Move the image to the internal registry

Save the image as an archive on the machine that built it:

```bash
docker save akko-mcp-trino:ubi9 | gzip > akko-mcp-trino-ubi9.tar.gz
```

Inside, load it and push it to the internal registry. `REGISTRY` is the registry and its project,
such as `registry.example.internal/data`:

```bash
gunzip -c akko-mcp-trino-ubi9.tar.gz | docker load
docker tag akko-mcp-trino:ubi9 "$REGISTRY/akko-mcp-trino:ubi9"
docker push "$REGISTRY/akko-mcp-trino:ubi9"
docker inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "$REGISTRY/akko-mcp-trino:ubi9" | grep "^$REGISTRY/"
```

The last line prints the image's digest in the registry. The chart pins it (`image.digest`).

## Let the server's account impersonate the callers

Trino checks each user's password with a bind to the directory
(`etc/password-authenticator.properties`):

```properties
password-authenticator.name=ldap
ldap.url=ldaps://ldap.example.internal:636
ldap.ssl.truststore.path=/etc/trino/internal-ca.pem
ldap.user-bind-pattern=uid=${USER},ou=people,dc=example,dc=internal
```

The server signs in as its own account, `mcp-server` here, and sends the caller's name in
`X-Trino-User`. With Trino's file-based access control, one rule lets that account, and no other,
run queries as the users it serves:

```json
{
  "impersonation": [
    {"original_user": "mcp-server", "new_user": "(alice|bob)", "allow": true}
  ]
}
```

A narrower pattern is safer than `.*`: list the users, or a pattern of their names. With Apache
Ranger, the same rule is a policy of the Trino service on the `trinouser` resource (the users the
server may act for) that grants the server's account the `impersonate` permission. Give the
server's account no right on the data: every query then runs with the caller's rights alone.

Trino's HTTP port does not bypass the passwords: Trino 482 answers a statement sent there with
`X-Trino-User: alice` and no password with `403 Forbidden: Authentication over HTTP is not enabled`,
as long as `http-server.authentication.allow-insecure-over-http` stays `false`, its default. When
other pods share the namespace, close that port anyway (`http-server.http.enabled=false`, with
`discovery.uri` over https and `node.internal-address` a name of its certificate) or filter it, as
[`integration/ldap/trino/config.properties`](../../integration/ldap/trino/config.properties) does.

## Install the chart

Create the Secret that holds the server's password, the ConfigMap of the internal certificate
authority, and, if the registry requires it, its pull secret:

```bash
read -rs TRINO_PASSWORD
printf %s "$TRINO_PASSWORD" | oc create secret generic mcp-trino-credentials --from-file=trino-password=/dev/stdin
oc create configmap trusted-ca --from-file=ca-bundle.crt=internal-ca.pem
read -rs REGISTRY_PASSWORD
printf '{"auths":{"registry.example.internal":{"auth":"%s"}}}' "$(printf %s:%s "$REGISTRY_USER" "$REGISTRY_PASSWORD" | base64 | tr -d '\n')" \
  | oc create secret generic internal-registry --type=kubernetes.io/dockerconfigjson --from-file=.dockerconfigjson=/dev/stdin
```

The passwords reach `oc` on its standard input, never as arguments, so that neither the shell
history nor the list of processes shows them.

The chart mounts the bundle for Trino (`TRINO_VERIFY`) and, with `ca.setSslCertFile` (on by
default), for the JWKS fetch (`SSL_CERT_FILE`). That variable replaces the system's store: the
bundle must hold every authority the server needs, Trino's and the identity provider's. Where an
administrator has given the cluster a trusted bundle (`spec.trustedCA` of `proxy/cluster`), an
empty ConfigMap labelled `config.openshift.io/inject-trusted-cabundle=true` can replace the second
command: OpenShift writes that bundle, with the system's authorities, under `ca-bundle.crt`.
Without that setting, nothing is written, and the ConfigMap must come from the PEM file.

The values, in `my-values.yaml` (the chart's checks lint and render the same file,
[`ci/airgapped-values.yaml`](../../charts/akko-mcp-trino/ci/airgapped-values.yaml)):

```yaml
# OpenShift without internet access: the UBI 9 image from an internal registry, by digest,
# with its pull secret; Trino in the same project with password authentication, the server's
# own account in impersonation mode, its password in an existing Secret; the internal
# authority from a ConfigMap, for Trino and for the JWKS; the NetworkPolicy: the router in,
# egress to DNS, Trino in the project and the identity provider only.
openshift: true
image:
  repository: registry.example.internal/data/akko-mcp-trino
  digest: sha256:0000000000000000000000000000000000000000000000000000000000000000
imagePullSecrets:
  - name: internal-registry
trino:
  host: trino
  port: 8443
  catalog: lakehouse
  identityMode: impersonation
  user: mcp-server
  credential: password
auth:
  jwksUrl: https://idp.example.internal/realms/data/protocol/openid-connect/certs
  issuer: https://idp.example.internal/realms/data
  audience: mcp-trino
  resourceUrl: https://mcp.apps.example.internal/mcp
  userClaim: preferred_username
existingSecret:
  name: mcp-trino-credentials
ca:
  # The internal authorities, Trino's and the identity provider's, in one PEM bundle.
  configMap: trusted-ca
  key: ca-bundle.crt
route:
  enabled: true
  host: mcp.apps.example.internal
networkPolicy:
  enabled: true
  egress:
    trino:
      - podSelector:
          matchLabels:
            app.kubernetes.io/name: trino
            app.kubernetes.io/component: coordinator
    identityProvider:
      to:
        - ipBlock:
            cidr: 192.0.2.10/32
      ports:
        - port: 443
          protocol: TCP
```

`imagePullSecrets` replaces the pull secrets OpenShift gives the pod's service account: for an
image of the project's own registry (one a BuildConfig built), leave it out, or the pull fails
(`ImagePullBackOff`, seen on OpenShift 4.22). Set `image.digest` to the digest the push printed, `userClaim` to the claim that carries the
directory's uid (the name Trino knows), and the peers of the NetworkPolicy to those of the
cluster. A `podSelector` without a `namespaceSelector` names pods of the release's project, and the
port of a rule is the pod's port, not the Service's. An `ipBlock` matches the address the packets
go to: when the identity provider is reached through the cluster's router or an egress proxy, name
those instead. With the NetworkPolicy on, the server reaches the cluster DNS, Trino and the identity
provider, and nothing else. The `helm test` pod gets its own policy, so that it passes in a project
that denies egress by default.

On Kubernetes without OpenShift (`openshift: false`), the chart runs the pod as UID and group
10001, the Debian image's user, not the UBI 9 image's 1001. The server works so, since it writes
only to `/tmp`.

Render it first: `helm template` fails on a value the server would refuse at start.

```bash
helm template mcp-trino ./charts/akko-mcp-trino -f my-values.yaml | grep -E 'image:|TRINO_VERIFY|SSL_CERT_FILE'
```

```text
  TRINO_VERIFY: "/etc/mcp-trino/ca/ca-bundle.crt"
  SSL_CERT_FILE: "/etc/mcp-trino/ca/ca-bundle.crt"
          image: "registry.example.internal/data/akko-mcp-trino@sha256:0000000000000000000000000000000000000000000000000000000000000000"
      image: "registry.example.internal/data/akko-mcp-trino@sha256:0000000000000000000000000000000000000000000000000000000000000000"
```

## Check with helm test

```bash
helm install mcp-trino ./charts/akko-mcp-trino -f my-values.yaml
helm test mcp-trino --logs
```

`helm test` calls `/health` and `/ready` through the health Service, then runs
`akko-mcp-trino --check` with the release's settings, Secret and mounts. `/ready` answers once the
server's account has run a bounded `SELECT 1` in Trino: the password, the certificate authority
and the NetworkPolicy are then right. A caller's first query then shows the impersonation rule:
`SELECT current_user` returns the caller's uid, never `mcp-server`.

## What the repository proves

[`integration/ldap/run.sh`](../../integration/ldap/run.sh) proves this case with Docker alone:
a directory with three accounts, Keycloak federated on it, Trino over https with password
authentication against it and the impersonation rule above, and the server from the UBI 9 image,
built with no network and run as UID 1234567 of group 0 on a read-only root filesystem. An
extract of a real run:

```text
  OK    without a token, the server answers 401 and says what to send
  OK    a token with the identity provider's key id, signed by another key is refused with 401: (401, '{"error":"unauthenticated","message":"the bearer token is invalid, expired, or not issued for this server: ask the identity provider for a token whose audience is this server"}')
  OK    Trino runs alice's query as alice (SELECT current_user)
  OK    Trino runs bob's query as bob (SELECT current_user)
  OK    bob reads the 5 nations his row filter leaves
  OK    alice's token with X-Trino-User: bob sent to the server: Trino still sees alice
  OK    the server's account alone reads no data: (200, 'Access Denied: Cannot select from table tpch.tiny.nation')
  OK    alice, signed in to Trino with a directory password, may not run a query as bob: (200, 'Access Denied: User alice cannot impersonate user bob')
  OK    Trino's HTTP port runs nothing as alice without her password: no listener: [Errno 111] Connection refused
  26 of 26 checks passed
  OK    no token in the server's log
  OK    no password in the server's log
  OK    the forged token is refused for its signature (InvalidSignatureError)
```

[`integration/helm/openshift-ldap.sh`](../../integration/helm/openshift-ldap.sh) runs the same
checks in an OpenShift project with a member's rights: the image built by a BuildConfig from
`Dockerfile.ubi9`, the chart installed with
[`values-ldap.yaml`](../../integration/helm/values-ldap.yaml), the pod under `restricted-v2`, and
`helm test`. On OpenShift 4.22, in a project without administration rights, every check passed:
the build, the pod under `restricted-v2` with a UID of the project's range, the read-only root, the
code no user can change, the directory under `restricted-v2`, the 26 checks of
`integration/ldap/check.py` through the Service, `helm test` with the NetworkPolicy on, and no token
or password in the server's log. It does not prove a disconnected cluster: the build in the cluster
has the network the cluster gives builds, and its fixtures pull their images from public
registries.
