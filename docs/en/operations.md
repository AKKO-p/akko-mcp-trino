# Operations

**English** · [Français](../fr/operations.md)

## Scaling and resource groups

The aggregate query of [`profile_table`](tools.md#profile_table) carries the client tag
`MCP_PROFILE_CLIENT_TAG` (`mcp-profile` by default). Trino's resource groups can therefore limit
how many of these queries run at once and the memory they take. Here is an example for the
file-based resource group manager. The figures are a starting point, not a measurement. Every
selector names a leaf group, so queries without the tag need their own group (`global.other`
here); otherwise Trino rejects them:

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

The resource group limits Trino, and `MCP_PROFILE_MAX_CONCURRENT` (4) limits each replica of this
server. A profile waiting in a Trino queue still holds one of the threads that every tool of the
replica shares. So past that many profiles at once, a call immediately returns `"kind": "busy"`
and sends no query. Keep it at or below the group's `hardConcurrencyLimit` plus `maxQueued`,
divided by the number of replicas.

Each call of `search_columns` runs up to `MCP_SEARCH_CONCURRENCY` metadata statements at once in
Trino. Limit them with a resource group on the server's `TRINO_SOURCE`, as for the client tag of
`profile_table`, or set `MCP_SEARCH_CONCURRENCY=1` to read one catalog at a time. The module
[`trino-tags`](modules.md#trino-tags) tags every query with its tool, so that a resource group can
target the queries the agents write themselves.

## Refusals

Every refusal carries `X-Reason` and the request id, never the token. The core refuses with
`401 unauthenticated` a request without a valid token (none, forged, expired, or issued for
another audience, Trino's included), and says in the body what to send:

```json
{"error": "unauthenticated", "message": "this server requires a bearer token issued for it by the identity provider: send Authorization: Bearer <access token>"}
```

When `MCP_RESOURCE_URL` is set, the server publishes its protected resource metadata at
`/.well-known/oauth-protected-resource`, and under the path of the URL, such as
`/.well-known/oauth-protected-resource/mcp`. The metadata names the resource and
`MCP_OIDC_ISSUER` as its authorization server, and the `401` carries
`WWW-Authenticate: Bearer resource_metadata="…"`, as the MCP authorization specification asks. An
extension's guards add their own reasons, and its documents and challenges take precedence over
the core's.

## Warnings

A configuration that leaves something open is reported in three places: a `WARNING` line at
start, `--check`, and the `instructions` of the MCP initialization, sent to every host. The core
warns in these cases, with this text:

| Condition | Warning |
|---|---|
| `TRINO_IDENTITY_MODE=service` | `TRINO_IDENTITY_MODE=service: every query runs as the Trino user '<TRINO_USER>', whoever calls; Trino cannot tell callers apart` |
| `MCP_AUTH_ENABLED=false` | `MCP_AUTH_ENABLED=false: no caller is verified; anyone who reaches this server queries Trino` |
| `MCP_AUTH_REQUIRED=false`, with authentication enabled | `MCP_AUTH_REQUIRED=false: a request without a valid token is served too` |
| `MCP_SEARCH_RESERVE_SECONDS=0` with a call budget | `MCP_SEARCH_RESERVE_SECONDS=0: a search of search_columns may run to the end of the call's budget, and the call then answers its time_budget error with nothing it found` |
| `MCP_CALL_TIMEOUT_SECONDS=0` | `MCP_CALL_TIMEOUT_SECONDS=0: a tool call has no time budget, so search_columns refuses to search every catalog and searches a named catalog only` |
| `MCP_EXTRA_READ_COMMANDS` names commands | the warning names the commands it enables; [Security](security.md#the-statements-an-agent-may-send) says what they are |
| `MCP_DESCRIBE_MAX_COLUMNS` above `MCP_PROFILE_DESCRIBE_ROWS` | `MCP_DESCRIBE_MAX_COLUMNS=<n> is above MCP_PROFILE_DESCRIBE_ROWS=<n>: describe_table shows columns profile_table cannot name` |
| `full` in `MCP_PROFILE_MODES` with no time budget | `MCP_PROFILE_MODES allows full while MCP_PROFILE_TIMEOUT_SECONDS, TRINO_QUERY_TIMEOUT_SECONDS and MCP_CALL_TIMEOUT_SECONDS are 0: a full profile reads every row it may read with no time limit` |
| a context provider without `MCP_CONTEXT_CANARY` | `MCP_CONTEXT_CANARY is not set: nothing checks that Trino filters its listings, and context follows what Trino lists` |
| a context provider in `service` mode | `context visibility is the service account's` |

## Audit

Every response of the MCP endpoint carries `X-Request-Id`. It is the one sent by the proxy or
gateway in front of the server, when it is a plain identifier (letters, digits, `.`, `_`, `:`,
`-`, at most 128 characters), or one the server generated. A tool call outside an HTTP request
(stdio) gets its own id. Every tool call, a plugin's included, writes one JSON line to the
`mcp.audit` logger, keyed by that id:

```json
{"request_id":"edge-42","tool":"execute_query","subject":"alice","ok":false,"error":"USER_ERROR:PERMISSION_DENIED","duration_ms":84}
```

With the default logging configuration, the line comes out prefixed with `INFO:mcp.audit:`. A
collector that expects plain JSON must strip that prefix.

| Field | Meaning |
|---|---|
| `request_id`, `tool`, `subject` | which call, which tool, for whom; the line has no field that could hold the bearer or the SQL text |
| `ok`, `error` | the outcome; `error` is a code, never a message, because a message from Trino may quote a value read from a row (`Cannot cast '<value>' to INT`): Trino's error type and name for a Trino error (`USER_ERROR:INVALID_CAST_ARGUMENT`), the class of any other exception (`TimeoutError`, `CallTimeout` past the call's budget), the code a tool names for its own refusal (`read_only`, `invalid_argument`, `sql_too_long`, `sql_too_nested`, `parse_deadline`, `parse_no_worker`, `parse_busy`, `parse_failed`, `result_columns_too_long`) or a plugin with `note(error=...)`, else `tool_error`. The agent reads the whole message |
| `duration_ms` | how long the call took |

What a call did (the SHA-256 of its SQL, the tables, the rows, whether they were cut) is kept in
its record while it runs (`akko_mcp_trino.audit.note`). The core's line does not write it; an
extension may (`wrap_audit`), as [`fingerprint-audit`](modules.md#fingerprint-audit) does. Each
list of the record keeps `MCP_AUDIT_MAX_NAMES` names (200 by default) and counts the others, once
each, at a constant cost: a search over a thousand catalogs never writes a line without a limit.

## Health and metrics

These endpoints exist over the HTTP transports only, on the health port.

- `/health` never touches Trino, so a liveness probe cannot kill the server because Trino is slow.
- What `/ready` checks depends on the identity mode: in `jwt` mode, Trino's public `/v1/info`; in
  `impersonation` and `service` modes, a bounded `SELECT 1` as `TRINO_USER`, impersonating no
  one. When the check fails, `/ready` returns `503` with `{"status": "error", "detail": ...}`.
- `/metrics` exposes `mcp_trino_queries_total`, `mcp_trino_query_errors_total`,
  `mcp_trino_query_duration_seconds` and `mcp_trino_sql_refusals_total`, the statements refused
  before Trino by `reason`: `too_long`, `too_nested`, `caller_busy`, `no_worker`, `deadline` (a
  parse cut by the deadline) or `worker_failed`; never a statement or a name. With context providers loaded, it also exposes, by provider,
  `mcp_trino_context_calls_total`, `mcp_trino_context_errors_total` (by status),
  `mcp_trino_context_call_duration_seconds`, `mcp_trino_context_cache_lookups_total` (hit or
  miss), `mcp_trino_context_cache_bytes` and `mcp_trino_context_breaker_open`. A call the tool
  stops waiting for is counted as one that ended, each source read as `partial` or
  `unavailable`; a server stopping during a read counts a few such calls.

## Upgrading

[MIGRATING.md](../../MIGRATING.md) gives every step. In short:

**From 0.7 to 0.8.** [MIGRATING.md](../../MIGRATING.md#from-07-to-08) lists the changes, among them
the modules that `MCP_EXTENSIONS` now names one by one.

**From 0.7.0.** 0.7.1 and 0.7.2 are security releases. `execute_query` refuses `SHOW STATS`,
`EXPLAIN`, `SHOW CREATE` and the other reads listed in
[Security](security.md#the-statements-an-agent-may-send), and the user is read from `sub`. With
Keycloak, set `MCP_JWT_USER_CLAIM=preferred_username` before upgrading.

**From 0.6.** 0.7.0 removes and renames nothing: it adds `search_columns` and `describe_schema`,
pages the listings, and gives every error a `kind`. Let the engine's policy read `system.jdbc` and
`system.metadata`, which the two new tools read (0.6 advised refusing all of `system`). Keep
`MCP_CALL_TIMEOUT_SECONDS` at 0 or above `MCP_SEARCH_RESERVE_SECONDS` (2 by default), or the
server refuses to start. A `truncated` listing is no longer the end: read on with `next_offset`.
For extension authors, one helper changes its signature, and in `oauth2` mode the guards run on
every request that is not discovery, no longer once at start.

**From 0.5.** 0.6.0 changed the defaults: a verified caller is required, and the `jwt` mode
exchanges the caller's token instead of passing it on.
