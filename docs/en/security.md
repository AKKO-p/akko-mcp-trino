# Security

**English** · [Français](../fr/security.md)

## Read-only through this server

The read-only check covers **what passes through this server**. The server refuses every
statement an agent sends if a write appears anywhere in it, before Trino sees it. The check is
not a guarantee about what a user may do in Trino by other means.

In `jwt` and `impersonation` modes, Trino runs each query as the user, with the rights its policy
gives that user. If that policy lets the user write, the user can still write through any other
Trino client (the CLI, a notebook, a BI tool), with a token for Trino that the identity provider
gives them directly. The token the server obtains by exchange stays in memory and is never
returned to the agent or the host. This does not change the user's rights in Trino. In `service`
mode, the same holds for `TRINO_USER`, for whoever holds its credential.

For read-only end to end, the engine must enforce it too:

- **`service`**: give `TRINO_USER` read rights only. With file-based access control, use
  `{"catalogs": [{"user": "mcp-reader", "catalog": ".*", "allow": "read-only"}]}`; with OPA or
  Ranger, a policy that grants this identity `SELECT` and nothing else.
- **`impersonation`**: limit the impersonation rule to the users the agents may act for. If those
  users may write elsewhere, give the server's account no rights of its own beyond `SELECT 1`.
- **`jwt`**: let only this server's client obtain tokens for Trino's audience by exchange, so that
  a token held by the agent's host never opens Trino. In the policy engine, give the users that
  agents act for read-only rules on the catalogs those agents may reach. Trino applies the same
  rules to a query whatever client sent it, so the policy is where a user's rights become
  read-only for every path, this one included.

The server's check is a second line of defense, not the only one. The engine's policy must also
refuse table functions and the `system` catalog (see [Design notes](design-notes.md)), **except**
reads of `system.jdbc` and `system.metadata`. Trino filters these for each user, as it does
`information_schema`: a user sees there only the tables and columns they may see elsewhere.
`describe_schema` and `search_columns` read `system.jdbc.columns` and
`system.metadata.table_comments` with statements the server builds. A policy that refuses them
makes these tools return an error that says so, and no other way is tried. The agent's own SQL
stays refused on all of `system`, these views included.

Also set `hide-inaccessible-columns=true` in Trino's `config.properties`. Without it, a
`SELECT *` over a table with a column the policy hides is refused, and the refusal names that
column. With it, the hidden column is dropped from the result and named nowhere.

## Trino first

**The server does not decide access.** Trino does, for each caller or for the configured
identity. Putting access rules in the server would duplicate, and eventually contradict, what the
engine already enforces for every other client.

In `service` mode, Trino decides for the one identity you give the server. Give that identity
read-only rights, and only on data every caller may read (see
[Identity](identity.md#one-identity-for-every-caller-service-only-when-named)).

## The statements an agent may send

Every identifier is validated (`[A-Za-z_][A-Za-z0-9_-]*`) before it is placed in SQL.
`execute_query` accepts any SQL Trino accepts, **as long as it is a read**.

- **Commands go through an allow list**: `SELECT`, `DESCRIBE` of a table, `SHOW CATALOGS`,
  `SHOW SCHEMAS`, `SHOW TABLES`, `SHOW COLUMNS` and `SHOW FUNCTIONS`. Any other command is
  refused, including one a later Trino release adds.
- Some reads, such as `SHOW STATS` or `EXPLAIN`, can show facts beyond a row filter or a
  column mask. They are refused unless the administrator names them in
  `MCP_EXTRA_READ_COMMANDS` (`EXPLAIN`, `SHOW STATS`, `SHOW CREATE`, `SHOW GRANTS`,
  `SHOW ROLES`, `SHOW SESSION`).
- **A statement is bounded before it is parsed.** One longer than `MCP_MAX_SQL_CHARS` characters
  (100000 by default), or whose brackets nest deeper than `MCP_MAX_SQL_NESTING` (12 by default),
  is refused before the parser sees it, so deeply nested SQL no longer ties up the server. A
  statement within the bounds is parsed once, and every check reads that one tree.
- **Writes are refused anywhere in the tree.** The statement is parsed into an AST. It is refused
  if it holds more than one statement, or if an `INSERT`, `UPDATE`, `DELETE`, `MERGE`, `CREATE`,
  `DROP`, `ALTER`, `GRANT`, `CALL` or `SET` appears anywhere in the tree, including inside a CTE,
  a subquery, or the statement an `EXPLAIN` explains. `EXPLAIN ANALYZE` is refused, because it
  runs what it explains, and so is an `EXPLAIN` of an `EXPLAIN`.
- **Table functions and `system` are refused**, and so is any `SHOW` that names `system`. The
  metadata tools refuse `system` as a catalog name too, and `list_catalogs` does not list it.
- **A name without a catalog** is read in the session catalog (`TRINO_CATALOG`, `system` by
  default). When that is `system`, a table without a catalog is refused (a CTE is not a table).
  Only `SHOW CATALOGS`, and a `SHOW SCHEMAS`, `SHOW TABLES` or `SHOW COLUMNS` whose `FROM` gives
  the full name, catalog included, get through.

So set `TRINO_CATALOG` to a catalog that holds data, never `system`. The engine's policy must also refuse `system`, except
reads of `system.jdbc` and `system.metadata` (see
[Read-only through this server](#read-only-through-this-server)); the server's check is a second
line of defense, not the only one.

## The rights of the context

Context from a data catalog follows what Trino lists for the caller. A name that comes from a
data catalog is never used before Trino has listed it for that caller. A provider never receives
the Trino client or the caller's identity, and it never writes to its data catalog. The text of a
provider's error never reaches the agent, only a status. A right withdrawn in Trino can leave the
context of a table visible for up to `MCP_VISIBILITY_TTL_SECONDS`, never its data, which Trino
refuses at once. [Deployment](deployment.md#with-a-data-catalog) says which allow lists open what.

## Audit

Every tool call writes one audit line, keyed by `X-Request-Id`. The line never holds the token
or the SQL text: it says who called which tool, with which outcome, and for how long.
[Operations](operations.md#audit) gives its fields.

## Reporting a vulnerability

Email **security@akko-ai.com**, as [SECURITY.md](../../SECURITY.md) says. Do not open a public
issue.
