# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- Every error a tool answers carries a `kind` that says what to do about it: `syntax`, `column`, `table`, `schema`, `catalog`, `function`, `type`, `permission`, `invalid_argument`, `read_only`, `time_budget`, `unavailable` or `other`. When Trino names the column, table, schema or catalog it could not resolve, the answer also carries `identifier`. `unavailable` (an internal error or a lost connection) says the statement is neither valid nor invalid. The `error` message and the audit codes are unchanged.
- `list_catalogs`, `list_schemas` and `list_tables` page their answer: an optional `offset` argument (0 by default) and a `next_offset` key in the answer, the offset of the next page or `null` on the last one. A page holds `TRINO_MAX_ROWS` names, sorted by name. Before, a list longer than the row cap was cut with `truncated: true` and no way to read the rest. `truncated` stays, true exactly when `next_offset` is not `null`. Schemas and tables are paged in Trino, on `information_schema.schemata` and `information_schema.tables` (was `SHOW SCHEMAS` and `SHOW TABLES`). An `offset` that is not a whole number from 0 is refused (`invalid_argument`) before Trino. `list_tables` on a schema that does not exist or that the caller may not see answers `kind` `schema`, "not found or not visible", the same for both, never an empty list for a missing or hidden schema.
- `MCP_MAX_CATALOGS` (10000 by default, 1 at least): `list_catalogs` reads `SHOW CATALOGS` whole, past the row cap, up to this bound (`system` aside), and pages it in the server; above the bound it answers an error, never a cut list. `--check` prints the page size and the bound.
- For plugins: `ToolContext.run` takes `row_limit` (replaces the row cap for a statement the tool builds itself) and `timeout` (lowers one query's time budget; a query stopped by it raises a plain `TimeoutError` and the call goes on). `ToolContext.run_whole(sql, bound=…, what=…)` reads every row up to a bound, or raises `ValueError`. `ToolContext.page_size` is the row cap. `check_offset(offset)` validates an offset. `TrinoClient.query` takes the same `row_limit` and `timeout`.

- `describe_schema(catalog, schema, offset)`: a schema's tables and views in one call, each with its type, its comment and all its columns (name, type, comment), paged by whole tables in Trino. `MCP_DESCRIBE_SCHEMA_TABLES` (20 by default, from 1 to 100) bounds the tables of a page and `MCP_DESCRIBE_SCHEMA_COLUMNS` (1000 by default) their columns in all; the first table of a page comes whole however wide, and `shortened` says when a page holds fewer tables for the column budget. Comments and columns are read from `system.metadata.table_comments` and `system.jdbc.columns`, which Trino filters for each user; a policy that refuses them gets an error that names what to allow. Each page reads only its own tables' comments and columns, never the whole schema's. A page costs four statements: the schema check, the page of names, one read of the columns bounded by `MCP_DESCRIBE_SCHEMA_COLUMNS` plus one row, which keeps the tables that arrive whole within the budget, and the comments of the tables kept; a first table wider than the budget is read again alone, by its own query, one statement more per `MCP_DESCRIBE_SCHEMA_COLUMNS` columns. A page size above 100 is refused at start. A missing schema and a hidden one answer the same `kind` `schema` error. `--check` prints both bounds.
- `search_columns(text, catalog, schema, cursor)`: the columns whose name or comment contains a text, ignoring case, in every catalog the caller sees (`system` never), in one catalog or in one schema. A named schema is checked first: one that does not exist and one the caller may not see answer the same `kind` `schema` error, never an empty search said to be complete; a catalog Trino does not know or refuses answers Trino's error. Its description makes it the first tool when the agent does not know where the data lives; the listing tools describe browsing. It reads `system.jdbc.columns` one schema at a time, in a stable order, and stays within the call's budget: before each schema it keeps `MCP_SEARCH_RESERVE_SECONDS` (2 by default) in reserve and stops each query in Trino at what is left, so it answers what it searched rather than the call's own error. The matches stop at the row cap. The answer says what was searched (`schemas_searched`), what was not and why (`not_searched`: `time_budget`, `row_cap` or the `kind` of Trino's error), whether everything in scope was searched over every call of the search (`complete`), and where to resume (`next_cursor`, inside a schema when the row cap stopped it there). The text is one escaped `LIKE` literal, its `%`, `_` and `!` taken literally; a text shorter than `MCP_SEARCH_MIN_LENGTH` (3 by default) is refused, and so is a search of every catalog without a call budget. A schema (or a catalog's schema list) slower than `TRINO_QUERY_TIMEOUT_SECONDS` while the call has time left is named `time_budget` and skipped, and so is one slower than a whole call: a call that runs out of time exactly where it resumed, having moved nothing, skips it. Every call advances, so a resumed search never stops at the same place forever. A cursor altered, kept for another search, or naming a catalog the caller does not see is refused (`invalid_argument`); every cursor the server gives resumes, whatever its schema's name. The audit line notes the matches and whether the search was cut, never the text. `--check` prints both settings, refuses a reserve not below `MCP_CALL_TIMEOUT_SECONDS`, warns of a reserve of 0 with a call budget, and warns that `MCP_CALL_TIMEOUT_SECONDS=0` limits the search to a named catalog.
- For plugins: `ToolContext.visible_catalogs(bound)` lists the catalogs the caller sees, sorted, `system` aside, read whole up to the bound. `ToolContext.schema_visible(catalog, schema)` says whether the caller sees a schema, the same false for a missing and a hidden one. `akko_mcp_trino.errors.VIEW_REFUSED` names the kinds of an error on a metadata view that its access control can be the cause of.
- For plugins: `akko_mcp_trino.sql_guard.sql_literal(text)` makes a whole Trino string literal; `akko_mcp_trino.errors.metadata_view_error(exc)` explains an error on Trino's metadata views.

### Changed
- The advice on the engine's policy: refuse the `system` catalog **except** reading `system.jdbc` and `system.metadata`, which Trino filters for each user; without them `describe_schema` and `search_columns` answer an error that says so. The agent's SQL stays refused on all of `system`.
- `listing_reply(key, items, next_offset)` takes the next page's offset, or `None`, instead of `truncated`, and answers both keys; a bool is refused with `TypeError`.
- For extension authors: with `TRINO_IDENTITY_MODE=oauth2` over stdio, the guards (`before_auth`, `after_auth`) no longer run once at start. They run on every request that is not discovery, after Trino named the user, so `before_auth` already sees the `principal`, and a binding lasts one request. Discovery (`initialize`, `ping`, the `*/list` methods, notifications) runs no guard. A guard that refuses at start no longer keeps the server from starting and listing its tools: it refuses each call. A refusal, or Trino naming no user, answers the request an `INVALID_REQUEST` error with the guard's reason and its `message`, instead of a bare error.
- `TRINO_IDENTITY_MODE=oauth2` refuses a query without an admitted user, like `jwt` and `impersonation`. Only the identity check, `TrinoClient.current_user()` (`SELECT current_user`), runs before one; it also answers the mode's `ping`.

### Fixed
- The `initialize` result announces the package version (`akko_mcp_trino.__version__`) as the server's version; it was empty, so a client or an audit could not tell which release answered. The server name is unchanged.
- `akko_mcp_trino.sql_guard.safe_sql_string` no longer doubles a backslash: Trino reads no backslash escape in a string literal, so `a\b` became `a\\b`, another value. It still doubles quotes and drops NUL bytes.
- Trino OAuth2 no longer opens the browser when the MCP host starts: startup and discovery never log in, and the login waits for the first request that needs Trino. Every request that is not discovery (a tool call, a resource read, any method a later SDK adds) logs in and is admitted before it runs.

## [0.6.1] - 2026-10-29

### Fixed
- `list_schemas`, `list_tables` and `describe_table` refuse the `system` catalog before reaching Trino, as `execute_query` and `explain_query` already did. `describe_table` with `sample_rows` could read `system.runtime.queries`. `list_catalogs` no longer lists `system`.

### Added
- `TRINO_IDENTITY_MODE=oauth2`: native Trino browser/SSO login over HTTPS for stdio hosts, with driver-managed token caching and reauthentication. The server sends Trino no user name, so Trino runs every query as the user who signed in, never as `TRINO_USER`. Startup verifies the user with `SELECT current_user` and uses that identity for admission and audit ([#15](https://github.com/AKKO-p/akko-mcp-trino/issues/15)).

## [0.6.0] - 2026-10-17

Safe defaults, and no token passthrough. **Breaking changes**: read
[MIGRATING.md](MIGRATING.md) before upgrading.

### Security
- **No token passthrough.** In `jwt` mode the server no longer sends Trino the caller's own token, which the MCP authorization specification forbids. It obtains a token for Trino by OAuth 2.0 Token Exchange (RFC 8693) at the identity provider, with its own confidential client (`TRINO_TOKEN_EXCHANGE_URL`, `_CLIENT_ID`, `_CLIENT_SECRET`, `_AUDIENCE`, `_SCOPE`, `_VERIFY`), keeps it in memory until shortly before it expires, and never returns it to the agent or the host. The token endpoint is https only.
- **Audience binding (RFC 8707).** A token must be issued for this server: its audience is `MCP_OIDC_AUDIENCE` or the new `MCP_RESOURCE_URL`. A token whose audience includes Trino's (`TRINO_TOKEN_EXCHANGE_AUDIENCE`) is refused, even if it also names this server. A configuration that would accept Trino's audience, or that sets no audience while auth is on, refuses to start.
- **Safe by default.** `TRINO_IDENTITY_MODE` defaults to `jwt` (was `service`); `MCP_AUTH_ENABLED` and `MCP_AUTH_REQUIRED` default to `true` (were `false`). A request without a valid token is refused with `401` and a body that says what to send. `jwt` and `impersonation` refuse to start unless the door requires a valid token. `MCP_AUTH_REQUIRED=true` with `MCP_AUTH_ENABLED=false` refuses to start (it would refuse every request).
- **Service mode only when named, and announced.** `service` mode and an open door are still possible, chosen explicitly. The server then logs a warning at start, `--check` prints it, and every MCP host receives it in the `instructions` of the initialization.
- **Tool annotations follow the settings.** With `TRINO_READ_ONLY=false`, `execute_query` is annotated `readOnlyHint: false`, `destructiveHint: true`, `idempotentHint: false`, its title is "Run a query (writes allowed)" and its description says it may write. Before, it was annotated read-only whatever the setting, so a host could skip a confirmation it should have asked for.
- The README and SECURITY.md say what read-only covers and what it does not: the guard covers what passes through this server, not what a user's Trino rights allow by other means, with the engine rules to make it end to end.

### Added
- Identity mode `impersonation`, for an identity provider without token exchange: the server authenticates to Trino as `TRINO_USER` (password, or a client certificate) and Trino runs each query as the verified caller, under an impersonation rule only that account has. Readiness runs `SELECT 1` as the account, impersonating no one.
- `TRINO_CLIENT_CERT` and `TRINO_CLIENT_KEY`: the server's account may authenticate to Trino by client certificate (mTLS), in `service` and `impersonation` modes.
- `MCP_RESOURCE_URL`: this server's canonical URL. Accepted as an audience; the server publishes its protected resource metadata (RFC 9728) at `/.well-known/oauth-protected-resource` (and under the URL's path), and a `401` carries `WWW-Authenticate: Bearer resource_metadata="…"`. An extension's documents and challenges take precedence.
- `Refusal.message`: a guard may tell the client in the body what to do. `ToolContext.tool(destructive=...)`.
- `Config.problems()` and `Config.warnings()`; `--check` prints `WARNING:` lines.
- `integration/`: a live run against a disposable Keycloak and Trino in Docker (token exchange, impersonation by client certificate, service mode, every refusal at the door), run in CI on every pull request.

### Changed
- Default budgets: `TRINO_QUERY_TIMEOUT_SECONDS` is `60` (was `0`, no limit) and `MCP_CALL_TIMEOUT_SECONDS` is `120` (was `0`). `0` still removes the limit. `TRINO_MAX_ROWS` must be 1 or more, and a negative budget refuses to start.
- `build_server` passes `instructions=` to `mcp_factory` when the configuration has warnings; a custom factory must accept that keyword in `service` mode or with an open door.
- The `serving` log line names the identity mode.

## [0.5.3] - 2026-10-11

### Changed
- Listed in the official MCP registry as `io.github.ab2dridi/akko-mcp-trino` (README marker and `server.json`). No code change.

## [0.5.2] - 2026-10-11

### Changed
- Dependencies are declared as ranges (lower bound: the version tested so far, upper bound: the next major), so that the server installs next to other tools and can be packaged for conda-forge. CI runs the test suite at the lowest and at the highest versions the ranges allow.
- The container image still installs exact versions, from `image-requirements.txt`.

## [0.5.1] - 2026-10-11

### Changed
- README: AKKO described as a French project, test count 322, no pinned Trino test version. No code change.

## [0.5.0] - 2026-10-11

First release.

### Added
- Six read-only tools: `list_catalogs`, `list_schemas`, `list_tables`, `describe_table`, `explain_query`, `execute_query`.
- Identity mode `service` (`TRINO_IDENTITY_MODE`, the default): every query runs as `TRINO_USER`, authenticated by `TRINO_PASSWORD` over https when it is set. A password is never sent over http; an unknown mode refuses to start.
- Identity mode `jwt`: the caller's verified bearer is sent to Trino, which verifies it again with its JWT authenticator and runs the query as that user, so Trino's access control decides for each user. https only (refused at start otherwise); a request without a verified bearer is refused, never run as `TRINO_USER`; readiness asks Trino's public `/v1/info`. The bearer is bound at the door only once verified, in a context variable of its own (`akko_mcp_trino.identity.current_bearer`), never in the principal nor the audit line. An extension may subclass `JwtIdentity` and override `trino_token(bearer)` to send Trino another token for the caller.
- Read-only guard on the SQL tree (CTEs and subqueries included), table functions and the `system` catalog refused; row cap with `truncated`, time budget per query and per tool call.
- The door: bearer tokens verified by JWKS (issuer, audience, expiry, clock tolerance), strict mode refusing any request without a verified caller, transports `streamable-http`, `sse` and `stdio`, Host header filter.
- One audit line per call: `request_id`, `tool`, `subject`, `ok`, `error`, `duration_ms`. Queries carry the source (`TRINO_SOURCE`).
- Tool plugins (`akko_mcp_trino.tools`, `MCP_TOOL_PLUGINS`) and extension points (`akko_mcp_trino.extensions`, `MCP_EXTENSIONS`): a package registers a factory `(config, env) -> Extension`. Hooks: `identity_modes`, `audiences`, `before_auth` and `after_auth` guards (`Refusal`, `Admission`), `public_documents`, `challenge`, `routes` (behind the guard), `wrap_audit`, `client_tags`, `describe_table_doc`, `tool_registrars`, `check_lines`. An unknown name, or a factory that cannot serve its settings, refuses to start and is reported by `--check`. An extension can only refuse: its guards run around the core's token verification, never instead of it.
- `ToolContext.add_describer(fn)` and public reply helpers for plugins (`error_reply`, `refusal_reply`, `listing_reply`, `was_cut`).
- `/health`, `/ready`, Prometheus metrics, `--check` and `--version`.
