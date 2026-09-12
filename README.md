# akko-mcp-trino

**A governed MCP server for Trino.** Every tool call an agent makes carries the
identity of the person it acts for, and the decision is taken where the data
lives — by Trino under OPA or Ranger — not by the server in front of it.

The promise, for whoever runs the platform: *your agents see exactly what the
user is allowed to see, on the Trino you already have, whatever your identity
provider.*

Apache 2.0. Python 3.12. Five tools, no free-form writes, no service account
doing the reading on the user's behalf.

## What it does

An MCP host — Cursor, Claude, VS Code, a LangGraph agent — presents a bearer
token on each request. The server verifies it against your OIDC provider's
JWKS (issuer, audience, expiry, pinned keys), extracts the subject, and forwards
that subject to Trino as `X-Trino-User`. Trino then enforces whatever your
engine-side policy says: catalog scope, row filters, column masks. The SQL an
agent writes can be anything the engine accepts; **it never reads what the user
could not read themselves.**

| Tool | What it returns |
|---|---|
| `list_catalogs` | the catalogs the user can see |
| `list_schemas(catalog)` | schemas in a catalog |
| `list_tables(catalog, schema)` | tables in a schema |
| `describe_table(catalog, schema, table)` | columns and types |
| `execute_query(sql)` | rows, bounded; read-only by default |

Identifiers are validated before they reach SQL. `execute_query` refuses
anything that is not a read when `read_only` is on, which it is by default.
Results are capped by `max_rows`.

## What it deliberately does not do

- It does not impersonate. If no verified identity is present and strict mode is
  on, the request is refused with `401`. There is no "run as the service
  account" fallback in strict mode.
- It does not decide access. OPA, Ranger, or Trino's own access control decide.
  This server carries identity; it is not a policy engine.
- It does not translate to a model, meter tokens, or multiplex other MCP
  servers. Put an AI gateway on the model hop if you need one; this sits on the
  lakehouse hop.

## How this compares to Cloudera's CDP Agent Gateway

Cloudera published a blueprint for the same category on 10 September 2026
([BrooksIan/CDPAgentGateway](https://github.com/BrooksIan/CDPAgentGateway),
Apache 2.0): APISIX in front of Knox, with MCP adapters for Spark, Hive and
Impala that forward the caller's Knox JWT so Ranger authorises the subject. It
is the precedent for this category and it sets the vocabulary — end user vs
agent platform, RFC 9728 discovery, request-id audit. This project takes that
vocabulary as given and covers what the blueprint states it does not.

| | CDP Agent Gateway | akko-mcp-trino |
| --- | --- | --- |
| Engine | Spark, Hive, Impala, through Knox | Trino — federated, one SQL across sources |
| Identity provider | Knox only (`iss` must be `KNOXSSO`) | Any OIDC issuer, verified by JWKS |
| Who decides | Ranger on the Knox subject | OPA or Ranger, inside the engine |
| SQL surface | Structured `select` only: columns required, no `WHERE`, 50 rows | Read-only free SQL, bounded rows — governance lives in the engine |
| Token exchange | Not published ("Knox is not an OIDC authorization server") | Planned as a separate broker (RFC 8693) |
| Catalog → policy | — | Planned (`akko-policy-sync`) |
| Proof | — | Planned (`akko-bench`): two accounts, two answers |

The SQL-surface difference is a choice, not an oversight. Without free SQL there
is no join between an Iceberg catalog and a Kerberised Hive, hence no
federation, hence no reason to put Trino there. The guarantee does not come from
the shape of the query; it comes from the engine enforcing policy under the
user's identity. The blueprint's belt-and-braces guards — refusing `SELECT *`,
capping rows, validating identifiers — are kept or on the roadmap.

## Configuration

Everything is environment-driven; nothing is hardcoded.

| Variable | Meaning | Default |
|---|---|---|
| `TRINO_HOST`, `TRINO_PORT` | the coordinator | — |
| `TRINO_USER` | fallback identity when auth is **not** required | — |
| `TRINO_CATALOG` | default catalog | — |
| `MCP_READ_ONLY` | refuse writes in `execute_query` | `true` |
| `MCP_MAX_ROWS` | result cap | `100` |
| `MCP_AUTH_ENABLED` | verify bearer tokens | `true` |
| `MCP_AUTH_REQUIRED` | refuse requests without a verified identity | `false` |
| `MCP_JWKS_URL` | your IdP's JWKS endpoint | — (required when auth is enabled) |
| `MCP_OIDC_ISSUER`, `MCP_OIDC_AUDIENCE` | claims to enforce | — |
| `MCP_TRANSPORT` | `sse` or `streamable-http` | `sse` |
| `MCP_PORT`, `MCP_HEALTH_PORT` | listening ports | `3000`, `3001` |

Auth enabled without a JWKS URL refuses to start. That is on purpose: an
authentication layer that cannot verify anything must not pretend to.

## Guarantees the tests hold

96 tests, 100 % coverage, and a lint that fails the build if the core ever
imports a product-specific module. The guard that matters most is on the
transport: **the identity middleware is mounted on whichever transport is
served**, and a test asserts it for both. It once lived on a branch the default
transport never took, and the server then answered every call under its
service account. That is the class of defect this project exists to make
impossible.

## Roadmap, in the blueprint's terms

1. Dual identity: `X-Agent-Key` for the agent product, distinct from the user.
2. RFC 9728 protected-resource metadata and `401` with `resource_metadata`.
3. Rate limits per user and per agent.
4. Request-id audit join — tool, subject, token id, never the raw token.
5. Optional token-state check for revocation before expiry.

## Status

Extracted on 12 September 2026 from the AKKO platform, where it has run in
production since July. Private until the naming review is done; the code is
ready to be public.
