# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed
- A write nested in a CTE or subquery is refused; the guard walks the whole AST.
- One trailing semicolon is dropped before the read-only check and before Trino, which refuses it.

### Added
- `examples/agent.py`: a complete agent on any OpenAI-compatible model, proven with Mistral.
- Packaging: console script `akko-mcp-trino`, Dockerfile, PyPI and GHCR release workflow.

## [0.1.0] - 2026-09-12

First release, extracted from the AKKO platform where the same code has run
in production since July 2026.

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
