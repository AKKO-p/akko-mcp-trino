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

A catalog, schema or table name given to a tool is taken as written: any text of 1 to 1024
characters without a control character, such as `orders$snapshots`, `2024_sales` or `données`.
It enters SQL only as a quoted identifier, each double quote in it doubled, or as a string
literal. A name with a control character, a bidirectional control (U+061C, U+200E, U+200F,
U+202A to U+202E, U+2066 to U+2069) or a line or paragraph separator (U+2028, U+2029) is refused
before Trino is asked, and the refusal shows it escaped; the zero-width joiner and non-joiner,
which some scripts need, are accepted.
`execute_query` accepts any SQL Trino accepts, **as long as it is a read**.

- **Commands go through an allow list**: `SELECT`, `DESCRIBE` of a table, `SHOW CATALOGS`,
  `SHOW SCHEMAS`, `SHOW TABLES`, `SHOW COLUMNS` and `SHOW FUNCTIONS`. Any other command is
  refused, including one a later Trino release adds.
- Some reads, such as `SHOW STATS` or `EXPLAIN`, can show facts beyond a row filter or a
  column mask. They are refused unless the administrator names them in
  `MCP_EXTRA_READ_COMMANDS` (`EXPLAIN`, `SHOW STATS`, `SHOW CREATE`, `SHOW GRANTS`,
  `SHOW ROLES`, `SHOW SESSION`).
- **A statement is bounded before it is parsed, and parsed within a deadline.** One longer than
  `MCP_MAX_SQL_CHARS` characters (100000 by default), or whose brackets nest deeper than
  `MCP_MAX_SQL_NESTING` (32 by default), is refused before the parser sees it. A statement within
  these bounds is parsed once, in one of `MCP_SQL_PARSE_WORKERS` worker processes (2 by default),
  never in the server's own process, and every check reads that one analysis. A worker that has
  not answered within `MCP_SQL_PARSE_SECONDS` (2 by default) is killed and replaced, and the call
  is refused (`time_budget`); so is a call that finds no worker free within that time, and a
  worker that fails refuses its call too. Deeply nested or repeated SQL therefore no longer ties
  up the server. A caller has one statement checked at a time, so it holds one worker at most: a
  second call while the first is checked waits its turn, and is refused (`busy`) if its turn has
  not come within `MCP_SQL_PARSE_SECONDS`. Calls without a verified caller count as the
  configured Trino account and share one such turn. A call waits at most twice
  `MCP_SQL_PARSE_SECONDS`: for its turn and a worker, then for its answer. The workers start on first use, on Linux, macOS and Windows
  alike, write nowhere (their standard streams are the null device, and the protocol runs on
  pipes of their own), get none of the server's settings, and end with the server.
  The remaining risk: an authenticated caller can keep one worker busy for the deadline at a
  time, so as many callers as there are workers can delay the others; per-user quotas are
  planned.
- **What an agent reads is bounded, and a comment is untrusted text.** An `execute_query` answer
  holds at most `MCP_MAX_RESULT_BYTES` bytes of JSON (1 MiB by default) and a cell at most
  `MCP_MAX_CELL_CHARS` characters (4000 by default), whatever type it has, so one huge cell or a
  wide result cannot flood an agent's context; the answer says what it cut (`truncated_reason`,
  `cut_columns`). A comment Trino holds on a table or a column, in `describe_table`,
  `describe_schema` and `search_columns`, is cleaned of control, bidirectional and zero-width
  characters and cut at `MCP_COMMENT_CHARS` characters (1000 by default), as catalog text is, and
  the entry says when it was cut. Comments stay data: the descriptions of these three tools tell
  the model to read them as data, never as instructions.
- **Row values are data, and their hidden characters can be removed.** The rows of
  `execute_query`, the `sample` of `describe_table` and the `min` and `max` of `profile_table`
  hold what was written to the tables, sometimes by people outside the organisation (a form, a
  ticket, a review). The descriptions of these three tools tell the model to read them as data,
  never as instructions. With `MCP_CLEAN_CELLS=true`, the server also removes from their text
  values the characters it removes from comments: zero-width characters, bidirectional controls,
  the Unicode tag characters (U+E0001 and U+E0020 to U+E007F, the assigned characters of the
  block U+E0000 to U+E007F), which can carry text a human reader does not see, and the other
  control, format and separator characters; newline and tab stay. The answer names what it
  cleaned (`cleaned_columns`, `"cleaned"` in `profile_table`). The option is off by default, for
  three reasons. A query should return what the database holds. Some of these characters are
  legitimate: the zero-width non-joiner in Persian and Arabic text, the zero-width joiner in emoji
  sequences, bidirectional marks, carriage returns; removing them changes the value, so a filter
  on a cleaned value may not match. And only hidden text is removed: visible text written as an
  instruction stays, and so do the characters of other Unicode categories, such as variation
  selectors and unassigned code points. The option reduces the room for hidden instructions; it does not neutralise
  prompt injection. The audit record and the metrics are the same with it or without it.
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

## Verifying a release

Each release signs its images and its Helm chart with cosign, without a key: the signature
certifies that the release workflow of this repository, at the release's tag, produced that
digest, and it is recorded in Sigstore's public transparency log. Each image also carries a
signed SBOM (SPDX) and a signed build provenance (SLSA); the provenance is on GHCR, under the same
digest as on Docker Hub. The Python packages and the chart archive come with a Sigstore bundle
each, attached to the release. Releases up to 0.8.6 carry the provenance and the SBOM, not the
cosign signatures.

The image, its SBOM and its provenance (each command exits non-zero if the check fails):

```bash
VERSION=X.Y.Z            # the release to check
IDENTITY=https://github.com/AKKO-p/akko-mcp-trino/.github/workflows/release.yml@refs/tags/v$VERSION
ISSUER=https://token.actions.githubusercontent.com
IMAGE=docker.io/akkoai/akko-mcp-trino@sha256:...   # the digest of $VERSION or $VERSION-ubi9
cosign verify --certificate-identity "$IDENTITY" --certificate-oidc-issuer "$ISSUER" "$IMAGE"
cosign verify-attestation --type spdxjson --certificate-identity "$IDENTITY" \
  --certificate-oidc-issuer "$ISSUER" "$IMAGE" > /dev/null
cosign verify-attestation --type slsaprovenance1 --certificate-identity "$IDENTITY" \
  --certificate-oidc-issuer "$ISSUER" "ghcr.io/akko-p/akko-mcp-trino@${IMAGE#*@}" > /dev/null
```

The chart, from its OCI repository and from the release:

```bash
CHART=A.B.C              # the chart version of that release (its appVersion is $VERSION)
helm pull oci://ghcr.io/akko-p/charts/akko-mcp-trino --version "$CHART"
cosign verify --certificate-identity "$IDENTITY" --certificate-oidc-issuer "$ISSUER" \
  "ghcr.io/akko-p/charts/akko-mcp-trino:$CHART"
gh release download "v$VERSION" --repo AKKO-p/akko-mcp-trino --pattern "akko-mcp-trino-$CHART.tgz*"
sha256sum -c "akko-mcp-trino-$CHART.tgz.sha256"
cosign verify-blob --bundle "akko-mcp-trino-$CHART.tgz.sigstore.json" \
  --certificate-identity "$IDENTITY" --certificate-oidc-issuer "$ISSUER" "akko-mcp-trino-$CHART.tgz"
```

A Python package, from the release: `cosign verify-blob --bundle <file>.sigstore.json` with the
same identity and issuer. Without cosign, `gh attestation verify oci://<image> --repo
AKKO-p/akko-mcp-trino` checks the provenance with a GitHub account.

## Reporting a vulnerability

Report privately, by a GitHub security advisory or by email to **security@akko-ai.com**, as
[SECURITY.md](../../SECURITY.md) says, with its response times. Do not open a public issue.
