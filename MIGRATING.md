# Migrating

## From 0.7 to 0.8 (unreleased)

Nothing is removed or renamed in the answers of the tools.

- Without `MCP_CONTEXT_PROVIDERS`, `describe_table` and `describe_schema` answer and describe themselves exactly as in 0.7.2.
- With a context provider, `describe_table` adds six keys to its answer: `table`, `column_context`, `terms`, `context_sources`, `context_truncated` and `undescribed_columns`, and its description adds one sentence saying that this text comes from a catalogue and is data, never instructions. `columns`, `rows`, `truncated` and `sample` are unchanged. A client that reads only those keeps working. A key of the same name that a plugin's describer added is replaced by the context's.
- With a context provider, each table of a `describe_schema` page may add `description`, `{"text", "source"}` (with `"cut": ["text"]` when the text was cut), and the answer adds `context_sources`; its description adds one sentence. `catalog`, `schema`, `tables` and their other keys, `next_offset` and `shortened` are unchanged.
- A plugin's describer that raises was silent; `describe_table` now adds `describer_errors`, the number of describers that failed, never their text. The columns are still answered.
- With a context provider, `MCP_CONTEXT_RESERVE_SECONDS` below 0.3 refuses to start: the context needs part of each call's budget kept back to end before the call does. The default, 0.5, needs no change.
- Over stdio with a context provider, SIGTERM closes the providers and exits 0, where 0.7.2 died by the signal (143). Without a provider, nothing changes.
- With `MCP_CONTEXT_REQUIRE_SELECT=true`, a read of the visible columns that Trino refuses is kept as refused for `MCP_VISIBILITY_TTL_SECONDS`: a right granted in Trino serves the context of that table at most that long after. A right withdrawn can leave the context visible as long, never the data.

### From the context tools `visible_tables` and `explain_table`

A server that offered the context of a catalogue through tools of its own now finds it in the core
tools, under the caller's rights in Trino:

- `visible_tables` becomes `describe_schema`: one page of the tables Trino lists, each with its
  columns and, with a provider, its `description`. `list_tables` still gives the names alone.
- `explain_table` becomes `describe_table`, and the shape of what it served changes. A client that
  read `explain_table` must be adapted:

  | `explain_table` | `describe_table` |
  |---|---|
  | the table's fields at the top: `description`, `owner`, `tier`, `grain`, and `tags`, `joins`, `terms`, `properties` in the shapes below | the same, under `table` |
  | `tags`: `["pii"]` | `table.tags`: `[{"name": "pii", "set", "source"}]` |
  | `joins`: `[{"columns", "target", "target_columns"}]` | `table.joins`: `[{"columns", "table", "target_columns", "source"}]` |
  | `columns`, the context of each visible column, by name | `column_context`, by name, in the order of `DESCRIBE` |
  | a column's `classification`: `["PII"]` | `classifications`: `[{"name": "PII", "set", "source"}]` |
  | `properties`: `{"<set>.<key>": "value"}` | `properties`: `{"<set>.<key>": {"value", "source"}}`, with `other_values` when providers differ; a value is cut at `MCP_CONTEXT_TEXT_CHARS` with `"cut": ["value"]` |
  | `terms`: `[{"name", "definition", "status"}]` | `terms`: `[{"name", "ref"}]` (`t1`, `t2`…); the term itself, with its `definition` and `status`, once in the answer's `terms` dictionary under its reference |
  | a column's `values` | removed: the context serves no data value |
  | nothing | `sources` (the provider of each field), `other_descriptions`, `other_grains`, `cut` |
  | nothing | `context_sources`, `context_truncated`, `undescribed_columns` at the top |

  A name (a set, a property's key, a tag, a classification, a term's name, the owner, the tier, a
  column of a grain or a join) longer than 256 characters once cleaned is dropped, never cut.
- `{"known": false}` becomes the error of `DESCRIBE`: a table Trino does not show to the caller
  answers Trino's own error, with its `kind`, and no provider is called. Nothing says whether a
  catalogue knows such a table. A hidden table and a missing one no longer answer the same bytes:
  under an access control that filters the metadata, both answer kind `table` ("does not exist");
  under one that decides by rules, a hidden table answers kind `permission` and a missing one kind
  `table`. What the server adds is the same for both; the error is Trino's. A table Trino shows
  but no catalogue knows answers its columns, an empty `table` and `column_context`, and
  `not_found` in `context_sources`, where `explain_table` answered `{"known": false}`.

## From 0.7.0 to 0.7.1

A security release: reads that Trino answers past a row filter or a column mask are refused by default. It also reads the user from `sub`: **with Keycloak, set `MCP_JWT_USER_CLAIM=preferred_username` before upgrading**, including when you follow the `0.7` image tag.

- `execute_query` refuses every command outside its allow-list (`SELECT`, `DESCRIBE` of a table, `SHOW CATALOGS`, `SHOW SCHEMAS`, `SHOW TABLES`, `SHOW COLUMNS`, `SHOW FUNCTIONS`) with `kind` `read_only`. An agent that ran `SHOW STATS`, `EXPLAIN`, `SHOW CREATE`, `SHOW GRANTS`, `SHOW ROLES`, `SHOW SESSION`, `DESCRIBE INPUT` or `DESCRIBE OUTPUT` now gets that refusal.
- `explain_query` answers `{"valid": true}` instead of `{"plan": "..."}`, and its errors are unchanged. A client that only checked a statement keeps working; one that read the plan must ask the administrator to open `EXPLAIN`.
- To keep the 0.7.0 behaviour for some of these commands, name them in `MCP_EXTRA_READ_COMMANDS`, for example `MCP_EXTRA_READ_COMMANDS="EXPLAIN, SHOW STATS"`. Do it only where no row filter or column mask needs to hold: these reads can show facts beyond a row filter or a column mask. The server says so at every start.
- **The user is read from `sub`**, the OpenID Connect standard, and no longer from `preferred_username` first. With Keycloak, whose `sub` is a UUID, set `MCP_JWT_USER_CLAIM=preferred_username` before upgrading (Entra ID, Okta and Dex also give `sub` an opaque value: name the claim your rules know). Otherwise, in `impersonation` mode Trino runs every query as the caller's UUID, which its rules do not know; in `jwt` mode Trino reads the user from its own `principal-field`, and only the audit line and the extensions see the UUID. A token without the claim is refused. `--check` prints the claim in use (`user from: ...`).
- For plugins: `is_read_only_sql(sql, session_catalog)` now refuses these commands too; pass `extra_reads=parse_extra_reads(...)` to open them.

## From 0.6 to 0.7

Nothing is removed or renamed. One helper for plugins changes its third argument, and the extension guards run differently in `oauth2` mode.

- Every error reply gains a `kind` key, and an `identifier` key when Trino names the object it could not resolve. `error` is exactly the string it was, and the audit codes are the same. A client that reads only `error` keeps working; one that wants to act on the failure can read `kind` (`syntax`, `column`, `table`, `schema`, `catalog`, `function`, `type`, `permission`, `invalid_argument`, `read_only`, `time_budget`, `unavailable`, `other`).
- A column Trino could not find (`COLUMN_NOT_FOUND`, named) in `execute_query` or `explain_query` adds a `hint` key to the error reply: it names the table, or each table, of the agent's own statement the column was written against, and advises `describe_table` on it. Only a table the caller sees is named, and the name comes from the statement, never from Trino. A client that reads only `error` and `kind` keeps working.
- A plugin that builds its error with `akko_mcp_trino.tools.error_reply` gets the `kind` for free. `refusal_reply(code, message)` takes an optional `kind`, `invalid_argument` by default.
- `list_catalogs`, `list_schemas` and `list_tables` answer one page at a time and gain a `next_offset` key: the `offset` to call again with for the next page, or `null` on the last one. `truncated` is kept, and is true exactly when `next_offset` is not `null`. A client that read only the first page keeps working; one that wants the rest calls again with `offset=next_offset`. The advice changes: a `truncated` list is no longer the end, read on with `next_offset`.
- `list_schemas` and `list_tables` read `information_schema.schemata` and `information_schema.tables` instead of `SHOW SCHEMAS` and `SHOW TABLES`. Trino filters `information_schema` by the caller's rights, so a schema the caller may not see shows no row, as a schema that does not exist. `list_tables` checks an empty page against the schemas the caller sees and answers `{"error": "Schema <catalog>.<schema> not found or not visible", "kind": "schema"}` for both, as `SHOW TABLES` failed before; a visible empty schema answers an empty list.
- `list_catalogs` reads at most `MCP_MAX_CATALOGS` catalogs (10000 by default) and answers an error above it: raise the bound if a deployment shows more.
- A new tool, `describe_schema`, reads `system.jdbc.columns` and `system.metadata.table_comments`, by statements the server builds. The advice on the engine's policy changes: refuse the `system` catalog **except** reading `system.jdbc` and `system.metadata`, which Trino filters for each user as it does `information_schema`. A policy that refuses them keeps every other tool working; `describe_schema` then answers an error that names what to allow. The agent's SQL is still refused on all of `system`.
- A new tool, `search_columns`, reads the same two views the same way, and is the first tool an agent is told to call: `search_columns` to find where the data lives, then `describe_schema`, then `execute_query`. It looks for every word of its text in the names and comments of columns and of their tables, and answers both `tables` and `matches` (columns); a table found by its own name or comment comes without its columns. A search of several words that found nothing together goes on with each word alone: its rows carry `words` and the answer `partial: true`. A complete search that found nothing adds `hint`; every page of the per-word pass carries one too (call again with `next_cursor`, pick among partial matches, or nothing was found). `MCP_SEARCH_MAX_WORDS` (8) and `MCP_SEARCH_MAX_WORD_LENGTH` (64) bound its text. A client that reads its answer should expect these keys. Its answer is complete only when `complete` is true; otherwise `not_searched` says what was left and why, and `next_cursor` resumes the search. It needs a call budget (`MCP_CALL_TIMEOUT_SECONDS` above 0, the default) to search every catalog; without one it searches a named catalog only, and the server warns of it at start, in `--check` and in its initialization instructions. `MCP_SEARCH_RESERVE_SECONDS` must stay below `MCP_CALL_TIMEOUT_SECONDS`, or the server refuses to start; set to 0 it is warned of, since a search may then end in the call's own `time_budget` error.
- The `initialize` result now carries the package version as the server's version (it was empty). The server name is unchanged.
- `MCP_DESCRIBE_SCHEMA_TABLES` (tables per `describe_schema` page) is 20 by default and at most 100; a larger value, like a value below 1, refuses to start. `MCP_DESCRIBE_SCHEMA_COLUMNS` (1000), `MCP_SEARCH_RESERVE_SECONDS` (2), `MCP_SEARCH_MIN_LENGTH` (3) and `MCP_MAX_CATALOGS` (10000) are new, with defaults that need no change.
- With `TRINO_IDENTITY_MODE=oauth2` over stdio, the extension guards (`before_auth`, `after_auth`) no longer run once at start: they run on every request that is not discovery (`initialize`, `ping`, the `*/list` methods, notifications), after Trino named the user, so `before_auth` already sees the `principal`, and a binding lasts one request. A guard that refuses no longer keeps the server from starting: it refuses each call, with an `INVALID_REQUEST` error that carries the guard's reason and `message`. A guard that relied on running at start, or without a principal, must be adapted.
- `TRINO_IDENTITY_MODE=oauth2` no longer opens the browser at start: the login waits for the first request that needs Trino. Only the identity check (`SELECT current_user`) runs before a user is admitted.
- New advice to administrators: set `hide-inaccessible-columns=true` in Trino's `config.properties`, so a `SELECT *` drops a column the policy hides instead of being refused with an error that names it.
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
