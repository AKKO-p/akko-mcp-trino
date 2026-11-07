# Modules

**English** · [Français](../fr/modules.md)

The core ships three modules: `token-policy`, `fingerprint-audit` and `trino-tags`. Each is off
until `MCP_EXTENSIONS` names it (comma-separated). In code, a module is an extension
(`akko_mcp_trino.extensions.Extension`), loaded like any other.

The order in `MCP_EXTENSIONS` matters when two modules or extensions touch the same thing:

- if two publish a document at the same path, the last one wins;
- for a refusal, the first one that returns a challenge wins;
- audit wrappers are applied in that order.

## token-policy

A verified token must also name an allowed client and carry the required scopes. The client is
the first of `MCP_JWT_CLIENT_CLAIMS` that the token carries, and its scopes the first of
`MCP_JWT_SCOPE_CLAIMS`. Both are compared exactly, case included, with no wildcard.

- A client that is not allowed gets the 401 of an invalid token (`X-Reason: client_not_allowed`).
  It is never told which scope would let it in, since the client is checked first.
- A token without every required scope gets a 403 `insufficient_scope`. Its `WWW-Authenticate`
  names the scopes to ask for (RFC 6750 §3.1), and the protected resource metadata adds
  `scopes_supported`.
- A request without a verified token, which the core lets through when `MCP_AUTH_REQUIRED=false`,
  gets the core's 401 and challenge, never a 403.

Every client and scope must be an OAuth scope-token (printable ASCII, no space, no quote, no
backslash); otherwise the server refuses to start. It also refuses to start when neither list is
set, since the module would enforce nothing. The presence of a claim is what counts: the first
one the token carries is read, whatever it holds, so an empty `scp` never falls back to `scope`.
List only claims that the identity provider sets itself. Never list a claim that is filled from an
attribute the user can edit.

For example, the integration suite (`integration/compose.yaml`, server `mcp-scoped`) sets:

```sh
MCP_EXTENSIONS=token-policy
MCP_ALLOWED_CLIENTS=mcp-host
MCP_REQUIRED_SCOPES=data:query
```

A token of the client `mcp-host` without the scope `data:query` gets this answer:

```http
HTTP/1.1 403 Forbidden
X-Reason: insufficient_scope
WWW-Authenticate: Bearer resource_metadata="http://mcp-scoped:3000/.well-known/oauth-protected-resource/mcp", error="insufficient_scope", scope="data:query"

{"error":"insufficient_scope","message":"the token lacks a scope this server requires: ask the identity provider for a token with the scopes the WWW-Authenticate header names"}
```

The protected resource metadata names the scope:

```json
{
  "resource": "http://mcp-scoped:3000/mcp",
  "authorization_servers": ["https://keycloak:8443/realms/integration"],
  "bearer_methods_supported": ["header"],
  "scopes_supported": ["data:query"]
}
```

Here are the claims, by identity provider:

- Keycloak: the defaults (`MCP_JWT_CLIENT_CLAIMS=azp,client_id`, `MCP_JWT_SCOPE_CLAIMS=scope`), tested by the integration suite.
- Entra ID: `MCP_JWT_SCOPE_CLAIMS=scp`; the client is in `azp` or `appid` depending on the token version, so check one of your tokens. Not tested live.
- Okta: `MCP_JWT_SCOPE_CLAIMS=scp`, `MCP_JWT_CLIENT_CLAIMS=cid`. Not tested live.

## fingerprint-audit

This module adds to the audit line what the call did, after the core's fields, in this order:
`request_id`, `tool`, `subject`, `token_id` (the token's `jti`, never the token), `ok`, `error`,
`sql_sha256`, `tables`, `duration_ms`, `rows`, `truncated`, `metadata_served` (the tables whose
data catalog description reached the caller). The SQL text still has no field.

`sql_sha256` is the SHA-256 of the statement sent to Trino for the caller. A tool that sends
several keeps the last one it noted, the one that answered the agent (the aggregate of
`profile_table`, the sample of `describe_table`). Trino's query history keeps the text, and a
policy engine that records it gets the same digest: use the digest to join them.

Each list keeps `MCP_AUDIT_MAX_NAMES` names. Past them, `tables_cut` or `metadata_served_cut`,
right after the list, says how many more there were, each counted once. A field that another
extension adds to the line never overwrites one of these: it is dropped and counted in
`extra_dropped`. The server wraps the audit in the order of `MCP_EXTENSIONS`, each wrapper around
the one before, so name `fingerprint-audit` before any other extension that wraps the audit.

Here is the line of an `execute_query` call in the integration suite. The subject and the ids are
replaced by fictitious ones; the field order and the digest are those of the run:

```json
{
  "request_id": "req-docs-0001",
  "tool": "execute_query",
  "subject": "alice_user",
  "token_id": "jti-docs-0001",
  "ok": true,
  "error": "",
  "sql_sha256": "595c26467ad8ea2d0a0857b4bf5c094d6f6d6bfc8c88ebc289f03a523ce7faf5",
  "tables": ["tpch.tiny.region"],
  "duration_ms": 1675,
  "rows": 1,
  "truncated": false,
  "metadata_served": []
}
```

## trino-tags

Every query of a call carries two client tags (`X-Trino-Client-Tags`), each only when known:
`request_id=<id>`, the request id sent by the proxy or gateway in front of the server, or made by
the server, and `tool=<tool>`. Trino keeps them with the query, so its query history, its
resource groups and a policy engine that records them see which call of the server sent it.

The core keeps an incoming request id only when it is a plain identifier, and the module keeps a
tool name only when it is one too, else `tool=other`. No comma and no `=` can therefore add a tag
of its own. The visibility filter's tag (`mcp-gate`) and the profile's tag (`mcp-profile`) come in
addition.

Here is a resource group for the queries the agents write themselves. The figures are a starting
point, not a measurement, and every selector names a leaf group:

```json
{
  "rootGroups": [
    {
      "name": "global",
      "softMemoryLimit": "80%",
      "hardConcurrencyLimit": 100,
      "maxQueued": 1000,
      "subGroups": [
        {"name": "agents", "softMemoryLimit": "30%", "hardConcurrencyLimit": 10, "maxQueued": 100},
        {"name": "other", "softMemoryLimit": "50%", "hardConcurrencyLimit": 90, "maxQueued": 900}
      ]
    }
  ],
  "selectors": [
    {"clientTags": ["tool=execute_query"], "group": "global.agents"},
    {"group": "global.other"}
  ]
}
```

In the integration suite, an `execute_query` call sent with `X-Request-Id: req-docs-0001`
ran its query with these client tags:

```text
X-Trino-Client-Tags: request_id=req-docs-0001,tool=execute_query
```

Trino's `system.runtime.queries` shows the query in the group that the selector on
`tool=execute_query` names (`integration/trino/resource-groups.json`, with the same selectors as
above):

```text
query_id                     resource_group_id    source
20261106_173554_00003_ppe7v  [global, agents]     trino-mcp
```

## Settings

`MCP_ALLOWED_CLIENTS` and `MCP_REQUIRED_SCOPES` are in [Configuration](configuration.md#modules),
`MCP_AUDIT_MAX_NAMES` in [its audit table](configuration.md#audit), and `MCP_JWT_CLIENT_CLAIMS`
and `MCP_JWT_SCOPE_CLAIMS` in [its identity table](configuration.md#identity).
