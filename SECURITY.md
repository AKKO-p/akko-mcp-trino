# Security policy

This server sits between AI agents and data. Take any weakness in it seriously;
we do.

## Supported versions

| Version | Supported |
|---|---|
| 0.6.x | yes |
| 0.5.x and earlier | no: `jwt` mode passes the caller's token on to Trino, and the defaults serve requests without a token. Upgrade with [MIGRATING.md](MIGRATING.md). |

## Reporting a vulnerability

Email **security@akko-ai.com** with a description, the version, and a way to
reproduce. Do not open a public issue. You will get an acknowledgement within
three working days and a fix or a mitigation plan within thirty.

Please do not include real tokens, real data or real hostnames in a report.

## Security model

**Who Trino sees.** By default (`jwt`) every query runs as the verified caller.
The server accepts only a token issued for itself (its audience is
`MCP_OIDC_AUDIENCE` or `MCP_RESOURCE_URL`, RFC 8707), refuses a token issued for
Trino, and obtains a token for Trino by OAuth 2.0 Token Exchange (RFC 8693)
with its own client. The caller's token is never passed on, as the MCP
authorization specification requires. Without token exchange, `impersonation`
runs each query as the caller through the server's own account and an
impersonation rule only that account has. `service` runs every query as one
identity; it is never a default, and it is announced as a warning at start, in
`--check` and to every MCP host. In every mode, the caller's name comes from
their verified token, never from a header they wrote.

**The door.** A request without a valid token (none, forged, expired, or
issued for another audience) is refused with `401` by default. The modes that
run as the caller refuse to start behind a door that would let such a request
through.

**What read-only covers.** The read-only guard refuses every write that passes
through this server: each statement is parsed, and a write anywhere in its tree
is refused before Trino, as are table functions and the `system` catalog. It is
not a guarantee about the user's rights in Trino. In `jwt` and `impersonation`
modes, Trino runs the query as the user, with the rights its policy gives that
user: a user allowed to write can still write through any other Trino client,
with a token for Trino the identity provider gives them directly. The token the
server obtains by exchange stays in its memory and is never returned to the
agent or the host. In `service` mode the same holds for `TRINO_USER`, for
whoever holds its credential.

For read-only end to end, the engine must hold the line too:

- give the identities agents reach Trino with read-only rules in the policy
  engine (OPA, Ranger, or file-based rules such as
  `{"catalogs": [{"user": "mcp-reader", "catalog": ".*", "allow": "read-only"}]}`);
- in `jwt` mode, let only this server's client obtain tokens for Trino's
  audience by exchange;
- in `impersonation` mode, limit the impersonation rule to the users agents act
  for, and keep the account's credential like any service credential;
- refuse table functions and the `system` catalog in the policy as well: the
  guard is a second line, not the only one.

**Bounds.** A row cap (100), a time budget per query (60 s, cancelled in Trino)
and per tool call (120 s) apply by default; `0` removes a time budget, and that
is a choice to make explicitly.

## What counts

Anything that lets a caller read data their Trino identity could not read (the
caller's own in `jwt` and `impersonation` modes, the configured one in
`service` mode), run a write through any tool while `TRINO_READ_ONLY` is true,
reach Trino as another identity than the mode allows, get a token accepted
that was not issued for this server, make the server send Trino the caller's
own token or a token it did not obtain for Trino, act without a verified
identity while `MCP_AUTH_REQUIRED` is true, or extract a token, a client secret
or a password from a log, an error message or `--check`.
