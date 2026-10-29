# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

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
