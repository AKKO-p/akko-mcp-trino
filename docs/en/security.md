# Security

**English** · [Français](../fr/security.md)

## Read-only through this server

The read-only guard covers **what passes through this server**: every statement
an agent sends is refused if a write appears anywhere in it, before Trino sees
it. It is not a guarantee about what a user may do in Trino by other means.

In `jwt` and `impersonation` modes, Trino runs each query as the user, with the
rights its policy gives that user. If that policy lets the user write, the user
can still write through any other Trino client (the CLI, a notebook, a BI
tool), with a token for Trino the provider gives them directly. The token the
server obtains by exchange is held in memory and never returned to the agent or
the host, but the user's own rights in Trino are what they are. In `service`
mode the same holds for `TRINO_USER`, for whoever holds its credential.

For read-only end to end, make the engine hold the line too:

- **`service`**: give `TRINO_USER` read rights only. With file-based access
  control, `{"catalogs": [{"user": "mcp-reader", "catalog": ".*", "allow": "read-only"}]}`;
  with OPA or Ranger, a policy granting that identity `SELECT` and nothing else.
- **`impersonation`**: limit the impersonation rule to the users the agents may
  act for, and, if those users may write elsewhere, give the server's account
  no rights of its own beyond `SELECT 1`.
- **`jwt`**: let only this server's client obtain tokens for Trino's audience by
  exchange, so that a token the agent's host holds never opens Trino; and give
  the users reached through agents read-only rules on the catalogs agents may
  reach, in the policy engine. Trino applies the same rules to a query whatever
  client sent it, so the policy is where a user's rights are read-only for
  every path, this one included.

The guard stays a second line, not the only one: the engine's policy must
refuse table functions and the `system` catalog too (see
[Design notes](design-notes.md)), **except** reading `system.jdbc` and
`system.metadata`, which Trino filters for each user, as it does
`information_schema`: a user sees there only the tables and columns they may see
elsewhere. `describe_schema` reads `system.jdbc.columns` and
`system.metadata.table_comments`, and so does `search_columns`, by statements the
server builds; a policy that refuses them makes them answer an error that says so,
with no other way tried. The agent's own SQL stays refused on all of `system`, these
views included.

Set `hide-inaccessible-columns=true` in Trino's `config.properties` too. Without it, a
`SELECT *` over a table with a column the policy hides is refused, and the refusal
names that column; with it, the hidden column is dropped from the result and named
nowhere.

## Trino first

**The server does not decide access.** Trino does, for each caller or for the
configured identity. Putting policy in the server would duplicate, and eventually
contradict, what the engine already enforces for every other client.

In `service` mode, Trino decides for the one identity you give the server: give that identity the
rights every caller may have, no more, and read-only rights only (see
[Identity](identity.md#one-identity-for-every-caller-service-only-when-named)).

## The statements an agent may send

Every identifier is validated (`[A-Za-z_][A-Za-z0-9_-]*`) before it is placed
in SQL. `execute_query` accepts any SQL Trino accepts, **as long as it is a
read**. Commands go by an allow-list: `SELECT`, `DESCRIBE` of a table, and
`SHOW CATALOGS`, `SHOW SCHEMAS`, `SHOW TABLES`, `SHOW COLUMNS` and
`SHOW FUNCTIONS`. Any other command is refused, including one a later Trino
adds. Some reads, such as `SHOW STATS` or `EXPLAIN`, can show facts
beyond a row filter or a column mask; they are refused unless the administrator
names them in `MCP_EXTRA_READ_COMMANDS` (`EXPLAIN`, `SHOW STATS`, `SHOW CREATE`,
`SHOW GRANTS`, `SHOW ROLES`, `SHOW SESSION`). An answer carries only columns and
rows, never Trino's statistics of the query. The statement is parsed into
an AST and refused if it is more than one statement, or if an `INSERT`, `UPDATE`, `DELETE`, `MERGE`, `CREATE`, `DROP`,
`ALTER`, `GRANT`, `CALL` or `SET` appears anywhere in the tree, including
inside a CTE, a subquery, or the statement an `EXPLAIN` explains. `EXPLAIN
ANALYZE` is refused: it executes what it explains. Table functions and the
`system` catalog are refused, and so is any `SHOW` that names `system`. A name
without a catalog is read in the session catalog (`TRINO_CATALOG`, `system` by
default); when that is `system`, a table without a catalog is refused (a CTE
is not a table), and only `SHOW CATALOGS` and a `SHOW SCHEMAS`, `SHOW TABLES`
or `SHOW COLUMNS` whose `FROM` gives the full name, catalog included, get
through. The metadata tools refuse `system` as a catalog name too, and
`list_catalogs` does not list it. So set `TRINO_CATALOG` to a data catalog;
the engine's policy must refuse `system` too, except reading `system.jdbc` and
`system.metadata` (see
[Read-only through this server](#read-only-through-this-server)), the guard being a second
line.

## The rights of the context

Context from a catalogue follows what Trino lists for the caller: a name that comes from a
catalogue is never used before Trino has listed it for that caller, a provider never receives
the Trino client or the caller's identity, and it never writes to its catalogue. The text of a
provider's error never reaches the agent, only a status. A right withdrawn in Trino can leave the
context of a table visible for up to `MCP_VISIBILITY_TTL_SECONDS`, never its data, which Trino
refuses at once. [Deployment](deployment.md#deployment-with-a-catalogue) says which allow lists
open what.

## Audit

Every tool call writes one audit line, keyed by `X-Request-Id`, never the token, never the SQL
text: who called which tool, with which outcome, for how long. [Operations](operations.md#audit)
gives its fields.

## Reporting a vulnerability

Email **security@akko-ai.com**, as [SECURITY.md](../../SECURITY.md) says. Do not open a public
issue.
