# Security policy

**English** · [Français](SECURITY.fr.md)

This server sits between AI agents and data. Please report any weakness you find.

## Supported versions

| Version | Supported |
|---|---|
| 0.8.x | yes: every fix, in the latest 0.8 release |
| 0.7.x | security fixes until 28 February 2027, then no |
| 0.6.x and earlier | no. Upgrade with [MIGRATING.md](MIGRATING.md); 0.5.x and earlier passed the caller's token on to Trino in `jwt` mode and served requests without a token by default. |

How long a minor version is supported, and what a release may change, is set out in the
[versioning policy](docs/en/versioning.md).

## Reporting a vulnerability

Report privately, by either channel:

- a private report on GitHub: **Security**, then **Report a vulnerability**
  ([security advisories](https://github.com/AKKO-p/akko-mcp-trino/security/advisories/new));
- an email to **security@akko-ai.com**.

Give a description, the version, and a way to reproduce. Do not open a public issue, and do not
include real tokens, real data or real hostnames.

| Step | Within |
|---|---|
| Acknowledgement | 48 hours |
| First assessment (confirmed or not, severity) | 7 days |
| Fix released, critical or high severity | 30 days |
| Fix released, other severities | 90 days |

Disclosure is coordinated: the fix is released first, then a GitHub security advisory describes
the weakness, the versions affected and the fixed version, with a CVE when one applies, and
credits the reporter unless they ask otherwise. A report that is not fixed within 90 days may be
disclosed by its author, after telling us.

## Telemetry

The server sends no telemetry, to its authors or to anyone else. It connects only to Trino, to
the identity provider configured (its keys, and the token endpoint for token exchange) and to the
context providers configured; `/health`, `/ready` and `/metrics` are served locally, on the
health port, to whoever reaches it. One nuance: the MCP SDK it is built on carries an
OpenTelemetry instrumentation that does nothing unless the environment installs an OpenTelemetry
SDK (an operator that injects one into every pod, for instance). Then the SDK's spans for each
MCP message go to the exporter that the environment sets (`OTEL_*`); they carry no SQL.

## Verifying a release

Each image and the Helm chart are signed by cosign without a key, by the release workflow of this
repository at the release's tag; each image carries a signed SBOM and a signed build provenance,
and the Python packages a Sigstore bundle each. The commands are in
[docs/en/security.md](docs/en/security.md#verifying-a-release).

## Security model

**Who Trino sees.** By default (`jwt`) every query runs as the verified caller.
The server accepts only a token issued for itself (its audience is
`MCP_OIDC_AUDIENCE` or `MCP_RESOURCE_URL`, RFC 8707), refuses a token issued for
Trino, and obtains a token for Trino by OAuth 2.0 Token Exchange (RFC 8693)
with its own client. The caller's token is never passed on to Trino, as the MCP
authorization specification requires. Without token exchange, `impersonation`
runs each query as the caller through the server's own account and an
impersonation rule only that account has. `service` runs every query as one
identity; it is never a default, and it is announced as a warning at start, in
`--check` and to every MCP host. In every mode, the caller's name comes from
their verified token, never from a header they set.

**Token verification.** A request without a valid token (none, forged,
expired, or issued for another audience) is refused with `401` by default. The
server refuses to start in a mode that runs as the caller when token
verification would let such a request through.

**What read-only covers.** The read-only check refuses every write that passes
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
  server's check is a second line of defense, not the only one.

**Bounds.** A row cap (100), a time budget per query (60 s, cancelled in Trino)
and per tool call (120 s) apply by default; `0` removes a time budget, and that
is a choice to make explicitly.

## What counts

A report qualifies if it lets a caller:

- read data that their Trino identity cannot read (the caller's own in `jwt` and
  `impersonation` modes, the configured one in `service` mode);
- run a write through any tool while `TRINO_READ_ONLY` is true;
- reach Trino as an identity the mode does not allow;
- get a token accepted that was not issued for this server;
- make the server send Trino the caller's own token, or any token it did not obtain for Trino;
- act without a verified identity while `MCP_AUTH_REQUIRED` is true;
- extract a token, a client secret or a password from a log, an error message or `--check`.
