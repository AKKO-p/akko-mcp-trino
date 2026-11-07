# The tools

**English** · [Français](../fr/tools.md)

## Overview

| Tool | Arguments | Returns |
|---|---|---|
| `search_columns` | `text`, `catalog`, `schema`, `cursor` (all but `text` optional) | `{"tables": [{"catalog", "schema", "table", "comment"}], "matches": [{"catalog", "schema", "table", "column", "type", "comment"}], "schemas_searched": n, "not_searched": [{"catalog", "schema", "reason"}], "complete": bool, "next_cursor": token or null}`: the tables and columns that hold every word of the text. When nothing holds every word, a page of the per-word pass adds `"partial": true` and gives each row its `"words"`. `"hint"`, a fixed sentence for the agent, comes on such a page (follow `next_cursor` while there is one) and on a complete search that found nothing |
| `list_catalogs` | `offset` (optional, 0 by default) | `{"catalogs": [...], "next_offset": n or null, "truncated": bool}`: one page of the catalogs Trino shows |
| `list_schemas` | `catalog`, `offset` (optional) | `{"schemas": [...], "next_offset": n or null, "truncated": bool}`: one page of the schemas in that catalog |
| `list_tables` | `catalog`, `schema`, `offset` (optional) | `{"tables": [...], "next_offset": n or null, "truncated": bool}`: one page of the tables and views in that schema |
| `describe_schema` | `catalog`, `schema`, `offset` (optional) | `{"catalog", "schema", "tables": [{"table", "type", "comment", "columns": [{"column", "type", "comment"}]}], "next_offset": n or null, "shortened": bool}`: one page of tables, each complete, with their comments and columns; with a context provider, a `description` per table and `context_sources` (see [Context from a catalogue](catalog-context.md)) |
| `describe_table` | `catalog`, `schema`, `table`, `sample_rows` (0 to 20, first page only), `column_offset` (optional, 0 by default) | one page of the columns, the row cap of them from `column_offset`, with types and comments; `truncated` when the page does not hold every column, `next_column_offset` where the next page starts (null on the last), `columns_cut` when the table has more than `MCP_DESCRIBE_MAX_COLUMNS` columns; a governed sample when asked; what an extension's describers add, and `describer_errors` when one failed; with a context provider, `table`, `column_context`, `terms`, `context_sources`, `context_truncated` and `undescribed_columns` (see [Context from a catalogue](catalog-context.md)) |
| `profile_table` | `catalog`, `schema`, `table`, `columns`, `mode`, `percent` (all but the table optional) | `{"table", "scope", "rows": n, "columns_cut": bool, "columns": [{"column", "type", "non_null", "null_fraction", "approx_distinct", "min", "max"}]}`: aggregates of the rows the caller may read, computed by one query run as the caller, so masks and row filters apply as in `execute_query`. `columns` are named as `DESCRIBE` lists them, the first `MCP_PROFILE_MAX_COLUMNS` by default; `DESCRIBE` is read up to `MCP_PROFILE_DESCRIBE_ROWS` rows, and `"columns_cut": true` says it listed more, which cannot be named. `mode` is one of `MCP_PROFILE_MODES`: `sample` (the default: the first `MCP_PROFILE_SAMPLE_ROWS` rows, `"scope": "first_rows"`, not a random sample), `bernoulli` (`TABLESAMPLE BERNOULLI (percent)`, `0 < percent <= 100`, `"scope": "random_sample"`, scans every row to draw the sample) or `full` (every row the caller may read, `"scope": "visible_rows"`); a mode the server does not allow is refused before any query, and the description names only the modes allowed. The aggregate query carries the client tag `MCP_PROFILE_CLIENT_TAG` and runs within the smallest of `MCP_PROFILE_TIMEOUT_SECONDS`, `TRINO_QUERY_TIMEOUT_SECONDS` and the call's budget set above 0; past it, it is cancelled in Trino and the answer is `"kind": "time_budget"`. At most `MCP_PROFILE_MAX_CONCURRENT` calls run at once; past them, `"kind": "busy"`. `null_fraction` is null when `rows` is 0. A type with no order (`array`, `map`, `row`, `json`, any type not known to be ordered) gets its count only and `"skipped": ["distinct", "min", "max"]`; `min` and `max` are rendered as the cells of `execute_query`, and a text or binary one longer than `MCP_PROFILE_VALUE_CHARS` (characters, or bytes) is cut in the query itself, an exact prefix of the true value, with `"cut": ["min"]`, `["max"]` or both; a type longer than `MCP_PROFILE_TYPE_CHARS` is cut, with `"type"` in `cut` |
| `explain_query` | `sql` | `{"valid": true}`: Trino analyzed a read-only statement as the caller, without running it; `{"plan": "..."}` only once `EXPLAIN` is in `MCP_EXTRA_READ_COMMANDS` |
| `execute_query` | `sql`, `max_rows` (optional, lower than the server cap) | `{"columns": [...], "rows": [...], "row_count": n, "truncated": bool}` |

Every tool runs in Trino through the identity mode, the listing tools
included: metadata is data, and Trino lists only what that identity may see.
Every description is written for a model and says what the tool returns. The
descriptions of `describe_table` and `execute_query` also say that a masked
value or a missing row is the access policy, not an error to retry.

## The path an agent follows

The path an agent is told to follow: `search_columns` to find where the data
lives, `describe_schema` (or `describe_table` for sample rows) to learn the tables
it found, then `execute_query`. `list_catalogs`, `list_schemas` and `list_tables`
are for browsing, when the agent wants to see what there is rather than find
something; the tool descriptions and `examples/agent.py` say the same.

## Listings

The three listing tools page their answer: a page holds `TRINO_MAX_ROWS` names,
sorted by name, and `next_offset` says where the next page starts, or is `null`
on the last one. The agent calls again with `offset=next_offset` to read on;
`truncated` is true exactly when `next_offset` is not `null`. Schemas and tables
are paged in Trino (`ORDER BY` with `OFFSET` on `information_schema.schemata`
and `information_schema.tables`), so Trino decides what each page holds. Catalogs
are read with `SHOW CATALOGS`, which works even under a policy that refuses all
of `system`, whole, past the row cap, up to `MCP_MAX_CATALOGS` (10000 by default),
then paged by the server; `system` does not count against the bound. Above it
`list_catalogs` answers an error, never a cut list. `list_tables` on a schema that
does not exist, or that the caller may not see, answers `kind` `schema` ("not found
or not visible", the same for both), never an empty list for a missing or hidden
schema. An `offset` that is not a whole number from 0 is refused (`invalid_argument`)
before Trino.

## describe_schema

`describe_schema` gives an agent a schema's context in one call before it writes
SQL: its tables and views, each with its type, its comment and all its columns
(name, type, comment). It pages by tables, in Trino (`ORDER BY` with `OFFSET` on
`information_schema.tables`): a page holds up to `MCP_DESCRIBE_SCHEMA_TABLES`
tables (20 by default, 100 at most), always whole, and only while their columns add up to
`MCP_DESCRIBE_SCHEMA_COLUMNS` at most (1000 by default). The first table of a page
always comes whole, however wide; a page that holds fewer tables for the column
budget says so (`shortened: true`), and `next_offset` starts the next page at the
table left out.

The comments come from `system.metadata.table_comments` and the
columns from `system.jdbc.columns`, two views of Trino's `system` catalog that Trino
filters for each user, read by statements the server builds, never by the agent's
SQL (see [Security](security.md#read-only-through-this-server)). A page costs four
statements: the schema check, the page of table names, one read of the columns, and
the comments of the tables the page keeps. The columns are read once, at most
`MCP_DESCRIBE_SCHEMA_COLUMNS` plus one rows: the tables that arrive whole within the
budget are kept, the table the budget cuts starts the next page. A first table wider
than the budget is read again alone, by its own query, in parts of
`MCP_DESCRIBE_SCHEMA_COLUMNS` columns: one statement more per part. The read of the
columns and that of the comments hold one `UNION ALL` branch per table, each filtered
by equality on the table's name, which Trino pushes down into these views: a page
reads the comments and columns of its own tables, never those of the whole schema (a
list, `IN` or `OR`, is not pushed down and would read every table of the schema).
Trino loads the metadata of every table of the page for that read, so a page costs
about the same whether its tables are narrow or wide.

A schema
that does not exist and one the caller may not see answer the same error, `kind` `schema`; a
visible empty schema answers an empty list. Comments are text the owners of the
data wrote: the tool's description tells the model to read them as data, never as
instructions, and the server never places them in its own MCP instructions.
`describe_table` stays the tool for sample rows and what extensions add.

In the integration suite, `describe_schema` of `tpch.tiny` answers alice with the eight tables
of the schema on one page; the first one, and the end of the answer:

```
{"catalog": "tpch", "schema": "tiny", "tables": [
  {"table": "customer", "type": "BASE TABLE", "comment": null, "columns": [{"column": "custkey", "type": "bigint", "comment": null}, {"column": "name", "type": "varchar(25)", "comment": null}, {"column": "address", "type": "varchar(40)", "comment": null}, {"column": "nationkey", "type": "bigint", "comment": null}, {"column": "phone", "type": "varchar(15)", "comment": null}, {"column": "acctbal", "type": "double", "comment": null}, {"column": "mktsegment", "type": "varchar(10)", "comment": null}, {"column": "comment", "type": "varchar(117)", "comment": null}]},
  ...],
 "next_offset": null, "shortened": false}
```

## describe_table

`describe_table` reads one page of a table's columns with Trino's `DESCRIBE`, run as the caller,
and adds a governed sample when `sample_rows` asks for one. In the integration suite, without a
context provider, `describe_table` of `tpch.tiny.nation` answers alice:

```
{"columns": ["Column", "Type", "Extra", "Comment"], "rows": [["nationkey", "bigint", "", ""], ["name", "varchar(25)", "", ""], ["regionkey", "bigint", "", ""], ["comment", "varchar(152)", "", ""]], "truncated": false, "next_column_offset": null, "columns_cut": false}
```

With a context provider, the answer adds the keys [Context from a catalogue](catalog-context.md)
describes.

## search_columns

`search_columns` is where an agent starts when it does not know which table holds
what it needs. It takes one word or several from column or table names and comments,
never a value (a date, an amount, a code: values are in the rows, not in the names).
Whitespace and punctuation split the text into words, `_` belongs to a word
(`customer_id` is one word), and each word is looked for ignoring case, lower-cased
one code point at a time as Trino's `lower()` does. A column matches when every word
appears in its name, its comment, its table's name or its table's comment, and one
word at least in its own name or comment: so "customer segment" finds
`customers.segment`, and a word found only in a table's name does not list every
column of that table. `tables` lists, with their comment, the tables whose own name
or comment holds every word and the tables that hold a column listed: "archived
2019" finds a table `transactions_2019` commented "Archived transactions" without
its columns, which `describe_table` then reads. It searches every catalog the caller
sees (`system` never), one catalog, or one schema. A named schema is checked first:
one that does not exist and one the caller may not see answer the same error, `kind`
`schema`, never an empty search said to be complete; a catalog Trino does not know
or refuses answers Trino's error.

Each word is one escaped `LIKE` literal: a word
holds no quote and no `%`, and its `_` is a plain character. A word shorter than
`MCP_SEARCH_MIN_LENGTH` (3 by default) is ignored, and a text with no word left is
refused and says so, so a letter or two does not read every column. A text of more
than `MCP_SEARCH_MAX_WORDS` words (8 by default), or with a word longer than
`MCP_SEARCH_MAX_WORD_LENGTH` characters (64 by default), is refused before Trino is
asked anything, with the bound in the message: each word adds a few `LIKE` to every
statement. The search reads `system.metadata.table_comments` and
`system.jdbc.columns`, which Trino filters for each user, one schema at a time in a
stable order (catalogs, then their schemas, sorted; in a schema its tables, then its
columns). A schema costs two statements, its tables then its columns, which read
`system.metadata.table_comments` three times and `system.jdbc.columns` twice in all:
the tables' statement reads the comments for the tables' own text and again for the
columns a table holds, joined to their table's comment, and the columns' statement
reads both views once. On Hive the comment view is the expensive one, a metastore
call per table. Every read filters on the catalog and the schema by equality, which
Trino pushes down to the views, so a catalog of many thousand tables answers schema
by schema. It stays within the call's budget (`MCP_CALL_TIMEOUT_SECONDS`): before
each schema it keeps `MCP_SEARCH_RESERVE_SECONDS` (2 by default) in reserve, each
query is stopped in Trino at what is left, and the search then answers what it
searched rather than the call's own error. `tables` and `matches` together stop at
the row cap (`TRINO_MAX_ROWS`). The answer says exactly what was searched:
`schemas_searched`, `not_searched` with a reason for each part left (`time_budget`,
`row_cap`, or the `kind` of Trino's error; a `schema` of `null` is a whole catalog),
and `complete`, true only when everything in scope was searched, over every call of
the search. `next_cursor` resumes the same search where it stopped (that schema and
the rest of its catalog, then every catalog not opened), inside a schema's tables or
columns when the row cap stopped it there.

When a search of several words is
complete and nothing holds every word on any page of its chain of cursors, the same
call goes on with a per-word pass: each word alone, by the same rules, over the same
scope, within the same row cap, time budget and rights. Its rows carry `words`, the
words that found each one, the rows most words found first, and the answer carries
`partial: true`; one pass reads all the words, two statements per schema as the
first, so it is bounded as the search is. `complete`, `not_searched`,
`schemas_searched` and `next_cursor` then speak of that pass alone: when it cannot
finish, it says what it left and its cursor resumes it, never complete. A page of
the per-word pass adds `hint`, a fixed sentence for the agent: while the page has a
`next_cursor`, that the pass is not finished and to call again with it; on its last
page, that these are partial matches, to pick the right one or search again with one
word from a column or table name, then `describe_table`. A complete search that found
nothing at all, and the last page of a per-word pass that found nothing on any page,
add the other fixed `hint`: search
again with one word from a column or table name, never a value, or browse with
`list_tables` and `describe_schema`. An agent should not conclude from an empty
search that the data does not exist.

A schema, or the schema list of a catalog, that
takes longer than one query may (`TRINO_QUERY_TIMEOUT_SECONDS`) while the call still
has time is named (`time_budget`) and skipped: resuming it would stop every later
call at the same place. So is one that takes longer than a whole call, when Trino's
own limit is left in place (`TRINO_QUERY_TIMEOUT_SECONDS=0`) or is as long as the
call: a call that runs out of time exactly where it resumed, having moved nothing,
skips it, so every call advances. A cursor is refused (`invalid_argument`) for
another text or scope, for a catalog the caller does not see, or when it was
altered; every cursor the server gives resumes, a schema such as Hive's `2024_sales`
included, since a schema in a cursor only ever enters a string literal. Without a
call budget (`MCP_CALL_TIMEOUT_SECONDS=0`) a search of every catalog would have no
bound: it is refused, a named catalog is searched, and `--check` warns of it.

It reads `MCP_SEARCH_CONCURRENCY` catalogs at once (8 by default), each as the caller
and within what is left of the call, never more than that many past the catalog it is
answering from. When the call's budget is not exhausted, it answers what a search of one
catalog at a time does: the same rows in the same order, the same `not_searched`, and the
same `next_cursor`. When the budget runs out, the page is a correct partial page whose
content can differ, since it read further: nothing past the first catalog it did not
finish is served, and `next_cursor` resumes there. Once the page is full, the reads still
in flight are cancelled in Trino at once and none starts. A catalog that hangs at the head
is waited for as one catalog at a time would, up to the query's or the call's budget.
`MCP_SEARCH_MAX_THREADS` (32) bounds the reads in flight in the threads of the whole
process, all calls together: a call that finds none free reads its next catalog itself, so
it always advances. For Trino, each call of `search_columns` runs up to
`MCP_SEARCH_CONCURRENCY` metadata statements at once: bound them with a resource group on
the server's `TRINO_SOURCE`, as for `profile_table`'s client tag, or set
`MCP_SEARCH_CONCURRENCY=1` to read one catalog at a time, as 0.7 did.
`--check` also warns of `MCP_SEARCH_RESERVE_SECONDS=0` with a call budget: a search
may then run to the end of the call, which answers its own `time_budget` error. The
audit line says how many tables and columns came and whether the search was cut,
never the text, which may be a value. Comments are data, never instructions, as for
`describe_schema`.

In the [sandbox](https://github.com/AKKO-p/akko-sandbox), `search_columns` with the word `email`
answers carol (shortened):

```
{"tables": [{"catalog": "demo", "schema": "crm", "table": "customers", ...}],
 "matches": [{"catalog": "demo", "schema": "crm", "table": "customers", "column": "email", "type": "varchar", ...}],
 "schemas_searched": 4, "not_searched": [], "complete": true, "next_cursor": null}
```

## profile_table

The aggregate query of `profile_table` carries the client tag `MCP_PROFILE_CLIENT_TAG`
(`mcp-profile` by default), so Trino's resource groups can bound how many run at once and
the memory they take: [Operations](operations.md#scaling-and-resource-groups) gives an example.

The resource group bounds Trino; `MCP_PROFILE_MAX_CONCURRENT` (4) bounds each replica of
this server: a profile waiting in a Trino queue still holds one of the threads every tool of
the replica shares, so past that many profiles at once a call answers `"kind": "busy"` at once
and sends no query. Keep it at or below the group's `hardConcurrencyLimit` plus `maxQueued`,
divided by the replicas.

## execute_query and explain_query

Every identifier is validated (`[A-Za-z_][A-Za-z0-9_-]*`) before it is placed
in SQL. `execute_query` accepts any SQL Trino accepts, **as long as it is a
read**: [Security](security.md#the-statements-an-agent-may-send) lists the commands it lets
through and those it refuses.

`execute_query` is annotated `readOnlyHint: true` only while `TRINO_READ_ONLY`
is true. With `TRINO_READ_ONLY=false`, its annotations say it may write
(`readOnlyHint: false`, `destructiveHint: true`) and so does its description, so
that a host asks for the confirmation it would skip for a read-only tool. The
eight other tools only read, whatever the setting.

## Bounds

Results are capped at `TRINO_MAX_ROWS` (100 by default, 1 at least): one row
more is read, so `truncated` says whether the answer was cut, and the rest of
the query is cancelled in Trino. With
`TRINO_QUERY_TIMEOUT_SECONDS` (60 s by default), a query that runs longer is
cancelled in Trino and the agent is told so. The budget is
enforced by the server, not set as a session property: a policy engine that
forbids users to set session properties would refuse every query otherwise.
With `MCP_CALL_TIMEOUT_SECONDS` (120 s by default), a whole tool call has a
time budget, every query it runs included: past it no query starts, the one running is cancelled
(even when the budget ran out before Trino named it), and the agent gets
`{"error": "the tool call exceeded its time budget…"}`, never what the call had
gathered so far, nor an answer that came past the deadline. A plugin that calls
its own backend bounds that request with
`akko_mcp_trino.audit.time_allowed(timeout)`: its own timeout, or what is left
of the call's budget when that is shorter.

The defaults are chosen for an agent, not for a batch job. A question an agent
asks should come back within a minute; a query that needs longer is usually a
full scan the agent should narrow, and the error it gets says so. Two minutes
per call leave room for a tool that runs two or three queries, without letting
an agent's turn hang on one tool. Set either to `0` to remove the limit (the query
then runs as long as Trino's own `query.max-execution-time` allows); `--check`
prints both.

## Errors

Errors come back to the agent as `{"error": "...", "kind": "..."}`. `error` is
Trino's whole message; a permission refusal from Trino is an ordinary error, not a
crash. `kind` says what to do about it, and `identifier` names the column, table,
schema or catalog when Trino names the one it could not resolve. A column Trino
could not find (`COLUMN_NOT_FOUND`, named) in `execute_query` or `explain_query`
adds `hint`, which names the table, or each table, of the agent's own statement the
column was written against (`c.customer_id` names the table aliased `c`; a column
without a qualifier names every table read, unless the statement has a CTE or a
subquery) and advises `describe_table` on it. It names only the tables the caller
sees: each candidate, at most four, is looked up in its catalog's
`information_schema.tables` by equality, as the caller, each part exactly as written
(a quoted part may hold a dot), in what is left of the call minus two seconds, at
most five seconds, so a slow lookup never costs the agent its error; a name without
catalog is looked up in `TRINO_CATALOG`, and a name without schema (`FROM accounts`)
gets no advice, a known limit; a hidden table, a refusal, an error or less than two
seconds left means no advice for it; an ambiguous or unnamed column gets no hint,
and a statement the parser cannot read gets none either, never an error of its own:
the name comes from the statement, never from Trino, so it shows nothing the caller
could not already name:

| `kind` | Meaning |
|---|---|
| `syntax`, `column`, `table`, `schema`, `catalog`, `function`, `type` | Trino refused the statement; fix that part of it |
| `permission` | Trino or the identity provider refused the right; report it, do not retry |
| `invalid_argument` | the server refused an argument (an identifier, an `offset`, a cursor, a search text out of bounds) before reaching Trino |
| `read_only` | the statement writes and the server is read-only |
| `time_budget` | the call ran out of its time budget; narrow the request |
| `busy` | `profile_table` already runs as many calls as this server allows (`MCP_PROFILE_MAX_CONCURRENT`); call again in a few seconds, as its `hint` says |
| `unavailable` | Trino could not answer (internal error, connection lost): the statement is neither valid nor invalid, try again later |
| `other` | any other error |

The audit line keeps only the error's code (see [Audit](operations.md#audit)); it is unchanged.
