# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.3.1] - 2026-09-13

### Added
- `server.json` and the `mcp-name` marker in the README so the package can be listed in the official MCP Registry (`io.github.akko-p/akko-mcp-trino`); the release workflow publishes the server entry after the package.

### Fixed
- The Host header filter is now decided by configuration (`MCP_ALLOWED_HOSTS`, off by default) and never by the SDK. Left to its default, the SDK since 1.10 assumes a localhost server and answers 421 Misdirected Request to any other Host, so a pod reached by its service name or a public name would have refused every call once the image moved past SDK 1.8.

### Changed
- MCP SDK 2.2 (was 1.30): the server is built on `mcp.server.mcpserver.MCPServer`, tool annotations use the SDK's snake_case fields, and the in-process transport suite uses the 2.x client (`streamable_http_client` with an `httpx2` client). The wire protocol, the tools and the guard are unchanged; hosts on either SDK generation connect the same way. The 1.x SDK is no longer supported: `mcp<2` would need the previous release.

## [0.3.0] - 2026-09-13

### Added
- Context providers (`MCP_CONTEXT_PROVIDERS`): `trino-comments` (the `COMMENT ON` Trino already carries, read under the caller's identity), `file` (a versioned JSON document: description, owner, tier, grain, joins, tags, column classification and values), chained field by field with a TTL cache; a product plugs its own catalogue through `build_server(context=...)`. `describe_table` returns `table` and `column_context`; new tool `explain_table`. A provider describes and never decides; one that fails never hides the columns.
- Plugin mechanism: any package can register a context provider under the entry-point group `akko_mcp_trino.context`; `akko-mcp-trino-openmetadata` (in `providers/openmetadata`) is the first, handing the agent OpenMetadata's descriptions, owners, tier, tags, keys and column classifications such as `PII.Sensitive`.
- `MCP_JWT_LEEWAY_SECONDS` (default 30) for clock drift between the issuer and the server; the refusal reason (exception class, never the token) is logged.

### Fixed
- A token whose `iat` was one second in the future (issuer clock ahead of the server's) was refused: one request in three got a 401 on a real deployment. Leeway added.

## [0.2.0] - 2026-09-13

### Changed
- MCP SDK 1.30 (protocol 2025-06-18); `streamable-http` is the default transport (`/mcp`). Set `MCP_TRANSPORT=sse` for older hosts.
- Invalid identifiers come back to the agent as `{"error": …}` instead of raising.
- The `WWW-Authenticate` realm is the server name (`MCP_SERVER_NAME`), no longer a constant.
- Dependencies refreshed: starlette 1.6, sqlglot 30, trino 0.339, PyJWT 2.14.

### Added
- `stdio` transport for local hosts (Claude Desktop, Cursor, Mistral Vibe): `MCP_USER_TOKEN` is verified like a bearer header and bound to the process; strict mode refuses to start without it.
- Trino connection: `TRINO_HTTP_SCHEME`, `TRINO_PASSWORD` (Basic auth for the service account, refused over plain http), `TRINO_VERIFY` (true, false, or a CA bundle), `TRINO_REQUEST_TIMEOUT_SECONDS`.
- `TRINO_IDENTITY_MODE=jwt`: the caller's own verified bearer is sent to Trino as its JWT; no impersonation right and no service password needed; refuses to fall back to the service account and requires https.
- Tools `search_columns`, `profile_table` (`SHOW STATS`) and `explain_query`; `describe_table` takes `sample_rows` (capped at 20, governed).
- MCP tool annotations on every tool (read-only, non-destructive, idempotent where it holds, closed world).
- Tool descriptions written for models; the governed tools explain what a masked value means.
- `akko-mcp-trino --version` and `--check` (effective configuration without secrets, exit 2 when it would not start).
- An in-process integration suite that runs the real FastMCP and the official client on all three transports.
- Every public name carries a docstring; mypy, pip-audit and the docstring rule run in CI; CodeQL and Dependabot enabled; issue and pull request templates; NOTICE file.
- Release: signed build provenance and an SPDX SBOM attached to each release; image attestation on GHCR.

### Fixed
- `list_catalogs`, `list_schemas`, `list_tables` and `describe_table` now run under the caller's identity; they queried Trino as the service account.
- `EXPLAIN ANALYZE <write>` passed the read-only guard and Trino would have executed the write: EXPLAIN is now unwrapped and the explained statement checked on its own; `EXPLAIN ANALYZE` is refused because it runs what it explains.
- Results carrying DATE, TIMESTAMP, DECIMAL, VARBINARY or UUID values are serialised (ISO 8601, exact digits, hex); they made every tool fail before.
- The console script started the server on `--help`.

## [0.1.0] - 2026-09-12

First release on PyPI, extracted from the AKKO platform where the same code
has run in production since July 2026.

### Added
- Five read tools: `list_catalogs`, `list_schemas`, `list_tables`, `describe_table`, `execute_query`.
- Identity forwarding: the verified JWT subject becomes `X-Trino-User`; the engine decides.
- JWT verification against the provider's JWKS (issuer, audience, expiry), fail-closed.
- Read-only guard decided on the SQL AST, refusing writes anywhere in the tree.
- Dual identity: `X-Agent-Key` names the calling product, never a user.
- RFC 9728 protected-resource metadata and `WWW-Authenticate` on every `401`.
- Quotas per user and per agent product, `429` with `Retry-After`.
- Audit join: `X-Request-Id` honoured or generated, one JSON line per tool call, never the token.
- Optional revocation through RFC 7662 introspection, fail-closed.
- Health endpoints independent of Trino, Prometheus metrics.
- Both MCP transports (`sse`, `streamable-http`) behind the same guard.
- `examples/agent.py`: a complete agent on any OpenAI-compatible model, proven with Mistral.
- Packaging: console script `akko-mcp-trino`, Dockerfile, PyPI and GHCR release workflow.

### Fixed (before release, found by the live proofs)
- A write nested in a CTE or subquery is refused; the guard walks the whole AST.
- One trailing semicolon is dropped before the read-only check and before Trino, which refuses it.
