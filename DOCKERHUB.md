# akko-mcp-trino

**Give your AI agents read-only access to Trino, with the rights of the user they work for.**

akko-mcp-trino is an [MCP](https://modelcontextprotocol.io) server for Trino, under the
Apache 2.0 licence. It lets an agent list catalogs, schemas and tables, describe a table,
explain a query and run it, and it guarantees that nothing it sends can write: every statement
is parsed, and a write anywhere in it is refused before Trino.

- **The user's own rights, end to end.** In `jwt` mode, the user's verified token goes to Trino,
  which verifies it again and runs the query as that person. Ranger or OPA decide for each user,
  not for a service account. Two users asking the same question get the answers their own rights
  allow.
- **Or one identity, configured by the administrator** (`service` mode, the default).
- **Bounded and audited.** Row and time limits on every call, one audit line per call, and no
  token ever written to the logs.
- **Six tools, no more**, for any MCP host and any model.

Source, documentation and issues: **[github.com/AKKO-p/akko-mcp-trino](https://github.com/AKKO-p/akko-mcp-trino)**

## Tags

| Tag | Meaning |
|---|---|
| `X.Y.Z` (e.g. `0.5.3`) | one release |
| `X.Y` (e.g. `0.5`) | the latest release of that minor line |
| `latest` | the latest release |

Every tag is built for `linux/amd64` and `linux/arm64`, runs as a non-root user (uid 10001),
and carries a health check. The same image is published as `ghcr.io/akko-p/akko-mcp-trino`.

## Run it

Each user with their own rights (`jwt`), Trino trusting the same identity provider:

```bash
docker run -d --name akko-mcp-trino -p 3000:3000 -p 3001:3001 \
  -e TRINO_HOST=trino.example.internal -e TRINO_PORT=8443 -e TRINO_HTTP_SCHEME=https \
  -e TRINO_CATALOG=iceberg -e TRINO_IDENTITY_MODE=jwt \
  -e MCP_AUTH_ENABLED=true -e MCP_AUTH_REQUIRED=true \
  -e MCP_JWKS_URL=https://idp.example.com/realms/data/protocol/openid-connect/certs \
  -e MCP_OIDC_ISSUER=https://idp.example.com/realms/data \
  -e MCP_OIDC_AUDIENCE=data-platform \
  akkoai/akko-mcp-trino:0.5
```

One identity configured by the administrator (`service`): replace `TRINO_IDENTITY_MODE=jwt` by
`-e TRINO_USER=mcp-reader -e TRINO_PASSWORD=...`.

The MCP endpoint is `http://localhost:3000/mcp` (streamable HTTP); the host sends the user's
access token as `Authorization: Bearer <token>`. Then:

```bash
curl -s localhost:3001/health   # {"status":"ok"}
curl -s localhost:3001/ready    # {"status":"ready"} once Trino answers
docker run --rm akkoai/akko-mcp-trino:0.5 akko-mcp-trino --version
```

Before serving, `akko-mcp-trino --check` prints the effective configuration (never a secret)
and exits with 2 if something would refuse to start:

```bash
docker run --rm -e TRINO_HOST=... -e TRINO_IDENTITY_MODE=jwt -e TRINO_HTTP_SCHEME=https \
  akkoai/akko-mcp-trino:0.5 akko-mcp-trino --check
```

Every setting, the Trino configuration for `jwt`, local hosts over stdio and the extension
points are described in the [README](https://github.com/AKKO-p/akko-mcp-trino#readme) and on
[akko-ai.com](https://akko-ai.com/en/docs/open-source-core/).

## About

Built and maintained by [AKKO](https://akko-ai.com), a French project working on governed access
for AI agents to enterprise data. Security reports: security@akko-ai.com.
