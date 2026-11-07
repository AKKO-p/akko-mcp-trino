# akko-mcp-trino

**Give your AI agents read-only access to Trino, and nothing more.**

akko-mcp-trino is an [MCP](https://modelcontextprotocol.io) server for Trino, under the
Apache 2.0 license. An agent can use it to find tables and columns, describe a schema or a table,
list catalogs, schemas and tables, and explain or run a query. The server parses every statement
and refuses any write before it reaches Trino.

- **Per-user rights by default.** In `jwt` mode, the default, the server verifies the user's
  token (issued for this server), exchanges it at the identity provider for a token whose
  audience is Trino (OAuth 2.0 Token Exchange, RFC 8693), and Trino runs the query as that
  user. The user's token is never passed on, and a request without a valid token gets `401`.
- **Without token exchange**, `impersonation` mode uses the server's own account (password or
  client certificate) and a Trino impersonation rule that only this account has.
- **Or one identity, only when named** (`service` mode), announced as a warning at start and
  to every host.
- **Bounded and audited.** 100 rows, 60 s per query, 120 s per tool call by default, one audit
  line per call, and no token ever written to the logs.
- **Nine tools**, for any MCP host and any model: `search_columns` (where the data lives),
  `describe_schema`, `describe_table`, `list_catalogs`, `list_schemas` and `list_tables` (paged),
  `profile_table` (aggregates of the rows you may read), `explain_query` and `execute_query`. Every error carries a `kind` that says what to do about it.

Source, documentation and issues: **[github.com/AKKO-p/akko-mcp-trino](https://github.com/AKKO-p/akko-mcp-trino)**

## Understand in five steps

![In an illustrative setup with a service account with broad rights, Trino sees mcp-svc and returns 10 customers with unmasked emails. With akko-mcp-trino in its default jwt mode, Trino sees carol_analyst and returns 5 European customers with masked emails.](https://raw.githubusercontent.com/AKKO-p/akko-mcp-trino/v0.7.0/docs/images/understand-1-problem-light.svg)

An agent connected through one service account with broad rights reaches Trino under that
account, so the rules written for Carol no longer apply (the left panel is an illustration).
akko-mcp-trino brings Carol's own identity to Trino.

![alice_admin sees the 10 customers unmasked, carol_analyst the 5 European customers with masked emails, and dave_viewer is refused by Trino (Access Denied).](https://raw.githubusercontent.com/AKKO-p/akko-mcp-trino/v0.7.0/docs/images/understand-2-who-sees-what-light.svg)

The same question from the three users of the sandbox. Trino applies the sandbox's OPA
policy to each of them; the server copies no rule.

![A real trial of Mistral on 0.7.0: a search by four words matched word by word, a wrong join, the server's hint, describe_table, the fixed join and the answer.](https://raw.githubusercontent.com/AKKO-p/akko-mcp-trino/v0.7.0/docs/images/understand-3-step-by-step-light.svg)

A real trial, tool call by tool call: every tool answer shown is the server's.

![The four identity modes on the same grid: jwt by default, impersonation, oauth2 on a workstation with Trino's OAuth2 login, and service, one account for everyone, marked in amber.](https://raw.githubusercontent.com/AKKO-p/akko-mcp-trino/v0.7.0/docs/images/understand-4-identity-modes-light.svg)

Four identity modes, from the safest to the simplest, and three questions to pick yours.

![Mistral answers correctly 62.5% of the time on 0.6.1 and 70% on 0.7.0, Claude 93.3% on both. Mistral goes from 5.8 to 3.0 tool calls per question, Claude from 8.5 to 4.9.](https://raw.githubusercontent.com/AKKO-p/akko-mcp-trino/v0.7.0/docs/images/understand-5-measurements-light.svg)

The same five business questions, put to Mistral and to Claude, on 0.6.1 and on 0.7.0. Mistral
varies more from one run to the next than the gap between the versions, and 0.7.0 sends about 41%
more input tokens per question (longer tool descriptions). The interactive version of these steps is on
[akko-ai.com/en/docs/understand](https://akko-ai.com/en/docs/understand/).

Try it in five minutes with the [sandbox](https://github.com/AKKO-p/akko-sandbox): Keycloak,
Trino, OPA and this image, with the three users above.

```bash
git clone https://github.com/AKKO-p/akko-sandbox.git
cd akko-sandbox
docker compose up -d --wait
./demo.sh
```

## Tags

| Tag | Meaning |
|---|---|
| `X.Y.Z` (e.g. `0.7.2`) | one release |
| `X.Y` (e.g. `0.7`) | the latest release of that minor line |
| `latest` | the latest release |

Every tag is built for `linux/amd64` and `linux/arm64`, runs as a non-root user (uid 10001),
and carries a health check. The same image is published as `ghcr.io/akko-p/akko-mcp-trino`.

## Run it

Each user with their own rights (`jwt`, the default), Trino trusting the same identity
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
`-e TRINO_IDENTITY_MODE=service -e TRINO_USER=mcp-reader -e TRINO_PASSWORD`. `read -rs` keeps each
secret out of your shell history, and an `-e` without a value passes it from your shell.

The MCP endpoint is `http://localhost:3000/mcp` (streamable HTTP); the host sends the user's
access token as `Authorization: Bearer <token>`. Then:

```bash
curl -s localhost:3001/health   # {"status":"ok"}
curl -s localhost:3001/ready    # {"status":"ready"} once Trino responds
docker run --rm akkoai/akko-mcp-trino:0.7 akko-mcp-trino --version
```

Before serving, `akko-mcp-trino --check` prints the effective configuration (never a secret)
and exits with status 2 if a setting would stop the server from starting:

```bash
docker run --rm -e TRINO_HOST=... -e TRINO_HTTP_SCHEME=https -e MCP_JWKS_URL=... \
  akkoai/akko-mcp-trino:0.7 akko-mcp-trino --check
```

Every setting, the Trino configuration for each mode, local hosts over stdio and the extension
points are described in the [documentation](https://github.com/AKKO-p/akko-mcp-trino/blob/main/docs/en/index.md)
([configuration](https://github.com/AKKO-p/akko-mcp-trino/blob/main/docs/en/configuration.md),
[identity](https://github.com/AKKO-p/akko-mcp-trino/blob/main/docs/en/identity.md),
[hosts](https://github.com/AKKO-p/akko-mcp-trino/blob/main/docs/en/hosts.md)) and on
[akko-ai.com](https://akko-ai.com/en/docs/open-source-core/).

## Upgrading

From 0.7.0: 0.7.1 and 0.7.2 are security releases. With Keycloak, set
`MCP_JWT_USER_CLAIM=preferred_username` before upgrading. From 0.6 or earlier, see
[MIGRATING.md](https://github.com/AKKO-p/akko-mcp-trino/blob/main/MIGRATING.md).

## About

Built and maintained by [AKKO](https://akko-ai.com), a French project working on governed access
for AI agents to enterprise data. Security reports: security@akko-ai.com.
