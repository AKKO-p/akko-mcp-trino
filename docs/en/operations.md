# Operations

**English** · [Français](../fr/operations.md)

## Scaling and resource groups

The aggregate query of [`profile_table`](tools.md#profile_table) carries the client tag `MCP_PROFILE_CLIENT_TAG`
(`mcp-profile` by default), so Trino's resource groups can bound how many run at once and
the memory they take; for example, with the file-based resource group manager (the figures
are an example to adapt, not a measure). Every selector names a leaf group, so the queries
without the tag need their own group (`global.other` here), or Trino refuses them:

```json
{
  "rootGroups": [
    {
      "name": "global",
      "softMemoryLimit": "80%",
      "hardConcurrencyLimit": 100,
      "maxQueued": 1000,
      "subGroups": [
        {"name": "profile", "softMemoryLimit": "10%", "hardConcurrencyLimit": 2, "maxQueued": 20},
        {"name": "other", "softMemoryLimit": "70%", "hardConcurrencyLimit": 98, "maxQueued": 980}
      ]
    }
  ],
  "selectors": [
    {"clientTags": ["mcp-profile"], "group": "global.profile"},
    {"group": "global.other"}
  ]
}
```

The resource group bounds Trino; `MCP_PROFILE_MAX_CONCURRENT` (4) bounds each replica of
this server: a profile waiting in a Trino queue still holds one of the threads every tool of
the replica shares, so past that many profiles at once a call answers `"kind": "busy"` at once
and sends no query. Keep it at or below the group's `hardConcurrencyLimit` plus `maxQueued`,
divided by the replicas.

For Trino, each call of `search_columns` runs up to
`MCP_SEARCH_CONCURRENCY` metadata statements at once: bound them with a resource group on
the server's `TRINO_SOURCE`, as for `profile_table`'s client tag, or set
`MCP_SEARCH_CONCURRENCY=1` to read one catalog at a time, as 0.7 did. The module
[`trino-tags`](modules.md#trino-tags) tags every query with its tool, for a resource group of the
queries the agents write themselves.

## Refusals

Every refusal carries `X-Reason` and the request id, never the token. The core
refuses with `401 unauthenticated` a request without a valid token (none,
forged, expired, or issued for another audience, Trino's included), and says in
the body what to send:

```json
{"error": "unauthenticated", "message": "this server requires a bearer token issued for it by the identity provider: send Authorization: Bearer <access token>"}
```

When `MCP_RESOURCE_URL` is set, the server publishes its protected resource
metadata at `/.well-known/oauth-protected-resource` (and under the path of the
URL, such as `/.well-known/oauth-protected-resource/mcp`), naming the resource
and `MCP_OIDC_ISSUER` as its authorization server, and the `401` carries
`WWW-Authenticate: Bearer resource_metadata="…"`, as the MCP authorization
specification asks. An extension's guards add their own reasons; its
documents and challenges take precedence over the core's.

## Warnings

A configuration that leaves something open is said three times: as a
`WARNING` line at start, in `--check`, and to every MCP host in the
`instructions` of the initialization. The core warns of `service` mode and of a
door that does not require a valid token.

## Audit

Every response of the MCP endpoint carries `X-Request-Id`. It is the one the
gateway in front of the server sent, when it is a plain token (letters,
digits, `.`, `_`, `:`, `-`, at most 128), or one the server generated. A tool
call outside an HTTP request (stdio) gets its own id. Every tool call, a
plugin's included, writes one JSON line to the `mcp.audit` logger keyed by
that id:

```json
{"request_id":"edge-42","tool":"execute_query","subject":"alice","ok":false,"error":"USER_ERROR:PERMISSION_DENIED","duration_ms":84}
```

With the default logging configuration, the line comes out prefixed with
`INFO:mcp.audit:`; a collector that expects pure JSON strips that prefix.

| Field | Meaning |
|---|---|
| `request_id`, `tool`, `subject` | which call, which tool, for whom; the line has no field that could hold the bearer or the SQL text |
| `ok`, `error` | the outcome; `error` is a code, never a message, because a message from Trino may quote a value read from a row (`Cannot cast '<value>' to INT`): Trino's error type and name for a Trino error (`USER_ERROR:INVALID_CAST_ARGUMENT`), the class of any other exception (`TimeoutError`, `CallTimeout` past the call's budget), the code a tool names for its own refusal (`read_only`, `invalid_argument`) or a plugin with `note(error=...)`, else `tool_error`. The agent reads the whole message |
| `duration_ms` | how long the call took |

What a call did (the SHA-256 of its SQL, the tables, the rows, whether they
were cut) is kept in its record while it runs (`akko_mcp_trino.audit.note`);
the core's line does not write it, an extension may (`wrap_audit`), as `fingerprint-audit`
does. Each list of the record keeps `MCP_AUDIT_MAX_NAMES` names (200 by default) and counts
the others, once each, at a constant cost: a search over a thousand catalogs never writes a
line without a bound.

## Health and metrics

`/health` on the health port never touches Trino, so a liveness probe cannot
kill the server because Trino is slow. `/ready` asks the identity mode: in
`jwt` mode Trino's public `/v1/info`; in `impersonation` and `service` modes, a
bounded `SELECT 1` as `TRINO_USER`, impersonating no one. `/metrics` exposes `mcp_trino_queries_total`,
`mcp_trino_query_errors_total` and `mcp_trino_query_duration_seconds`. With context providers loaded it also
exposes `mcp_trino_context_calls_total`, `mcp_trino_context_errors_total` (by status),
`mcp_trino_context_call_duration_seconds`, `mcp_trino_context_cache_lookups_total` (hit or miss),
`mcp_trino_context_cache_bytes` and `mcp_trino_context_breaker_open`, each by provider.

## Upgrading

**From 0.7.0.** 0.7.1 and 0.7.2 are a security release. `execute_query`
refuses `SHOW STATS`, `EXPLAIN`, `SHOW CREATE` and the other reads that can
show facts beyond a row filter or a column mask, and the user is read from `sub`.
With Keycloak, set `MCP_JWT_USER_CLAIM=preferred_username` before upgrading.
See [MIGRATING.md](../../MIGRATING.md).

**From 0.6.** 0.7.0 removes and renames nothing: it adds
`search_columns` and `describe_schema`, pages the listings, and gives every
error a `kind`. Let the engine's policy read `system.jdbc` and
`system.metadata`, which the two new tools read (0.6 advised refusing all of
`system`). Keep `MCP_CALL_TIMEOUT_SECONDS` at 0 or above
`MCP_SEARCH_RESERVE_SECONDS` (2 by default), or the server refuses to start.
A `truncated` listing is no longer the end: read on with `next_offset`.
Extension authors: one helper changes its signature, and in `oauth2` mode the
guards run on every request that is not discovery, no longer once at start.

**From 0.5.** 0.6.0 changed the defaults: a verified caller is required, and
the `jwt` mode exchanges the caller's token instead of passing it on. See
[MIGRATING.md](../../MIGRATING.md) for both.
