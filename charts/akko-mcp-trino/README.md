# akko-mcp-trino Helm chart

Installs the read-only MCP server for Trino on Kubernetes or OpenShift. Each query runs
as the verified caller; the server's own documentation is in the
[repository's README](../../README.md).

The defaults are the safe ones:

- transport `streamable-http`, endpoint `/mcp`;
- the door on: every caller's bearer token is verified against the identity provider's
  JWKS, and a request without a valid token is refused with `401`;
- identity mode `jwt`: the caller's token is exchanged for a token issued for Trino;
- a pod that runs as a non-root user, with a read-only root filesystem, no capability,
  no privilege escalation and the `RuntimeDefault` seccomp profile; on OpenShift the
  user is left to the `restricted-v2` SCC;
- secrets read from an existing Secret, never written in a ConfigMap or in the values.

`helm template` and `helm install` refuse a setting the server would refuse at start, or
one that would leave something open, with a message that names the value to change: a
missing Trino host, the `system` catalog, an identity mode other than `service` over
`http`, the door turned off without `auth.acknowledgeNoAuth: true`, a JWKS over `http`
without `auth.acknowledgeInsecureJwks: true`, a NodePort or LoadBalancer Service without
`service.acknowledgePlainHttp: true`, a resource URL whose path is not the transport's
endpoint, an `extraEnv` entry that would override a variable the chart manages, and
others. `values.schema.json` refuses a value the chart does not know. NOTES warns of what
is left open on purpose: a JWKS over `http`, an empty issuer, TLS verification off,
`trino.readOnly: false`, an Ingress without TLS.

## Install

Create the Secret first. In `jwt` mode it holds the token exchange client's secret:

```bash
kubectl create secret generic mcp-trino-credentials \
  --from-literal=token-exchange-client-secret='...'
```

Then a values file:

```yaml
trino:
  host: trino.example.internal
  port: 8443
  catalog: lakehouse
  tokenExchange:
    url: https://idp.example.com/realms/data/protocol/openid-connect/token
    clientId: mcp-trino
    audience: trino
auth:
  jwksUrl: https://idp.example.com/realms/data/protocol/openid-connect/certs
  issuer: https://idp.example.com/realms/data
  audience: mcp-trino
  resourceUrl: https://mcp.example.com/mcp
existingSecret:
  name: mcp-trino-credentials
```

```bash
helm install mcp-trino ./charts/akko-mcp-trino -f my-values.yaml
kubectl rollout status deployment/mcp-trino-akko-mcp-trino
```

Check what the server reads, without any secret (exit `2` means it would refuse to start):

```bash
kubectl exec deployment/mcp-trino-akko-mcp-trino -- akko-mcp-trino --check
```

And run the chart's test: `/health` and `/ready` through the Service, then `--check` in a
pod of its own, with the release's settings, secrets and mounts:

```bash
helm test mcp-trino --logs
```

## Identity modes

| Mode | What Trino sees | What the chart needs |
|---|---|---|
| `jwt` (default) | the caller, with a token obtained by token exchange (RFC 8693) | `trino.tokenExchange.url`, `clientId`, `audience`; the client secret in `existingSecret` |
| `impersonation` | the caller, impersonated by the server's account | `trino.user`; `trino.credential: password` (key in `existingSecret`) or `clientCertificate` (`trino.clientCertificate.secretName`) |
| `service` | `trino.user` for every caller, announced as a warning | `trino.user`; a credential, or `trino.credential: none` against a test coordinator |

Every mode but `service` requires `trino.httpScheme: https`, and `jwt` and
`impersonation` require the door on. `oauth2` (a browser login over stdio) cannot be
served by a pod and is refused.

### A client certificate (impersonation)

The certificate and its key come from a Secret (a `kubernetes.io/tls` Secret works as
is), mounted read-only with mode `0440`; the pod's group owns them (`fsGroup`, or the
SCC's on OpenShift):

```yaml
trino:
  identityMode: impersonation
  user: mcp-service
  credential: clientCertificate
  clientCertificate:
    secretName: mcp-service-client-tls
```

### Behind Trino Gateway

The gateway terminates TLS, so a client certificate does not reach the cluster: the
server's account authenticates with a password (`trino.credential: password`), which the
gateway forwards. See [`ci/gateway-values.yaml`](ci/gateway-values.yaml).

## An internal certificate authority

```yaml
ca:
  configMap: internal-ca   # or secret: internal-ca
  key: ca.crt
```

The bundle is mounted at `/etc/mcp-trino/ca/` and used for Trino (`TRINO_VERIFY`), the
token endpoint (`TRINO_TOKEN_EXCHANGE_VERIFY`) and, with `ca.setSslCertFile: true` (the
default), every other TLS client of the process, the JWKS fetch included
(`SSL_CERT_FILE`). `SSL_CERT_FILE` replaces the system store: the bundle must then hold
every authority the server needs.

## OpenShift

```yaml
openshift: true      # detected when the cluster serves security.openshift.io/v1
route:
  enabled: true
  host: mcp.example.com
  tls:
    termination: edge
networkPolicy:
  enabled: true      # the router's namespaces are admitted to the MCP port
```

With `openshift` on, the chart sets no `runAsUser`, `runAsGroup` or `fsGroup`: the
`restricted-v2` SCC assigns a UID from the project's range, with group `0`. The image
runs as any UID. `helm install` detects OpenShift; `helm template` offline (GitOps) does
not, so set `openshift: true` there. The Route routes every path of the MCP port (the
endpoint, the protected resource metadata and, with `sse`, `/messages/`). Its certificate
is never inline in the values: name a `kubernetes.io/tls` Secret the router may read in
`route.tls.externalCertificate` (on an OpenShift version whose Route API has
`spec.tls.externalCertificate`), or leave it empty for the router's default certificate.
With it, the chart also renders the Role and RoleBinding that let the router's service
account (`openshift-ingress/router`) read that Secret, as OpenShift documents.
The Route terminates TLS at the router (`edge`) and reaches the pod in plain HTTP; `reencrypt` needs a TLS listener in front of the server (a sidecar in
`extraContainers`, its port in `service.extraPorts` and named in `route.targetPort`),
since the server itself listens in plain HTTP. The Route's timeout annotation is above
the server's tool call budget (120 s by default).

## Ingress

Without `paths`, an Ingress host routes the transport's endpoint (`/mcp` or `/sse`), the
protected resource metadata (`/.well-known/oauth-protected-resource`, when
`auth.resourceUrl` is set, for the hosts' OAuth discovery) and `/messages/` with `sse`.
Set `ingress.tls`, or terminate TLS in front: tokens must not cross the network in clear.
A tool call may last up to `MCP_CALL_TIMEOUT_SECONDS` (120 s by default): raise the
controller's read timeout above it, for example
`nginx.ingress.kubernetes.io/proxy-read-timeout: "180"` with ingress-nginx. List the
Ingress host, and the Service's name for callers in the cluster, in `auth.allowedHosts`
(the Host header filter against DNS rebinding).

## NetworkPolicy

With `networkPolicy.enabled: true`, the server's pods accept:

- the MCP port from `networkPolicy.ingress.from` only, plus the OpenShift router when
  the Route is enabled;
- the health port from the release's `helm test` pod and `networkPolicy.metrics.from`;

and reach only the cluster DNS, Trino (`networkPolicy.egress.trino`, on `trino.port`),
the identity provider (`networkPolicy.egress.identityProvider`) and
`networkPolicy.egress.extra`. Trino's peers are required, and so are the identity
provider's when the door is on, and the ingress controller's with an Ingress. The health
port admits a pod of the namespace that carries the release's test labels: anyone who
may create pods in the namespace can do so, so keep the release in a namespace of its own.

## Environment

`extraEnv` adds variables, an extension's own settings for example; one the chart
manages (`TRINO_HOST`, `MCP_AUTH_ENABLED`...) refuses to render and names the value to set
instead, so that the render-time checks cannot be bypassed. `extraEnvFrom` comes before
the chart's ConfigMap, so the chart's settings win on a duplicate; it must not carry
those variables either.

## What is proven live, and what is only rendered

[`integration/helm/run.sh`](../../integration/helm/run.sh) installs the chart on a
cluster in two passes:

- **`service` mode**, against a disposable Trino over `http` and a static JWKS: the door
  (401 without a token or with one for another audience), tool calls, a refused write,
  the NetworkPolicy (an unlisted pod reaches neither port), the pod's security, an
  arbitrary UID with group `0`, `helm test` and `--check`;
- **`jwt` mode, the default**, against a disposable Keycloak with token exchange and
  Trino over `https` with JWT authentication: the client secret from `existingSecret`,
  the internal CA for Trino, the token endpoint and the JWKS fetch (`SSL_CERT_FILE`), 401
  without a token or with a token issued for Trino, two users through the Service, each
  seen by Trino as themselves (a row filter applies to one), `helm test` and `--check`.

[`integration/helm/openshift.sh`](../../integration/helm/openshift.sh) runs the same
`service` and `jwt` passes, then `impersonation` with a client certificate over `https`,
in the current project only (a project member's rights, within a quota, no python on the
machine that runs it). On OpenShift it installs an edge Route and checks, through it: the
pod under `restricted-v2` with a UID from the project's range, the `401` that points to
the protected resource metadata, the metadata, the MCP calls, and no health path. Where
the Route API is absent, those checks print `SKIP` and the calls go through the Service.

Only rendered and validated (`helm template`, kubeconform, the render checks):
`impersonation` with a password, the Ingress, `route.tls.externalCertificate`, the
PodDisruptionBudget and the anti-affinity.

## Values

| Value | Meaning | Default |
|---|---|---|
| `image.repository`, `image.tag`, `image.digest` | the image; the tag defaults to the chart's `appVersion`, a digest takes precedence | `akkoai/akko-mcp-trino`, `""`, `""` |
| `replicaCount` | replicas | `1` |
| `openshift` | leave the user and group to the SCC (also detected) | `false` |
| `transport` | `streamable-http` or `sse` (`MCP_TRANSPORT`) | `streamable-http` |
| `ports.mcp`, `ports.health` | listening ports (`MCP_PORT`, `MCP_HEALTH_PORT`) | `3000`, `3001` |
| `server.name` | name announced to hosts (`MCP_SERVER_NAME`) | the server's |
| `server.toolPlugins`, `server.extensions` | plugins and extensions installed in the image (`MCP_TOOL_PLUGINS`, `MCP_EXTENSIONS`) | `[]` |
| `trino.host`, `trino.port` | the coordinator (`TRINO_HOST`, `TRINO_PORT`) | required, `8443` |
| `trino.httpScheme` | `http` or `https` (`TRINO_HTTP_SCHEME`) | `https` |
| `trino.catalog` | session catalog, never `system` (`TRINO_CATALOG`) | required |
| `trino.identityMode` | `jwt`, `impersonation` or `service` (`TRINO_IDENTITY_MODE`) | `jwt` |
| `trino.user` | the server's own account (`TRINO_USER`) | required but in `jwt` |
| `trino.credential` | `password`, `clientCertificate` or `none`, in `impersonation` and `service` | `password` |
| `trino.clientCertificate.secretName`, `certKey`, `keyKey` | the certificate's Secret and keys (`TRINO_CLIENT_CERT`, `TRINO_CLIENT_KEY`) | `""`, `tls.crt`, `tls.key` |
| `trino.verify` | verify Trino's TLS (`TRINO_VERIFY`); with `ca` set, against that bundle | `true` |
| `trino.readOnly` | refuse writes (`TRINO_READ_ONLY`) | `true` |
| `trino.maxRows`, `trino.source`, `trino.queryTimeoutSeconds`, `trino.requestTimeoutSeconds` | `TRINO_MAX_ROWS`, `TRINO_SOURCE`, `TRINO_QUERY_TIMEOUT_SECONDS`, `TRINO_REQUEST_TIMEOUT_SECONDS`; `null` keeps the server's default | `null` |
| `trino.tokenExchange.url`, `clientId`, `audience`, `scope`, `verify` | `jwt`: the token exchange (`TRINO_TOKEN_EXCHANGE_*`) | required, required, required, `""`, `true` |
| `auth.enabled`, `auth.required` | the door (`MCP_AUTH_ENABLED`, `MCP_AUTH_REQUIRED`) | `true`, `true` |
| `auth.acknowledgeNoAuth` | render with the door off (`service` mode, local tests) | `false` |
| `auth.acknowledgeInsecureJwks` | accept a JWKS URL that is not `https` (tests) | `false` |
| `auth.jwksUrl`, `auth.issuer`, `auth.audience`, `auth.resourceUrl` | `MCP_JWKS_URL`, `MCP_OIDC_ISSUER`, `MCP_OIDC_AUDIENCE`, `MCP_RESOURCE_URL` | the JWKS URL and an audience are required with the door on |
| `auth.userClaim`, `auth.algorithms`, `auth.leewaySeconds`, `auth.allowedHosts` | `MCP_JWT_USER_CLAIM`, `MCP_JWT_ALGORITHMS`, `MCP_JWT_LEEWAY_SECONDS`, `MCP_ALLOWED_HOSTS` | the server's |
| `limits.*` | `MCP_CALL_TIMEOUT_SECONDS`, `MCP_MAX_CATALOGS`, `MCP_DESCRIBE_SCHEMA_TABLES`, `MCP_DESCRIBE_SCHEMA_COLUMNS`, `MCP_SEARCH_MIN_LENGTH`, `MCP_SEARCH_MAX_WORDS`, `MCP_SEARCH_MAX_WORD_LENGTH`, `MCP_SEARCH_RESERVE_SECONDS`, `MCP_SEARCH_CONCURRENCY`, `MCP_SEARCH_MAX_THREADS`, `MCP_DESCRIBE_MAX_COLUMNS`, and for `profile_table` `MCP_PROFILE_MODES` (`profileModes`, a list), `MCP_PROFILE_MAX_COLUMNS`, `MCP_PROFILE_SAMPLE_ROWS`, `MCP_PROFILE_VALUE_CHARS`, `MCP_PROFILE_TYPE_CHARS`, `MCP_PROFILE_DESCRIBE_ROWS`, `MCP_PROFILE_CLIENT_TAG`, `MCP_PROFILE_TIMEOUT_SECONDS`, `MCP_PROFILE_MAX_CONCURRENT`; `null` or an empty list keeps the server's default | `null` |
| `extraReadCommands` | read commands the allow-list refuses, opened one by one (`MCP_EXTRA_READ_COMMANDS`): some reads, such as SHOW STATS or EXPLAIN, can show facts beyond a row filter or a column mask; refused by default | `[]` |
| `existingSecret.name`, `existingSecret.keys.*` | the Secret of the credentials and its keys | `""`, `token-exchange-client-secret`, `trino-password` |
| `ca.configMap`, `ca.secret`, `ca.key`, `ca.setSslCertFile` | an internal CA bundle | `""`, `""`, `ca.crt`, `true` |
| `extraEnv`, `extraEnvFrom`, `extraVolumes`, `extraVolumeMounts`, `extraContainers` | added as is; a secret only through `valueFrom` | `[]` |
| `podSecurityContext`, `securityContext` | see [values.yaml](values.yaml) | non-root, read-only, no capability |
| `resources` | requests and limits | `100m`/`128Mi`, `1`/`384Mi` |
| `livenessProbe`, `readinessProbe`, `startupProbe` | `/health` (never touches Trino), `/ready` (reaches Trino), `/health` | on the health port |
| `service.type`, `service.acknowledgePlainHttp` | the MCP Service; a type other than `ClusterIP` needs the acknowledgement | `ClusterIP`, `false` |
| `service.exposeHealthPort`, `service.extraPorts` | the health Service (`<fullname>-health`, always `ClusterIP`), more MCP Service ports | `true`, `[]` |
| `ingress.*` | an Ingress to the MCP port; paths default to the transport's endpoint | off |
| `route.*` | an OpenShift Route, `edge` or `reencrypt`; `route.tls.externalCertificate` names the certificate's Secret | off, `edge` |
| `networkPolicy.*` | see above | off |
| `podDisruptionBudget.enabled`, `minAvailable`, `maxUnavailable` | a PodDisruptionBudget; `minAvailable` (1 when both are `null`) below `replicaCount` | off, `null`, `null` |
| `podAntiAffinity` | `soft` spreads the replicas over nodes when it can | `""` |
| `affinity`, `nodeSelector`, `tolerations`, `topologySpreadConstraints`, `priorityClassName` | scheduling | none |
| `serviceAccount.create`, `name`, `annotations`, `automountServiceAccountToken` | the ServiceAccount | `true`, the full name, `{}`, `false` |
| `tests.enabled`, `tests.ready` | the `helm test` pod, and whether it calls `/ready` | `true`, `true` |

## Development

`helm lint` and `helm template` run in CI on every value set in [`ci/`](ci/) (the
defaults with the required values, OpenShift, impersonation, Trino Gateway), the output
is validated by `kubeconform` against Kubernetes 1.27 to 1.36 and the OpenShift schema of
the Route, and `ct lint` checks the chart.
[`integration/helm/run.sh`](../../integration/helm/run.sh) is the live proof on a cluster
(see above); it removes everything it created.
