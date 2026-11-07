# Modules

**English** · [Français](../fr/modules.md)

The core ships three extensions of its own, each off until `MCP_EXTENSIONS` names it
(comma-separated): `token-policy`, `fingerprint-audit` and `trino-tags`. Their order matters
where extensions meet: a public document of the same path is the last extension's, the
challenge of a refusal is the first extension's that answers one, and the audit is wrapped in
that order.

## token-policy

A verified token must also name an allowed client and carry the required
scopes. The client is the first of `MCP_JWT_CLIENT_CLAIMS` the token carries, and its scopes
the first of `MCP_JWT_SCOPE_CLAIMS`; both are compared exactly, case included, with no
wildcard. A foreign client gets the 401 of an invalid token (`X-Reason: client_not_allowed`),
and is never told which scope would let it in, since the client is checked first. A token
without every required scope gets 403 `insufficient_scope`, whose `WWW-Authenticate` names the
scopes to ask for (RFC 6750 §3.1), and the protected resource metadata adds
`scopes_supported`. A request without a verified token, which `MCP_AUTH_REQUIRED=false` lets
past the core, gets the core's 401 and challenge, never 403. Every client and scope must be an
OAuth scope-token (printable ASCII, no space, no quote, no backslash), or it refuses to start;
with neither list set, it refuses to start too: it would enforce nothing. The claims are read
by presence: the first one the token carries is read, whatever it holds, so an empty `scp`
never falls back to `scope`; list only claims the identity provider sets itself, never one an
attribute a user may edit feeds.

For example, in the integration suite (`integration/compose.yaml`, server `mcp-scoped`):

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

and the protected resource metadata names the scope:

```json
{
  "resource": "http://mcp-scoped:3000/mcp",
  "authorization_servers": ["https://keycloak:8443/realms/integration"],
  "bearer_methods_supported": ["header"],
  "scopes_supported": ["data:query"]
}
```

The claims, by identity provider:

- Keycloak: the defaults (`MCP_JWT_CLIENT_CLAIMS=azp,client_id`, `MCP_JWT_SCOPE_CLAIMS=scope`), tested by the integration suite.
- Entra ID: `MCP_JWT_SCOPE_CLAIMS=scp`; the client is in `azp` or `appid` depending on the token version, so check one of your tokens. Not tested live.
- Okta: `MCP_JWT_SCOPE_CLAIMS=scp`, `MCP_JWT_CLIENT_CLAIMS=cid`. Not tested live.

## fingerprint-audit

The audit line adds what the call did, after the core's fields, in
this order: `request_id`, `tool`, `subject`, `token_id` (the token's `jti`, never the token),
`ok`, `error`, `sql_sha256`, `tables`, `duration_ms`, `rows`, `truncated`, `metadata_served`
(the tables whose catalogue description reached the caller). The SQL text still has no field.
`sql_sha256` is the SHA-256 of the statement sent to Trino for the caller; a tool that sends
several keeps the last one it noted, the one that answered the agent (the aggregate of
`profile_table`, the sample of `describe_table`). Trino's query history keeps the text, and a
policy engine that records it hashes to the same digest: that is the join. Each list keeps
`MCP_AUDIT_MAX_NAMES` names; past them, `tables_cut` or `metadata_served_cut`, right after
the list, says how many more there were, each counted once. A field another extension adds to
the line never overwrites one of these: it is dropped and counted in `extra_dropped`. The
server wraps the audit in the order of `MCP_EXTENSIONS`, each around the one before: name
`fingerprint-audit` before any other extension that wraps the audit.

For example, the line of an `execute_query` call in the integration suite (the subject and the
ids are replaced by fictitious ones; the field order and the digest are those of the run):

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

Every query of a call carries two client tags (`X-Trino-Client-Tags`):
`request_id=<id>`, the request id the edge sent or the server made, and `tool=<tool>`, each
only when known. Trino keeps them with the query, so that its query history, its resource
groups and a policy engine that records them see which call of the server sent it. The core
keeps an edge's request id only when it is a plain token, and the module keeps a tool name
only when it is one too, else `tool=other`, so no comma and no `=` can add a tag of its own. The filter's tag (`mcp-gate`) and the profile's (`mcp-profile`) come in addition.
For example, a resource group for the queries the agents write themselves (the figures are an
example to adapt, not a measure; every selector names a leaf group):

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

In the integration suite, a call of `execute_query` sent with `X-Request-Id: req-docs-0001`
ran its query with these client tags:

```text
X-Trino-Client-Tags: request_id=req-docs-0001,tool=execute_query
```

and Trino's `system.runtime.queries` shows it in the group the selector on
`tool=execute_query` names (`integration/trino/resource-groups.json`, with the same selectors
as above):

```text
query_id                     resource_group_id    source
20261106_173554_00003_ppe7v  [global, agents]     trino-mcp
```

## Settings

`MCP_ALLOWED_CLIENTS`, `MCP_REQUIRED_SCOPES` and `MCP_AUDIT_MAX_NAMES` are in
[Configuration](configuration.md#modules), with `MCP_JWT_CLIENT_CLAIMS` and
`MCP_JWT_SCOPE_CLAIMS` in [its identity table](configuration.md#identity).
