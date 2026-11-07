# The tools

**English** · [Français](../fr/tools.md)

## Overview

| Tool | Arguments | Returns |
|---|---|---|
| `search_columns` | `text`, `catalog`, `schema`, `cursor` (all but `text` optional) | the tables and columns that hold every word of the text; see [search_columns](#search_columns) |
| `list_catalogs` | `offset` (optional, 0 by default) | `{"catalogs": [...], "next_offset": n or null, "truncated": bool}`: one page of the catalogs Trino shows |
| `list_schemas` | `catalog`, `offset` (optional) | `{"schemas": [...], "next_offset": n or null, "truncated": bool}`: one page of the schemas in that catalog |
| `list_tables` | `catalog`, `schema`, `offset` (optional) | `{"tables": [...], "next_offset": n or null, "truncated": bool}`: one page of the tables and views in that schema |
| `describe_schema` | `catalog`, `schema`, `offset` (optional) | one page of tables, each complete, with their comments and columns; see [describe_schema](#describe_schema) |
| `describe_table` | `catalog`, `schema`, `table`, `sample_rows` (0 to 20, first page only), `column_offset` (optional, 0 by default) | one page of the columns, with types and comments, and a governed sample when asked; see [describe_table](#describe_table) |
| `profile_table` | `catalog`, `schema`, `table`, `columns`, `mode`, `percent` (all but the table optional) | aggregates of the rows the caller may read; see [profile_table](#profile_table) |
| `explain_query` | `sql` | `{"valid": true}`: Trino analyzed a read-only statement as the caller, without running it; `{"plan": "..."}` only when `EXPLAIN` is in `MCP_EXTRA_READ_COMMANDS` |
| `execute_query` | `sql`, `max_rows` (optional, lower than the server's cap) | `{"columns": [...], "rows": [...], "row_count": n, "truncated": bool}` |

Every tool runs in Trino through the identity mode, the listing tools included: metadata is data,
and Trino lists only what that identity may see. Every description is written for a model and
says what the tool returns. The descriptions of `describe_table`, `profile_table` and
`execute_query` also say that a masked value or a missing row is the access policy, not an error
to retry.

## The path an agent follows

Agents are told to call `search_columns` first, to find where the data lives. They then call
`describe_schema` (or `describe_table` for sample rows) to learn about the tables it found, and
finally `execute_query`. `list_catalogs`, `list_schemas` and `list_tables` are for browsing, when
the agent wants to see what there is rather than find something. The tool descriptions and
`examples/agent.py` say the same.

## Listings

The three listing tools return one page at a time. A page holds `TRINO_MAX_ROWS` names, sorted by
name, and `next_offset` says where the next page starts, or is `null` on the last page. The agent
calls again with `offset=next_offset` to read on. `truncated` is true exactly when `next_offset`
is not `null`.

Schemas and tables are paged in Trino (`ORDER BY` with `OFFSET` on
`information_schema.schemata` and `information_schema.tables`), so Trino decides what each page
holds. Catalogs are read with `SHOW CATALOGS`, which works even when the policy refuses all of
`system`. The server reads the full list, ignoring the row cap, up to `MCP_MAX_CATALOGS` (10000
by default), then pages it itself. `system` does not count toward this limit. Above the limit,
`list_catalogs` returns an error rather than a truncated list.

`list_tables` on a schema that does not exist, or that the caller may not see, returns an error of
kind `schema` ("not found or not visible", the same for both), never an empty list. The server
refuses an `offset` that is not a whole number from 0 (`invalid_argument`) before calling Trino.

## describe_schema

`describe_schema` gives an agent a schema's context in one call, before it writes SQL: its tables
and views, each with its type, its comment and all its columns (name, type, comment). It returns
`{"catalog", "schema", "tables": [{"table", "type", "comment", "columns": [{"column", "type",
"comment"}]}], "next_offset": n or null, "shortened": bool}`. With a context provider, it adds a
`description` per table and `context_sources` (see
[Context from a data catalog](catalog-context.md)).

It pages by tables, in Trino (`ORDER BY` with `OFFSET` on `information_schema.tables`). A page
holds up to `MCP_DESCRIBE_SCHEMA_TABLES` tables (20 by default, 100 at most), always whole, as long
as their columns add up to `MCP_DESCRIBE_SCHEMA_COLUMNS` at most (1000 by default). The first
table of a page always comes whole, however wide. A page that holds fewer tables because of the
column budget says so (`shortened: true`), and `next_offset` starts the next page at the table
left out.

The comments come from `system.metadata.table_comments` and the columns from
`system.jdbc.columns`. Trino filters these two views of its `system` catalog for each user. The
server reads them with statements it builds itself, never with the agent's SQL (see
[Security](security.md#read-only-through-this-server)).

A page costs four statements: the schema check, the page of table names, one read of the columns,
and the comments of the tables the page keeps. The columns are read once, at most
`MCP_DESCRIBE_SCHEMA_COLUMNS` plus one rows. The tables that fit whole within the budget are kept,
and the table the budget cuts starts the next page. A first table wider than the budget is read
again on its own, in parts of `MCP_DESCRIBE_SCHEMA_COLUMNS` columns, one statement per part.

The reads of the columns and of the comments hold one `UNION ALL` branch per table, each filtered
by equality on the table's name, which Trino pushes down into these views. A page therefore reads
the comments and columns of its own tables, never those of the whole schema. A list (`IN` or
`OR`) is not pushed down and would read every table of the schema. Trino loads the metadata of
every table of the page for that read, so a page costs about the same whether its tables are
narrow or wide.

A schema that does not exist and one the caller may not see return the same error, of kind
`schema`. A visible empty schema returns an empty list. Comments are text that the owners of the
data wrote: the tool's description tells the model to read them as data, never as instructions,
and the server never puts them in its own MCP instructions.

In the integration suite, `describe_schema` on `tpch.tiny` returns, for Alice, the eight tables of
the schema on one page. Here is the first table, and the end of the answer:

```
{"catalog": "tpch", "schema": "tiny", "tables": [
  {"table": "customer", "type": "BASE TABLE", "comment": null, "columns": [{"column": "custkey", "type": "bigint", "comment": null}, {"column": "name", "type": "varchar(25)", "comment": null}, {"column": "address", "type": "varchar(40)", "comment": null}, {"column": "nationkey", "type": "bigint", "comment": null}, {"column": "phone", "type": "varchar(15)", "comment": null}, {"column": "acctbal", "type": "double", "comment": null}, {"column": "mktsegment", "type": "varchar(10)", "comment": null}, {"column": "comment", "type": "varchar(117)", "comment": null}]},
  ...],
 "next_offset": null, "shortened": false}
```

## describe_table

`describe_table` reads one page of a table's columns with Trino's `DESCRIBE`, run as the caller.
A page holds up to `TRINO_MAX_ROWS` columns, starting at `column_offset`, with their types and
comments.

- `truncated` is true when the page does not hold every column, and `next_column_offset` says
  where the next page starts (`null` on the last page).
- `columns_cut` is true when the table has more than `MCP_DESCRIBE_MAX_COLUMNS` columns.
- `sample_rows` adds a governed sample, on the first page only.
- A tool plugin's describers add their own keys, and `describer_errors` counts those that failed.
- With a context provider, the answer adds `table`, `column_context`, `terms`,
  `context_sources`, `context_truncated` and `undescribed_columns` (see
  [Context from a data catalog](catalog-context.md)).

`describe_table` remains the tool for sample rows and for what extensions add. In the
integration suite, without a context provider, `describe_table` on `tpch.tiny.nation` returns,
for Alice:

```
{"columns": ["Column", "Type", "Extra", "Comment"], "rows": [["nationkey", "bigint", "", ""], ["name", "varchar(25)", "", ""], ["regionkey", "bigint", "", ""], ["comment", "varchar(152)", "", ""]], "truncated": false, "next_column_offset": null, "columns_cut": false}
```

## search_columns

`search_columns` is where an agent starts when it does not know which table holds what it needs.
It returns:

```
{"tables": [{"catalog", "schema", "table", "comment"}],
 "matches": [{"catalog", "schema", "table", "column", "type", "comment"}],
 "schemas_searched": n, "not_searched": [{"catalog", "schema", "reason"}],
 "complete": bool, "next_cursor": token or null}
```

### Matching

The text is one or more words from column or table names and comments, never a value. Dates,
amounts and codes are in the rows, not in the names. Whitespace and punctuation split the text
into words, and `_` belongs to a word (`customer_id` is one word). Each word is matched
case-insensitively, lower-cased one code point at a time as Trino's `lower()` does.

A column matches when every word appears in its name, its comment, its table's name or its
table's comment, and at least one word appears in its own name or comment. So "customer segment"
finds `customers.segment`, and a word found only in a table's name does not list every column of
that table. `tables` lists, with their comment, the tables whose own name or comment holds every
word, and the tables that hold a listed column. For example, "archived 2019" finds a table
`transactions_2019` with the comment "Archived transactions", without its columns, which
`describe_table` then reads.

The search covers every catalog the caller sees (never `system`), one catalog, or one schema. A
named schema is checked first: a schema that does not exist and one the caller may not see return
the same error, of kind `schema`, never an empty search reported as complete. A catalog that Trino
does not know or refuses returns Trino's error.

### Input limits

Each word becomes one escaped `LIKE` literal: a word holds no quote and no `%`, and its `_` is a
plain character. A word shorter than `MCP_SEARCH_MIN_LENGTH` (3 by default) is ignored. A text
with no word left is refused with a message that says so, so a letter or two never reads every
column. A text of more than `MCP_SEARCH_MAX_WORDS` words (8 by default), or with a word longer
than `MCP_SEARCH_MAX_WORD_LENGTH` characters (64 by default), is refused before Trino is called,
and the message gives the limit. Each word adds a few `LIKE` predicates to every statement.

### Cost in Trino

The search reads `system.metadata.table_comments` and `system.jdbc.columns`, which Trino filters
for each user. It reads one schema at a time, in a stable order: catalogs, then their schemas,
sorted; within a schema, its tables, then its columns. A schema costs two statements, one for its
tables and one for its columns. Together they read `system.metadata.table_comments` three times
and `system.jdbc.columns` twice. The tables' statement reads the comments for the tables' own
text, and again for the columns a table holds, joined to their table's comment. The columns'
statement reads both views once. On Hive, the comment view is the expensive one, with one
metastore call per table. Every read filters on the catalog and the schema by equality, which
Trino pushes down into the views, so a catalog with many thousands of tables is searched one
schema at a time.

The search reads up to `MCP_SEARCH_CONCURRENCY` catalogs at once (8 by default), each as the
caller and within what is left of the call. It never reads more than that many catalogs ahead of
the one whose results it is returning. When the call's budget is not exhausted, the answer is the
same as a search of one catalog at a time: the same rows in the same order, the same
`not_searched` and the same `next_cursor`. When the budget runs out, the page is a correct partial
page whose content can differ, since the search read further ahead. Nothing past the first
catalog it did not finish is returned, and `next_cursor` resumes there. Once the page is full, the
reads still in flight are cancelled in Trino at once, and no new read starts. A catalog that hangs
at the head is waited for, as a search of one catalog at a time would, up to the query's or the
call's budget. `MCP_SEARCH_MAX_THREADS` (32) limits the reads in flight in the threads of the
whole process, all calls together. A call that finds no free thread reads its next catalog itself,
so it always makes progress. [Operations](operations.md#scaling-and-resource-groups) shows how to
limit these statements in Trino.

### Time budget

The search stays within the call's budget (`MCP_CALL_TIMEOUT_SECONDS`). Before each schema, it
keeps `MCP_SEARCH_RESERVE_SECONDS` (2 by default) in reserve, and each query is stopped in Trino
at the time left. The search then returns what it has searched so far, instead of failing with a
time-budget error. `tables` and `matches` together stop at the row cap (`TRINO_MAX_ROWS`).

A schema, or the schema list of a catalog, that takes longer than one query is allowed
(`TRINO_QUERY_TIMEOUT_SECONDS`) while the call still has time is reported (`time_budget`) and
skipped: resuming it would stop every later call at the same place. The same holds for a schema
that takes longer than a whole call, when Trino's own limit is left in place
(`TRINO_QUERY_TIMEOUT_SECONDS=0`) or is as long as the call. A call that runs out of time exactly
where it resumed, without making progress, skips that schema, so every call moves forward.

Without a call budget (`MCP_CALL_TIMEOUT_SECONDS=0`), a search of every catalog would have no
limit: it is refused, a named catalog is still searched, and `--check` warns about it. `--check`
also warns about `MCP_SEARCH_RESERVE_SECONDS=0` with a call budget: a search may then run to the
end of the call, which returns its own `time_budget` error.

### What the answer says

The answer says exactly what was searched:

- `schemas_searched`;
- `not_searched`, with a reason for each part left out (`time_budget`, `row_cap`, or the `kind` of
  Trino's error); a `schema` of `null` means a whole catalog;
- `complete`, true only when everything in scope was searched, over every call of the search.

`next_cursor` resumes the same search where it stopped: that schema and the rest of its catalog,
then every catalog not yet opened. It resumes inside a schema's tables or columns when the row
cap stopped the search there. The server refuses a cursor (`invalid_argument`) for another text
or scope, for a catalog the caller does not see, or when it was altered. Every cursor the server
gives resumes, including one for a Hive schema such as `2024_sales`, since a schema in a cursor
only ever enters a string literal.

### The per-word pass

When a search of several words is complete and no page of its chain of cursors holds every word,
the same call goes on with a per-word pass. Each word is searched alone, by the same rules, over
the same scope, within the same row cap, time budget and rights. Its rows carry `words`, the words
that found each one, and the rows that most words found come first. The answer carries
`partial: true`. One pass reads all the words, with two statements per schema as before, so it is
limited as the search is. `complete`, `not_searched`, `schemas_searched` and `next_cursor` then
refer to that pass only. When it cannot finish, it says what it left out and its cursor resumes
it; it is never reported as complete.

A page of the per-word pass adds `hint`, a fixed sentence for the agent. While the page has a
`next_cursor`, the hint says the pass is not finished and to call again with it. On the last
page, it says these are partial matches, and to pick the right one or search again with one word
from a column or table name, then call `describe_table`. A complete search that found nothing at
all, and the last page of a per-word pass that found nothing on any page, add the other fixed
hint: search again with one word from a column or table name, never a value, or browse with
`list_tables` and `describe_schema`. An agent should not conclude from an empty search that the
data does not exist.

The audit line says how many tables and columns came back and whether the search was cut, never
the text, which may be a value. Comments are data, never instructions, as for `describe_schema`.

In the [sandbox](https://github.com/AKKO-p/akko-sandbox), `search_columns` with the word `email`
returns, for Carol (shortened):

```
{"tables": [{"catalog": "demo", "schema": "crm", "table": "customers", ...}],
 "matches": [{"catalog": "demo", "schema": "crm", "table": "customers", "column": "email", "type": "varchar", ...}],
 "schemas_searched": 4, "not_searched": [], "complete": true, "next_cursor": null}
```

## profile_table

`profile_table` returns `{"table", "scope", "rows": n, "columns_cut": bool, "columns":
[{"column", "type", "non_null", "null_fraction", "approx_distinct", "min", "max"}]}`. These are
aggregates of the rows the caller may read, computed by one query run as the caller, so column
masks and row filters apply as in `execute_query`.

- **Columns.** `columns` are named as `DESCRIBE` lists them; by default, the first
  `MCP_PROFILE_MAX_COLUMNS`. `DESCRIBE` is read up to `MCP_PROFILE_DESCRIBE_ROWS` rows, and
  `"columns_cut": true` says it listed more columns, which cannot be named.
- **Modes.** `mode` is one of `MCP_PROFILE_MODES`. `sample`, the default, reads the first
  `MCP_PROFILE_SAMPLE_ROWS` rows (`"scope": "first_rows"`, not a random sample). `bernoulli` uses
  `TABLESAMPLE BERNOULLI (percent)`, with `0 < percent <= 100` (`"scope": "random_sample"`), and
  scans every row to draw the sample. `full` reads every row the caller may read
  (`"scope": "visible_rows"`). A mode the server does not allow is refused before any query, and
  the description names only the allowed modes.
- **Time budget.** The aggregate query carries the client tag `MCP_PROFILE_CLIENT_TAG`. It runs
  within the smallest non-zero value among `MCP_PROFILE_TIMEOUT_SECONDS`,
  `TRINO_QUERY_TIMEOUT_SECONDS` and the call's budget. Past it, Trino cancels the query and the
  answer is `"kind": "time_budget"`.
- **Concurrency.** At most `MCP_PROFILE_MAX_CONCURRENT` calls run at once in a process. Past that,
  a call immediately returns `"kind": "busy"`. [Operations](operations.md#scaling-and-resource-groups)
  shows how to size it.
- **Types without an order.** `array`, `map`, `row`, `json` and any type not known to be ordered
  get their count only, with `"skipped": ["distinct", "min", "max"]`. `null_fraction` is `null`
  when `rows` is 0.
- **Cut values.** `min` and `max` are rendered like the cells of `execute_query`. A text or binary
  value longer than `MCP_PROFILE_VALUE_CHARS` (characters, or bytes) is cut in the query itself,
  to an exact prefix of the true value, with `"cut": ["min"]`, `["max"]` or both. A type longer
  than `MCP_PROFILE_TYPE_CHARS` is cut, with `"type"` in `cut`.

## execute_query and explain_query

`execute_query` accepts any SQL that Trino accepts, as long as it is a read. Identifiers are
validated first. [Security](security.md#the-statements-an-agent-may-send) lists the statements the
server lets through and those it refuses.

`execute_query` is annotated `readOnlyHint: true` only while `TRINO_READ_ONLY` is true. With
`TRINO_READ_ONLY=false`, its annotations say it may write (`readOnlyHint: false`,
`destructiveHint: true`), and so does its description, so that a host asks for the confirmation it
would skip for a read-only tool. The eight other tools only read, whatever the setting.

## Bounds

Results are capped at `TRINO_MAX_ROWS` (100 by default, at least 1). The server reads one more
row, so `truncated` says whether the answer was cut, and the rest of the query is cancelled in
Trino.

With `TRINO_QUERY_TIMEOUT_SECONDS` (60 s by default), the server cancels a longer query in Trino
and tells the agent. The server enforces this budget itself rather than setting a session
property: a policy engine that forbids users to set session properties would otherwise refuse
every query.

With `MCP_CALL_TIMEOUT_SECONDS` (120 s by default), a whole tool call has a time budget, every
query it runs included. Past it, no query starts, and the running query is cancelled, even when
the budget ran out before Trino named it. The agent gets
`{"error": "the tool call exceeded its time budget…"}`, never what the call had gathered so far,
and never an answer that arrived after the deadline. A plugin that calls its own backend limits
that request with `akko_mcp_trino.audit.time_allowed(timeout)`, which returns its own timeout, or
what is left of the call's budget when that is shorter.

The defaults are chosen for an agent, not for a batch job. A question an agent asks should come
back within a minute. A query that needs longer is usually a full scan that the agent should
narrow, and the error it gets says so. Two minutes per call leave room for a tool that runs two or
three queries, without letting an agent's turn hang on one tool. Set either value to `0` to
remove the limit; the query then runs as long as Trino's own `query.max-execution-time` allows.
`--check` prints both.

## Errors

Errors come back to the agent as `{"error": "...", "kind": "..."}`. `error` is Trino's whole
message. A permission refusal from Trino is an ordinary error, not a crash. `kind` says what to do
about it, and `identifier` names the column, table, schema or catalog when Trino names the one it
could not resolve.

| `kind` | Meaning |
|---|---|
| `syntax`, `column`, `table`, `schema`, `catalog`, `function`, `type` | Trino refused the statement; fix that part of it |
| `permission` | Trino or the identity provider refused the right; report it, do not retry |
| `invalid_argument` | the server refused an argument (an identifier, an `offset`, a cursor, a search text out of bounds) before calling Trino |
| `read_only` | the statement writes and the server is read-only |
| `time_budget` | the call ran out of its time budget; narrow the request |
| `busy` | `profile_table` already runs as many calls as this server allows (`MCP_PROFILE_MAX_CONCURRENT`); call again in a few seconds, as its `hint` says |
| `unavailable` | Trino could not answer (internal error, lost connection); this says nothing about whether the statement is valid, so try again later |
| `other` | any other error |

When Trino cannot find a column (`COLUMN_NOT_FOUND`, with its name) in `execute_query` or
`explain_query`, the error adds a `hint`. The hint names the table the column was written against
and suggests calling `describe_table` on it.

- A qualified column (`c.customer_id`) names the table aliased `c`.
- An unqualified column names every table the statement reads, unless the statement has a CTE or
  a subquery.
- The hint names only tables the caller can see. Each candidate (at most four) is looked up in
  its catalog's `information_schema.tables` by equality, as the caller, each part exactly as
  written (a quoted part may hold a dot).
- The lookup gets the time left in the call minus two seconds, and at most five seconds. A slow
  lookup never costs the agent its error.
- A table name without a catalog is looked up in `TRINO_CATALOG`. A name without a schema
  (`FROM accounts`) gets no hint. This is a known limit.
- No hint is given for a hidden table, a refused or failed lookup, less than two seconds left, an
  ambiguous or unnamed column, or a statement the parser cannot read. A missing hint never adds an
  error of its own.

The table name comes from the agent's own statement, never from Trino, so the hint shows nothing
the caller could not already name. The audit line keeps only the error's code (see
[Audit](operations.md#audit)).
