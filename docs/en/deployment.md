# Deployment

**English** · [Français](../fr/deployment.md)

## Install

```bash
pip install akko-mcp-trino        # or: pipx install akko-mcp-trino
uvx akko-mcp-trino --version      # run it without installing, the way most MCP hosts do
```

Or from source:

```bash
git clone https://github.com/AKKO-p/akko-mcp-trino.git
cd akko-mcp-trino
python -m venv .venv && source .venv/bin/activate
pip install -e .
```

The package installs a Python module (`akko_mcp_trino`) and a command
(`akko-mcp-trino`); `python -m akko_mcp_trino` is the same entrypoint.

## Run

Point it at your Trino and your identity provider, then serve. The defaults
are the safe ones: each query runs as the caller (`jwt`), and a request without
a valid token is refused.

```bash
export TRINO_HOST=trino.example.internal
export TRINO_PORT=8443
export TRINO_HTTP_SCHEME=https          # required: a token never travels over http
export TRINO_CATALOG=iceberg

# The door: tokens issued for this server, verified against the provider's JWKS
export MCP_JWKS_URL=https://idp.example.com/realms/data/protocol/openid-connect/certs
export MCP_OIDC_ISSUER=https://idp.example.com/realms/data
export MCP_OIDC_AUDIENCE=akko-mcp        # the audience the provider gives tokens for this server
export MCP_RESOURCE_URL=https://mcp.example.com/mcp   # optional: this server's canonical URL

# A token for Trino, by token exchange (RFC 8693), never the caller's own
export TRINO_TOKEN_EXCHANGE_URL=https://idp.example.com/realms/data/protocol/openid-connect/token
export TRINO_TOKEN_EXCHANGE_CLIENT_ID=akko-mcp
export TRINO_TOKEN_EXCHANGE_CLIENT_SECRET=...
export TRINO_TOKEN_EXCHANGE_AUDIENCE=trino

python -m akko_mcp_trino
```

The other identity modes are in [Identity](identity.md). Before serving,
`akko-mcp-trino --check` prints the effective configuration (never a secret), any warning, and
exits with 2 if something would refuse to start; over stdio it checks that `MCP_USER_TOKEN` is
present, not that it is valid (the real start verifies the token). Then:

```
INFO:__main__:serving transport=streamable-http port=3000 auth=True strict=True identity=jwt extensions=none
```

Check it is alive and can reach Trino:

```bash
curl -s localhost:3001/health   # {"status":"ok"}       never touches Trino
curl -s localhost:3001/ready    # {"status":"ready"}    jwt: Trino's /v1/info; impersonation and service: a bounded SELECT 1 as the server's account
curl -s localhost:3001/metrics  # Prometheus exposition
```

## Transports

`streamable-http` (`/mcp`) is the default and what current hosts expect. Set
`MCP_TRANSPORT=sse` for older hosts (`/sse`); the guard is the same on both. With
`MCP_TRANSPORT=stdio`, the host starts the server itself: see
[Local hosts over stdio](hosts.md#local-hosts-over-stdio).

## Container

A [Dockerfile](../../Dockerfile) ships with the repository (non-root, health check,
OCI labels). The image is published for `linux/amd64` and `linux/arm64` as
`akkoai/akko-mcp-trino` and `ghcr.io/akko-p/akko-mcp-trino`. It installs
every dependency from the lock `requirements/image.txt`, at exact versions and
with their hashes (see [CONTRIBUTING.md](../../CONTRIBUTING.md#dependencies-and-locks)).
Run it with the same environment variables, publishing ports `3000` (MCP) and
`3001` (health):

```bash
docker run --rm akkoai/akko-mcp-trino:0.7 akko-mcp-trino --version
```

## Kubernetes and OpenShift

A Helm chart ships in [`charts/akko-mcp-trino/`](../../charts/akko-mcp-trino/): the server with
the door on and identity mode `jwt` by default, a non-root pod with a read-only root
filesystem that also runs under OpenShift's arbitrary UID, secrets read from an existing
Secret, an internal CA, and an optional Ingress, OpenShift Route, NetworkPolicy and
PodDisruptionBudget. `helm template` refuses a setting the server would refuse at start,
or the door turned off without an explicit acknowledgement, and `helm test` runs
`--check` with the release's configuration:

```bash
helm install mcp-trino ./charts/akko-mcp-trino -f my-values.yaml
helm test mcp-trino --logs
```

The [chart's README](../../charts/akko-mcp-trino/README.md) lists its values.

## Behind Trino Gateway

The [sandbox](https://github.com/AKKO-p/akko-sandbox) has a variant with Trino Gateway in front
of two Trino clusters. The MCP server then reaches the gateway only, so every request, every
next page and every cancel goes through the gateway. Its `tests/gateway.sh` checks that each cluster runs the queries as
the user who asked, that a capped result goes through the gateway, and that a query past its
time budget is cancelled in Trino through the gateway.

With a context provider, the statements of the filter and those of the call may reach two
clusters of one routing group: every cluster of a group must share the same access control (the
same plugin and the same rules). A routing group whose clusters apply different rules is not
supported.

## Deployment with a catalogue

The server runs with a data catalogue in one of two modes.

**Mode 1, Trino only.** The server runs without a context provider. A team that already runs
the MCP server of its catalogue keeps it, side by side: nothing in this server blocks or
replaces it. This mode fits when the metadata of the catalogue is not sensitive.

**Mode 2, with a context provider.** Business meaning (descriptions, glossary terms,
classifications, owner) is shown only for the tables and columns Trino lists to the caller.
This mode fits when metadata must follow the rights on the data.

**Rights.** The context follows Trino's rights. The provider reads the catalogue with a
read-only technical account. Descriptions follow what Trino lists to the caller. With
`MCP_CONTEXT_REQUIRE_SELECT=true`, `describe_table` also requires that Trino validates a select
by the caller of the columns it lists, while `describe_schema` keeps serving the descriptions of
the tables Trino lists. It serves glossary terms, classifications and business metadata only
when their glossary, classification or set is named in the allow lists, closed by default, and
the owner only when it is configured (for Atlas: `ATLAS_GLOSSARIES`, `ATLAS_CLASSIFICATIONS`,
`ATLAS_BUSINESS_METADATA`, empty means none, and `ATLAS_OWNER_ATTRIBUTE`). So a glossary or a
classification restricted in the catalogue is never served unless the operator names it. Reading
the catalogue as the caller is not in this version.

Without a provider, no catalogue code is loaded and no catalogue is called.
[Context from a catalogue](catalog-context.md) describes what a provider serves.

## With or without modules

The core ships three modules, `token-policy`, `fingerprint-audit` and `trino-tags`, each off
until `MCP_EXTENSIONS` names it (comma-separated). Without them the server runs as described
here; [Modules](modules.md) says what each one adds.
