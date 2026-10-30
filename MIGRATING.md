# Migrating

## From 0.6 to 0.7

Nothing is removed or renamed. One helper for plugins changes its third argument, and the extension guards run differently in `oauth2` mode.

- Every error reply gains a `kind` key, and an `identifier` key when Trino names the object it could not resolve. `error` is exactly the string it was, and the audit codes are the same. A client that reads only `error` keeps working; one that wants to act on the failure can read `kind` (`syntax`, `column`, `table`, `schema`, `catalog`, `function`, `type`, `permission`, `invalid_argument`, `read_only`, `time_budget`, `unavailable`, `other`).
- A plugin that builds its error with `akko_mcp_trino.tools.error_reply` gets the `kind` for free. `refusal_reply(code, message)` takes an optional `kind`, `invalid_argument` by default.
- `list_catalogs`, `list_schemas` and `list_tables` answer one page at a time and gain a `next_offset` key: the `offset` to call again with for the next page, or `null` on the last one. `truncated` is kept, and is true exactly when `next_offset` is not `null`. A client that read only the first page keeps working; one that wants the rest calls again with `offset=next_offset`. The advice changes: a `truncated` list is no longer the end, read on with `next_offset`.
- `list_schemas` and `list_tables` read `information_schema.schemata` and `information_schema.tables` instead of `SHOW SCHEMAS` and `SHOW TABLES`. Trino filters `information_schema` by the caller's rights, so a schema the caller may not see shows no row, as a schema that does not exist. `list_tables` checks an empty page against the schemas the caller sees and answers `{"error": "Schema <catalog>.<schema> not found or not visible", "kind": "schema"}` for both, as `SHOW TABLES` failed before; a visible empty schema answers an empty list.
- `list_catalogs` reads at most `MCP_MAX_CATALOGS` catalogs (10000 by default) and answers an error above it: raise the bound if a deployment shows more.
- A new tool, `describe_schema`, reads `system.jdbc.columns` and `system.metadata.table_comments`, by statements the server builds. The advice on the engine's policy changes: refuse the `system` catalog **except** reading `system.jdbc` and `system.metadata`, which Trino filters for each user as it does `information_schema`. A policy that refuses them keeps every other tool working; `describe_schema` then answers an error that names what to allow. The agent's SQL is still refused on all of `system`.
- A new tool, `search_columns`, reads `system.jdbc.columns` the same way, and is the first tool an agent is told to call: `search_columns` to find where the data lives, then `describe_schema`, then `execute_query`. Its answer is complete only when `complete` is true; otherwise `not_searched` says what was left and why, and `next_cursor` resumes the search. It needs a call budget (`MCP_CALL_TIMEOUT_SECONDS` above 0, the default) to search every catalog; without one it searches a named catalog only, and the server warns of it at start, in `--check` and in its initialization instructions. `MCP_SEARCH_RESERVE_SECONDS` must stay below `MCP_CALL_TIMEOUT_SECONDS`, or the server refuses to start; set to 0 it is warned of, since a search may then end in the call's own `time_budget` error.
- The `initialize` result now carries the package version as the server's version (it was empty). The server name is unchanged.
- `MCP_DESCRIBE_SCHEMA_TABLES` (tables per `describe_schema` page) is 20 by default and at most 100; a larger value, like a value below 1, refuses to start. `MCP_DESCRIBE_SCHEMA_COLUMNS` (1000), `MCP_SEARCH_RESERVE_SECONDS` (2), `MCP_SEARCH_MIN_LENGTH` (3) and `MCP_MAX_CATALOGS` (10000) are new, with defaults that need no change.
- With `TRINO_IDENTITY_MODE=oauth2` over stdio, the extension guards (`before_auth`, `after_auth`) no longer run once at start: they run on every request that is not discovery (`initialize`, `ping`, the `*/list` methods, notifications), after Trino named the user, so `before_auth` already sees the `principal`, and a binding lasts one request. A guard that refuses no longer keeps the server from starting: it refuses each call, with an `INVALID_REQUEST` error that carries the guard's reason and `message`. A guard that relied on running at start, or without a principal, must be adapted.
- `TRINO_IDENTITY_MODE=oauth2` no longer opens the browser at start: the login waits for the first request that needs Trino. Only the identity check (`SELECT current_user`) runs before a user is admitted.
- `safe_sql_string` keeps a backslash as it is: a plugin that relied on it being doubled built a different value than the one it meant.
- A plugin that called `listing_reply(key, items, truncated)` passes the next page's offset, or `None`, instead: `listing_reply(key, items, None)` for a complete list. A bool is refused with `TypeError`, so an old call fails loudly rather than answer a wrong `truncated`.

## From 0.5 to 0.6

0.6.0 makes the defaults safe and stops passing the caller's token on to Trino.
A 0.5 configuration that relied on the defaults, or on `jwt` mode, no longer
starts as it is: `akko-mcp-trino --check` says what is missing. Nothing changes
in the six tools, the read-only guard or the audit line.

### Breaking changes

| What | 0.5 | 0.6 |
|---|---|---|
| `TRINO_IDENTITY_MODE` default | `service` (every query as `TRINO_USER`) | `jwt` (every query as the caller) |
| `MCP_AUTH_ENABLED` default | `false` | `true` |
| `MCP_AUTH_REQUIRED` default | `false` | `true` |
| `jwt` mode | sends Trino the caller's own token | sends Trino a token obtained by token exchange (RFC 8693); needs `TRINO_TOKEN_EXCHANGE_*` |
| Accepted token audience | `MCP_OIDC_AUDIENCE`, which could be Trino's | this server's own (`MCP_OIDC_AUDIENCE` or `MCP_RESOURCE_URL`); a token for Trino is refused, and so is a configuration that would accept one |
| Auth on without an audience | started, refused every token | refuses to start |
| `MCP_AUTH_REQUIRED=true` with `MCP_AUTH_ENABLED=false` | started, refused every request | refuses to start |
| `jwt` or `impersonation` with the door open | `jwt` started, failed each query | refuses to start |
| `TRINO_QUERY_TIMEOUT_SECONDS` default | `0` (no limit) | `60` |
| `MCP_CALL_TIMEOUT_SECONDS` default | `0` (no limit) | `120` |
| `TRINO_MAX_ROWS=0`, negative budgets | accepted | refuse to start |
| `execute_query` annotations with `TRINO_READ_ONLY=false` | read-only | `readOnlyHint: false`, `destructiveHint: true` |
| Custom `mcp_factory` (Python) | `factory(name)` | also `factory(name, instructions=...)` when the configuration has warnings |

### You ran `service` mode (the 0.5 default)

Name it, and keep the door you had:

```bash
export TRINO_IDENTITY_MODE=service
# with a door (recommended):
export MCP_JWKS_URL=... MCP_OIDC_ISSUER=... MCP_OIDC_AUDIENCE=<this server's audience>
# or without one, locally only:
export MCP_AUTH_ENABLED=false MCP_AUTH_REQUIRED=false
```

The server now warns, at start and to every host, that every query runs as
`TRINO_USER`. Consider `jwt` or `impersonation`, so that Trino decides for each
user.

### You ran `jwt` mode

In 0.5 the caller's token went to Trino, so its audience was usually Trino's
(`MCP_OIDC_AUDIENCE=trino`, say). In 0.6 the token is issued for this server and
exchanged for one for Trino.

1. At the identity provider, create a confidential client for this server (say
   `akko-mcp`), allowed to exchange tokens, with Trino's audience available to
   it. With Keycloak 26: *Client authentication* on, *Standard token exchange*
   on, and an audience mapper for the Trino client.
2. Have the clients of your MCP hosts issue tokens whose audience is this
   server (an audience mapper for `akko-mcp`), and no longer Trino's.
3. Configure the server:

   ```bash
   export MCP_OIDC_AUDIENCE=akko-mcp
   export TRINO_TOKEN_EXCHANGE_URL=https://idp.example.com/realms/data/protocol/openid-connect/token
   export TRINO_TOKEN_EXCHANGE_CLIENT_ID=akko-mcp
   export TRINO_TOKEN_EXCHANGE_CLIENT_SECRET=...
   export TRINO_TOKEN_EXCHANGE_AUDIENCE=trino
   export TRINO_TOKEN_EXCHANGE_VERIFY=/path/to/ca.pem   # if the provider's CA is not a public one
   ```

4. Trino keeps its JWT authenticator and its `required-audience`: it now
   receives tokens for that audience from the exchange.

If your provider cannot exchange tokens, use `impersonation` instead
(below). Nothing falls back to it, or to `service`, by itself.

### Your provider cannot exchange tokens: `impersonation`

```bash
export TRINO_IDENTITY_MODE=impersonation
export TRINO_USER=mcp-service
export TRINO_CLIENT_CERT=/certs/mcp-service.crt   # or TRINO_PASSWORD, over https
export TRINO_CLIENT_KEY=/certs/mcp-service.key
```

In Trino, authenticate that account (certificate or password authenticator)
and let it, and only it, impersonate the users agents act for:

```json
{"impersonation": [{"original_user": "mcp-service", "new_user": ".*", "allow": true}]}
```

### You relied on unbounded queries

Set `TRINO_QUERY_TIMEOUT_SECONDS=0` and `MCP_CALL_TIMEOUT_SECONDS=0` to keep
0.5's behaviour, or larger values. `--check` prints the budgets in force.

### You wrote an extension or embed the server

- An identity mode that sends Trino a token for the caller must obtain one for
  Trino, never forward the caller's. `JwtIdentity.trino_token(bearer)` now
  performs the exchange; an override must not return `bearer`.
- A custom `mcp_factory` receives `instructions=` when the configuration has
  warnings.
- `Config` has new fields with defaults; code that builds a `Config` by hand
  gets `trino_identity_mode="jwt"` unless it names another mode.
