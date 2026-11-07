# Deployment

**English** · [Français](../fr/deployment.md)

## Install

```bash
pip install akko-mcp-trino        # or: pipx install akko-mcp-trino
uvx akko-mcp-trino --version      # run it without installing, the way most MCP hosts do
```

You can also install from source:

```bash
git clone https://github.com/AKKO-p/akko-mcp-trino.git
cd akko-mcp-trino
python -m venv .venv && source .venv/bin/activate
pip install -e .
```

The package installs a Python module (`akko_mcp_trino`) and a command (`akko-mcp-trino`).
`python -m akko_mcp_trino` runs the same entrypoint.

## Run

Point the server at your Trino and your identity provider, then start it. The defaults are the
safe ones: each query runs as the caller (`jwt`), and the server refuses a request without a
valid token.

```bash
export TRINO_HOST=trino.example.internal
export TRINO_PORT=8443
export TRINO_HTTP_SCHEME=https          # required: a token never travels over http
export TRINO_CATALOG=iceberg

# Token verification: tokens issued for this server, checked against the IdP's JWKS
export MCP_JWKS_URL=https://idp.example.com/realms/data/protocol/openid-connect/certs
export MCP_OIDC_ISSUER=https://idp.example.com/realms/data
export MCP_OIDC_AUDIENCE=akko-mcp        # the audience the IdP gives tokens for this server
export MCP_RESOURCE_URL=https://mcp.example.com/mcp   # optional: this server's canonical URL
export MCP_JWT_USER_CLAIM=preferred_username          # Keycloak; the default is sub

# A token for Trino, by token exchange (RFC 8693), never the caller's own
export TRINO_TOKEN_EXCHANGE_URL=https://idp.example.com/realms/data/protocol/openid-connect/token
export TRINO_TOKEN_EXCHANGE_CLIENT_ID=akko-mcp
read -rs TRINO_TOKEN_EXCHANGE_CLIENT_SECRET && export TRINO_TOKEN_EXCHANGE_CLIENT_SECRET
export TRINO_TOKEN_EXCHANGE_AUDIENCE=trino

python -m akko_mcp_trino
```

`read -rs` reads the client secret without showing it, so it stays out of your shell history.
[Identity](identity.md) covers the other identity modes. Before you start the server,
`akko-mcp-trino --check` prints the effective configuration (never a secret) and any warning. It
exits with status 2 if a setting would stop the server from starting. Over stdio, it checks that
`MCP_USER_TOKEN` is set, not that it is valid; the real start verifies the token. Once started,
the server logs:

```
INFO:__main__:serving transport=streamable-http port=3000 auth=True strict=True identity=jwt extensions=none
```

The logger name depends on how you start the server: `__main__` with `python -m akko_mcp_trino`,
`akko_mcp_trino.__main__` with the `akko-mcp-trino` command.

Check that it is alive and can reach Trino:

```bash
curl -s localhost:3001/health   # {"status":"ok"}       never touches Trino
curl -s localhost:3001/ready    # {"status":"ready"}    jwt: Trino's /v1/info; impersonation and service: a bounded SELECT 1 as the server's account
curl -s localhost:3001/metrics  # Prometheus exposition
```

When Trino does not answer, `/ready` returns `503` with `{"status": "error", "detail": ...}`, the
detail being the error the check raised.

## Transports

`streamable-http` (`/mcp`) is the default, and what current hosts expect. Set
`MCP_TRANSPORT=sse` for older hosts (`/sse`); token verification is the same on both. With
`MCP_TRANSPORT=stdio`, the host starts the server itself: see
[Any host over stdio, with your own token](hosts.md#any-host-over-stdio-with-your-own-token).

## Container

A [Dockerfile](../../Dockerfile) ships with the repository: it runs as a non-root user, with a
health check and OCI labels. The image is published for `linux/amd64` and `linux/arm64` as
`akkoai/akko-mcp-trino` and `ghcr.io/akko-p/akko-mcp-trino`. It installs every dependency from
the lock `requirements/image.txt`, at exact versions and with their hashes (see
[CONTRIBUTING.md](../../CONTRIBUTING.md#dependencies-and-locks)).

Each user with their own rights (`jwt`, the default), with Trino trusting the same identity
provider:

```bash
read -rs TRINO_TOKEN_EXCHANGE_CLIENT_SECRET && export TRINO_TOKEN_EXCHANGE_CLIENT_SECRET
docker run -d --name akko-mcp-trino -p 3000:3000 -p 3001:3001 \
  -e TRINO_HOST=trino.example.internal -e TRINO_PORT=8443 -e TRINO_HTTP_SCHEME=https \
  -e TRINO_CATALOG=iceberg \
  -e MCP_JWKS_URL=https://idp.example.com/realms/data/protocol/openid-connect/certs \
  -e MCP_OIDC_ISSUER=https://idp.example.com/realms/data \
  -e MCP_OIDC_AUDIENCE=akko-mcp -e MCP_JWT_USER_CLAIM=preferred_username \
  -e TRINO_TOKEN_EXCHANGE_URL=https://idp.example.com/realms/data/protocol/openid-connect/token \
  -e TRINO_TOKEN_EXCHANGE_CLIENT_ID=akko-mcp -e TRINO_TOKEN_EXCHANGE_CLIENT_SECRET \
  -e TRINO_TOKEN_EXCHANGE_AUDIENCE=trino \
  akkoai/akko-mcp-trino:0.7
```

Without token exchange, use `-e TRINO_IDENTITY_MODE=impersonation -e TRINO_USER=mcp-service`, with
a password or a client certificate for that account. For one identity for every caller, read the
password with `read -rs TRINO_PASSWORD && export TRINO_PASSWORD`, then use
`-e TRINO_IDENTITY_MODE=service -e TRINO_USER=mcp-reader -e TRINO_PASSWORD`. An `-e` without a
value passes the variable of your shell, so no secret is typed on the command line, where the shell
history would keep it.

The MCP endpoint is `http://localhost:3000/mcp` (streamable HTTP). The host sends the user's
access token as `Authorization: Bearer <token>`. Then check the server:

```bash
curl -s localhost:3001/health   # {"status":"ok"}
curl -s localhost:3001/ready    # {"status":"ready"} once Trino responds
docker run --rm akkoai/akko-mcp-trino:0.7 akko-mcp-trino --version
```

`--check` works in a container too:

```bash
docker run --rm -e TRINO_HOST=... -e TRINO_HTTP_SCHEME=https -e MCP_JWKS_URL=... \
  akkoai/akko-mcp-trino:0.7 akko-mcp-trino --check
```

## Kubernetes and OpenShift

A Helm chart ships in [`charts/akko-mcp-trino/`](../../charts/akko-mcp-trino/). By default it runs
the server with token verification on and identity mode `jwt`. The pod runs as a non-root user,
with a read-only root filesystem, and also runs under OpenShift's arbitrary UID. Secrets are read
from an existing Secret. The chart can also add an internal CA, an Ingress, an OpenShift Route, a
NetworkPolicy and a PodDisruptionBudget.

`helm template` fails on a setting the server would refuse at start, and on token verification
turned off without an explicit acknowledgement. `helm test` runs `--check` with the release's
configuration:

```bash
helm install mcp-trino ./charts/akko-mcp-trino -f my-values.yaml
helm test mcp-trino --logs
```

The [chart's README](../../charts/akko-mcp-trino/README.md) lists its values.

## Behind Trino Gateway

The gateway terminates TLS, so a client certificate does not reach the cluster. Behind the
gateway, the server's account in `impersonation` mode authenticates with a password
(`TRINO_PASSWORD`, or `trino.credential: password` in the chart), which the gateway forwards. The
chart's [Behind Trino Gateway](../../charts/akko-mcp-trino/README.md#behind-trino-gateway) section
and [`ci/gateway-values.yaml`](../../charts/akko-mcp-trino/ci/gateway-values.yaml) give an
example.

The [sandbox](https://github.com/AKKO-p/akko-sandbox) has a variant with Trino Gateway in front
of two Trino clusters. The MCP server then reaches only the gateway, so every request, every
next-page request and every cancellation goes through it. The sandbox runs it with:

```bash
COMPOSE_FILE=compose.yaml:gateway/compose.gateway.yaml docker compose up -d --wait
COMPOSE_FILE=compose.yaml:gateway/compose.gateway.yaml ./demo.sh --check
COMPOSE_FILE=compose.yaml:gateway/compose.gateway.yaml docker compose --profile tools down -v
tests/gateway.sh   # the checks, end to end
```

Start the variant with the same `COMPOSE_FILE` every time, and stop it with it too: a
`docker compose down` without it leaves the second cluster and the gateway behind.

`tests/gateway.sh` checks that each cluster runs the queries as the user who asked, that a
capped result goes through the gateway, and that a query past its time budget is cancelled in
Trino through the gateway. In that sandbox, each cluster trusts `X-Forwarded-Proto` from any peer
on its network. In production, nothing but the gateway should reach a cluster.

With a context provider, the statements of the visibility filter and those of the call may reach
two clusters of one routing group. Every cluster of a group must therefore share the same access
control: the same plugin and the same rules. A routing group whose clusters apply different rules
is not supported.

## With a data catalog

There are two ways to run the server next to a data catalog.

**Mode 1, Trino only.** The server runs without a context provider. A team that already runs the
MCP server of its data catalog keeps it, side by side: nothing in this server blocks or replaces
it. This mode fits when the metadata of the data catalog is not sensitive.

**Mode 2, with a context provider.** Business meaning (descriptions, glossary terms,
classifications, owner) is shown only for the tables and columns Trino lists to the caller. This
mode fits when metadata must follow the rights on the data.

**Rights.** The context follows Trino's rights. The provider reads the data catalog with a
read-only technical account. Descriptions follow what Trino lists to the caller. With
`MCP_CONTEXT_REQUIRE_SELECT=true`, `describe_table` also asks Trino to confirm that the caller may
select the columns it lists. `describe_schema` keeps serving the descriptions of the tables Trino
lists. The provider serves glossary terms, classifications and business metadata only when their
glossary, classification or set is named in an allow list. The allow lists are empty by default.
The owner is served only when it is configured (for Atlas: `ATLAS_GLOSSARIES`,
`ATLAS_CLASSIFICATIONS`, `ATLAS_BUSINESS_METADATA`, empty means none, and
`ATLAS_OWNER_ATTRIBUTE`). A restricted glossary or classification is therefore never served
unless the operator names it. This version cannot read the data catalog as the caller.

Without a provider, no data catalog code is loaded and no data catalog is called.
[Context from a data catalog](catalog-context.md) describes what a provider serves.

### On Kubernetes and OpenShift

The chart has no value of its own for the context: its variables go in `extraEnv`, a secret only
through `valueFrom`, and the `file` provider's JSON file in `extraVolumes` and `extraVolumeMounts`.
The published images (0.7.x) do not carry the Atlas provider. Build the image from `main`,
which installs it inactive, without the Kerberos libraries. For Atlas with Basic authentication:

```yaml
extraEnv:
  - name: MCP_CONTEXT_PROVIDERS
    value: atlas
  - name: MCP_CONTEXT_CANARY
    value: <catalog.schema.table>   # a table no caller may see
  - name: ATLAS_URL
    value: https://atlas.example.internal
  - name: ATLAS_CLUSTERS
    value: '{"lake": "primary"}'
  - name: ATLAS_USER
    value: mcp-reader
  - name: ATLAS_PASSWORD
    valueFrom:
      secretKeyRef:
        name: mcp-trino-atlas
        key: password
```

For the `file` provider:

```yaml
extraEnv:
  - name: MCP_CONTEXT_PROVIDERS
    value: file
  - name: MCP_CONTEXT_FILE
    value: /etc/mcp-trino/context/context.json
extraVolumes:
  - name: context
    configMap:
      name: mcp-trino-context
extraVolumeMounts:
  - name: context
    mountPath: /etc/mcp-trino/context
    readOnly: true
```

## With or without modules

The core ships three modules, `token-policy`, `fingerprint-audit` and `trino-tags`. Each is off
until `MCP_EXTENSIONS` names it (comma-separated). Without them, the server runs as described on
this page. [Modules](modules.md) says what each one adds.

### On Kubernetes and OpenShift

The chart names the modules in `server.extensions`. Their own settings go in `extraEnv`:

```yaml
server:
  extensions: [token-policy, fingerprint-audit]
extraEnv:
  - name: MCP_ALLOWED_CLIENTS
    value: mcp-host
  - name: MCP_REQUIRED_SCOPES
    value: data:query
```
