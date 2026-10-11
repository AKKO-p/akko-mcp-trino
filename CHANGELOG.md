# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

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
