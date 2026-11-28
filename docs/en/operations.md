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
| `TRINO_VERIFY=false` (or `0`, `no`) over https | `TRINO_VERIFY=false: Trino's certificate is not verified, so the server cannot know whom it sends its credentials to; set TRINO_VERIFY=true, or the path of a CA bundle` |
| `MCP_JWKS_URL` not https, with token verification on | `MCP_JWKS_URL=<url>: the keys that verify tokens are read over plain http, so anyone on the path could substitute them and mint accepted tokens; set an https MCP_JWKS_URL` |
| `TRINO_TOKEN_EXCHANGE_VERIFY=false` (or `0`, `no`) in `jwt` mode | `TRINO_TOKEN_EXCHANGE_VERIFY=false: the token endpoint's certificate is not verified, so the server cannot know whom it sends its client secret and the callers' tokens to; set TRINO_TOKEN_EXCHANGE_VERIFY=true, or the path of a CA bundle` |
| no `discovery` tool exposed | `MCP_TOOLSETS leaves out every discovery tool: the agent writes its queries without listing or describing the tables it may read; add discovery` |
| `MCP_SEARCH_RESERVE_SECONDS=0` with a call budget | `MCP_SEARCH_RESERVE_SECONDS=0: a search of search_columns may run to the end of the call's budget, and the call then answers its time_budget error with nothing it found` |
| `MCP_CALL_TIMEOUT_SECONDS=0` | `MCP_CALL_TIMEOUT_SECONDS=0: a tool call has no time budget, so search_columns refuses to search every catalog and searches a named catalog only` |
| `MCP_EXTRA_READ_COMMANDS` names commands | the warning names the commands it enables; [Security](security.md#the-statements-an-agent-may-send) says what they are |
| `MCP_DESCRIBE_MAX_COLUMNS` above `MCP_PROFILE_DESCRIBE_ROWS` | `MCP_DESCRIBE_MAX_COLUMNS=<n> is above MCP_PROFILE_DESCRIBE_ROWS=<n>: describe_table shows columns profile_table cannot name` |
| `full` in `MCP_PROFILE_MODES` with no time budget | `MCP_PROFILE_MODES allows full while MCP_PROFILE_TIMEOUT_SECONDS, TRINO_QUERY_TIMEOUT_SECONDS and MCP_CALL_TIMEOUT_SECONDS are 0: a full profile reads every row it may read with no time limit` |
| a context provider without `MCP_CONTEXT_CANARY` | `MCP_CONTEXT_CANARY is not set: nothing checks that Trino filters its listings, and context follows what Trino lists` |
| a context provider in `service` mode | `context visibility is the service account's` |

## Audit and logs

The server writes two kinds of line to stderr: the audit, on the `mcp.audit` logger, and the
technical log of its other loggers. Over stdio, stderr is what the host keeps; in a pod, it is
what the cluster's collector reads. The server sends no line anywhere itself: shipping, keeping
and protecting them is the job of the platform (see [Sending the lines to a SIEM](#sending-the-lines-to-a-siem)).

### Log format

`MCP_LOG_FORMAT` chooses the format of every line.

- `text`, the default in 0.8, keeps the lines of earlier versions: `LEVEL:logger:message`. The
  audit line is the message of `mcp.audit`, so it comes out as `INFO:mcp.audit:{...}`.
- `json` writes every line as one JSON object, with nothing before it: `ts` (UTC, ISO 8601 with
  milliseconds), `level`, `logger`, then `msg` for a technical line, or the audit's fields at
  the top level. uvicorn's own lines take the same format. An exception that goes with a
  technical line is in `exc`, on the same line.

Every audit line carries its own `ts` in both formats, so that a line kept without its collector
still says when it was written. A later minor release may make `json` the default over HTTP;
[MIGRATING.md](../../MIGRATING.md) will say so first.

### A tool call

Every tool call, a plugin's included, writes one line. Here is a real one, in `json`, in
`service` mode behind a token check, with `fingerprint-audit` and `trino-tags`, put on several
lines for reading (the server writes it on one); the fields after `query_ids` are the module's:

```json
{
  "ts": "2026-11-28T16:15:18.261Z",
  "level": "INFO",
  "logger": "mcp.audit",
  "event": "tool_call",
  "request_id": "preuve-087-appel",
  "tool": "execute_query",
  "subject": "alice",
  "client_id": "agent-essai",
  "mcp_client": "mcp/0.1.0",
  "client_ip": "198.51.100.7",
  "identity_mode": "service",
  "trino_user": "A12345",
  "ok": true,
  "error": "",
  "duration_ms": 1278,
  "query_ids": ["20261128_161517_00302_qyb4k"],
  "token_id": "jti-essai-1",
  "sql_sha256": "047c266c14197de8cb2a77b806c37d62a5f3dd8459328cd67a2c8691b79433f2",
  "tables": ["tpch.tiny.nation"],
  "rows": 3,
  "truncated": false,
  "metadata_served": []
}
```

| Field | Meaning |
|---|---|
| `ts`, `event` | when the line was written (the end of the call), and `tool_call` |
| `request_id` | the `X-Request-Id` sent by the proxy or gateway in front of the server, when it is a plain identifier (letters, digits, `.`, `_`, `:`, `-`, at most 128 characters), or one the server generated; every response of the MCP endpoint echoes it. A call over stdio gets its own |
| `tool` | the tool called |
| `subject` | the verified caller, from `MCP_JWT_USER_CLAIM`; empty when no token was verified (`MCP_AUTH_ENABLED=false`, stdio without `MCP_USER_TOKEN`) |
| `client_id` | the application the token was issued to, the first of `MCP_JWT_CLIENT_CLAIMS` the token carries (`azp`, then `client_id`); verified with the token, empty without one |
| `mcp_client` | `name/version` of the MCP client as it declared itself at initialization (`clientInfo`), cut to 64 and 32 characters, printable ASCII only. Declared, never verified: any client may write any name. Use `client_id` to know which application holds the token |
| `client_ip` | the address of the client: the peer's, or behind `MCP_TRUSTED_PROXIES`, the one their `X-Forwarded-For` gives (see [The client's address](#the-clients-address)); empty over stdio |
| `identity_mode` | `TRINO_IDENTITY_MODE`: how the server reaches Trino |
| `trino_user` | the user Trino saw for the call's queries: `TRINO_USER` in `service`, the caller in `jwt` and `impersonation`, the user Trino named in `oauth2`; empty when the call ran no query |
| `ok`, `error` | the outcome; `error` is a code, never a message, because a message from Trino may quote a value read from a row (`Cannot cast '<value>' to INT`): Trino's error type and name for a Trino error (`USER_ERROR:INVALID_CAST_ARGUMENT`), the class of any other exception (`TimeoutError`, `CallTimeout` past the call's budget), the code a tool names for its own refusal (`read_only`, `invalid_argument`, `sql_too_long`, `sql_too_nested`, `parse_deadline`, `parse_no_worker`, `parse_busy`, `parse_failed`, `result_columns_too_long`) or a plugin with `note(error=...)`, else `tool_error`. The agent reads the whole message |
| `duration_ms` | how long the call took |
| `query_ids` | the ids Trino gave the call's queries, in order, a failed query's included: the key of Trino's query history (`system.runtime.queries`, `/v1/query`, the event listeners). Up to `MCP_AUDIT_MAX_NAMES`; past them, `query_ids_cut` says how many more there were |

The line has no field that could hold the bearer or the SQL text. What a call did (the SHA-256 of
its SQL, the tables, the rows, whether they were cut) is kept in its record while it runs
(`akko_mcp_trino.audit.note`); [`fingerprint-audit`](modules.md#fingerprint-audit) adds it to the
line, after the core's fields. Each list of the record keeps `MCP_AUDIT_MAX_NAMES` names (200 by
default) and counts the others, once each, at a constant cost: a search over a thousand catalogs
never writes a line without a limit. A field an extension adds never takes the place of a field
of the core: it is dropped and counted in `extra_dropped`.

### A refused request

Every request refused before a tool runs writes one line, with `"event":"refused"`, and counts in
`mcp_trino_request_refusals_total{reason}`. Real lines, a request without a token and a token
signed by another key:

```json
{"ts":"2026-11-28T16:15:18.547Z","level":"INFO","logger":"mcp.audit","event":"refused","request_id":"preuve-087-sans-jeton","reason":"missing_token","status":401,"client_ip":"172.19.0.9","subject":"","client_id":""}
{"ts":"2026-11-28T16:15:18.552Z","level":"INFO","logger":"mcp.audit","event":"refused","request_id":"preuve-087-forge","reason":"bad_signature","status":401,"client_ip":"172.19.0.9","subject":"","client_id":""}
```

| Field | Meaning |
|---|---|
| `ts`, `event` | when, and `refused` |
| `request_id` | as for a call; the client received it in `X-Request-Id` |
| `reason` | why, a stable code from the table below |
| `status` | the HTTP status of the answer; `0` over stdio |
| `client_ip` | as for a call |
| `subject`, `client_id` | those of a token that was verified (a refusal after the token, by an extension or by the transport); empty otherwise. A token the server could not verify names nobody, and none of its claims is written |

| Reason | Status | When |
|---|---|---|
| `missing_token` | 401 | no bearer, with `MCP_AUTH_REQUIRED=true` |
| `invalid_token` | 401 | a bearer that does not parse as a JWT, or any other failure of the check |
| `bad_signature` | 401 | signed by a key of the JWKS, but the signature does not match |
| `unknown_key` | 401 | the JWKS was read, and no key of it matches the token's `kid` (or the token names none) |
| `jwks_unreachable` | 401 | the last read of the JWKS failed, and the server holds no key for the token's `kid` |
| `wrong_algorithm` | 401 | an algorithm outside `MCP_JWT_ALGORITHMS`, or not the one its key declares |
| `wrong_audience` | 401 | no audience this server accepts, or the audience of Trino's tokens (`TRINO_TOKEN_EXCHANGE_AUDIENCE`) |
| `wrong_issuer` | 401 | another issuer than `MCP_OIDC_ISSUER` |
| `token_expired`, `token_not_yet_valid` | 401 | past `exp`, or before `nbf` or `iat`, beyond `MCP_JWT_LEEWAY_SECONDS` |
| `missing_claim` | 401 | no `exp`, `iss` or `aud` |
| `no_user_claim` | 401 | no usable `MCP_JWT_USER_CLAIM` |
| an extension's reason | its own | `client_not_allowed`, `insufficient_scope` with [`token-policy`](modules.md#token-policy); a reason that is not a short lower-case word is written and counted as `extension_refused` |
| `host_not_allowed` | 421 | a `Host` outside `MCP_ALLOWED_HOSTS` |
| `origin_not_allowed` | 403 | an `Origin` header while `MCP_ALLOWED_HOSTS` is set |
| `payload_too_large` | 413 | a body past the SDK's limit |
| `session_invalid` | 404 | an MCP session the server does not know (another replica's, or expired) |
| `not_found` | 404 | a path the server does not serve |
| `bad_request`, `method_not_allowed`, `not_acceptable`, `unsupported_media_type`, `conflict` | 400, 405, 406, 415, 409 | what the MCP transport refuses: an unreadable body, a method, an `Accept` or a `Content-Type` it does not serve |

The refusals at the door also carry `X-Reason` for the client (`unauthenticated` for the core's,
the extension's reason otherwise); the audit's `reason` is finer. A tool that refuses a statement
(`read_only`) or that Trino refuses (`USER_ERROR:PERMISSION_DENIED`) is a tool call with
`"ok":false`, not a refusal. Over stdio, a missing or invalid `MCP_USER_TOKEN` and an extension's
refusal at admission are written with `status` 0 before the server stops; in `oauth2` mode, an
admission refused on a call is written too. Requests to the health port (`/health`, `/ready`,
`/metrics`) are not audited.

### The client's address

Without `MCP_TRUSTED_PROXIES`, `client_ip` is the address of the peer of the TCP connection,
whatever `X-Forwarded-For` says: a header a client wrote is never taken for an address. Behind an
ingress, a router or a gateway, set `MCP_TRUSTED_PROXIES` to their addresses, as CIDR (chart:
`server.trustedProxies`). The server then reads `X-Forwarded-For` from the right, skips the
trusted proxies, and keeps the first address that is not one; a malformed entry stops the reading
at the last trusted hop.

### Joining Trino's history

| Mode | Joined by |
|---|---|
| every mode | `query_ids` of the line = `query_id` in Trino's history; `trino_user` = Trino's `user`; `source` = `MCP_SERVER_NAME` or `TRINO_SOURCE` |
| with `trino-tags` | each query carries `request_id=<id>` and `tool=<tool>` in its client tags: the line's `request_id` is in Trino's `clientTags` |
| with `fingerprint-audit` | `sql_sha256` = the SHA-256 of the text Trino keeps |

What Trino kept of the call above, and of a second call of the same request that Trino refused
(`/v1/query`):

```text
20261128_161517_00302_qyb4k user=A12345 source=trino-mcp clientTags=['request_id=preuve-087-appel', 'tool=execute_query'] FINISHED
20261128_161518_00303_qyb4k user=A12345 source=trino-mcp clientTags=['request_id=preuve-087-appel', 'tool=execute_query'] FAILED PERMISSION_DENIED
```

In `service` mode, Trino sees `TRINO_USER` for every caller: the line's `subject` and `client_id`
are the only record of who called. Keep the audit lines as long as Trino's history.

### Sending the lines to a SIEM

- Set `MCP_LOG_FORMAT=json`: each line is one JSON object, which a collector (Fluent Bit,
  Vector, the OpenTelemetry Collector, the platform's) reads without a parser. Route the
  `mcp.audit` logger, or the lines that have an `event` field, to the audit index.
- `ts` is the server's clock, in UTC. Synchronize the nodes with a documented reference source
  (NTP): an audit trail is only as good as its clock.
- The integrity of the lines (write once, signed or hashed storage) and how long they are kept
  are the platform's, according to the organization's policy: the server writes to stderr and
  keeps nothing. A collector that loses lines loses them silently; watch its own metrics.
- Alert on the rate of `mcp_trino_request_refusals_total` by reason: a burst of
  `bad_signature` or `wrong_audience` is a token forged or used against the wrong server, a
  burst of `client_not_allowed` an application that should not call.
- These lines answer what the texts ask of an audit trail: who, when, from where, what, with
  which outcome, and the failed access attempts. Regulation (EU) 2022/2554 (DORA), through
  Commission Delegated Regulation (EU) 2024/1774, article 12, asks for logged events, logical
  access and identities, and a clock synchronized on a documented reference
  ([EUR-Lex](https://eur-lex.europa.eu/eli/reg_del/2024/1774/oj)); the OWASP
  [MCP Security Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/MCP_Security_Cheat_Sheet.html)
  and the OWASP [MCP Top 10](https://owasp.org/www-project-mcp-top-10/) (MCP08, lack of audit and
  telemetry) ask to log each tool call with its user and its time. Compliance is the deployment's,
  not a component's.

### Profiles

| Setting | Test (a workstation, stdio, `service`) | Production (HTTP, the chart) |
|---|---|---|
| `MCP_LOG_FORMAT` | `text`, stderr kept by the host | `json`, read by the cluster's collector |
| `MCP_EXTENSIONS` | `fingerprint-audit,trino-tags` advised | `token-policy,fingerprint-audit,trino-tags` (`token-policy` when the identity provider gives scopes) |
| `MCP_TRUSTED_PROXIES` | none | the ingress controller or the router; the edge sets `X-Request-Id` |
| identity | `service`, announced at start | `jwt` or `impersonation`, the door strict; `service` only for a named technical account |
| health port | none (stdio) | `networkPolicy.enabled: true`, `/metrics` open to Prometheus only |
| OpenTelemetry | none | none, or an SDK the platform installs on purpose (see below) |

In the chart, the production profile reads:

```yaml
server:
  logFormat: json
  extensions: [token-policy, fingerprint-audit, trino-tags]
  trustedProxies: ["10.128.0.0/14"]   # the router's or the ingress controller's pods
networkPolicy:
  enabled: true
```

### Telemetry and OpenTelemetry

The server sends nothing to its publisher nor to any third party: it talks to Trino, to the
identity provider (JWKS, token exchange) and to the context providers it is configured with,
nothing else.

The MCP SDK it is built on wraps every message it receives in an OpenTelemetry span, through the
`opentelemetry-api` package alone. Without an OpenTelemetry SDK installed in the environment, the
API does nothing and no span leaves the process: this is the image as published. When the
platform installs and configures an SDK (an auto-instrumentation agent and its `OTEL_*`
variables), these spans go to the exporter it configures. They carry `mcp.method.name`,
`mcp.protocol.version`, `jsonrpc.request.id`, and for a tool call `gen_ai.operation.name`
(`execute_tool`) and `gen_ai.tool.name`; no SQL and no token. An exception the SDK does not
expect puts its message in the span's status, which the audit never does: install an SDK only
where those messages may go. An instrumentation of the HTTP client libraries, if the platform
adds one, also traces the requests to Trino.

`OTEL_PYTHON_TRACER_PROVIDER` naming a provider that no installed package provides (a platform
that sets the OpenTelemetry variables on every pod) would stop the SDK at its first import. The
server drops that variable before, says so at start and in `--check`, and emits no span.

## Health and metrics

These endpoints exist over the HTTP transports only, on the health port.

- `/health` never touches Trino, so a liveness probe cannot kill the server because Trino is slow.
- What `/ready` checks depends on the identity mode: in `jwt` mode, Trino's public `/v1/info`; in
  `impersonation` and `service` modes, a bounded `SELECT 1` as `TRINO_USER`, impersonating no
  one. The probe's queries are not counted in `mcp_trino_queries_total`, nor in its siblings.
  When the check fails, `/ready` returns `503` with `{"status": "error", "reason": "<code>"}`,
  never the exception, whose text names the address of Trino: `trino_starting`,
  `trino_unavailable` (an HTTP 502, 503 or 504, or `/v1/info` not answering 200),
  `trino_auth_failed`, `trino_permission_denied`, `trino_query_failed`, `trino_http_error`,
  `trino_tls_failed`, `trino_timeout`, `trino_unreachable` (a name that does not resolve, a
  connection refused), `identity_refused` or `trino_error`. The exception goes to the server's
  log once each time the reason changes, and so does the return to ready:

  ```json
  {"ts":"2026-11-28T16:15:19.126Z","level":"WARNING","logger":"akko_mcp_trino.health","msg":"not ready: trino_unreachable (TrinoConnectionError: failed to execute: HTTPConnectionPool(host='trino-absent', port=8080): Max retries exceeded with url: /v1/statement (Caused by NameResolutionError(\"HTTPConnection(host='trino-absent', port=8080): Failed to resolve 'trino-absent' ([Errno -2] Name or service not known)\")))"}
  ```
- `/metrics` exposes `mcp_trino_queries_total`, `mcp_trino_query_errors_total`,
  `mcp_trino_query_duration_seconds` and `mcp_trino_sql_refusals_total`, the statements refused
  before Trino by `reason`: `too_long`, `too_nested`, `caller_busy`, `no_worker`, `deadline` (a
  parse cut by the deadline) or `worker_failed`; never a statement or a name. It also exposes
  `mcp_trino_request_refusals_total`, the requests refused before any tool ran, by the `reason`
  of their [audit line](#a-refused-request), and `mcp_trino_ready_checks_total`, the readiness
  probes, by `result` (`ready` or the code of the reason). With context providers loaded, it also exposes, by provider,
  `mcp_trino_context_calls_total`, `mcp_trino_context_errors_total` (by status),
  `mcp_trino_context_call_duration_seconds`, `mcp_trino_context_cache_lookups_total` (hit or
  miss), `mcp_trino_context_cache_bytes` and `mcp_trino_context_breaker_open`. A call the tool
  stops waiting for is counted as one that ended, each source read as `partial` or
  `unavailable`; a server stopping during a read counts a few such calls. With token
  verification on, it exposes `mcp_trino_jwks_fetches_total` (by `result`, `ok` or `failed`) and
  `mcp_trino_jwks_up`, 0 after a failed read of the JWKS
  ([Security](security.md#authentication)).

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
