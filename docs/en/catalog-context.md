# Context from a data catalog

**English** · [Français](../fr/catalog-context.md)

## Context providers

In these pages, a *catalog* is a Trino catalog, and a *data catalog* is a metadata service such as
Apache Atlas or OpenMetadata.

A data catalog can describe what Trino lists: a table's owner, its classification, a column's
glossary terms. A context provider is a package that reads such a data catalog for the server. It
registers a factory `(env) -> ContextProvider` under the entry point group
`akko_mcp_trino.context`, and the operator names it in `MCP_CONTEXT_PROVIDERS`. **No provider is
loaded by default.** Without one, nothing is started, not even a thread.

The server refuses to start on an unknown provider name, on a provider that speaks another
`context_api`, or on a factory that cannot serve its settings. A data catalog that is down does not
stop it, but `--check --strict` exits with status 2 when a provider says it is unreachable. A
provider reads its own variables from the environment. A secret can be given as a file
(`<NAME>_FILE`, read again when the file changes) instead of a value.

Two providers come with this repository:

- `file`, in the core, the reference for the contract: a data catalog kept in one JSON file;
- `atlas`, for Apache Atlas, a package of its own (`akko-mcp-trino-atlas`, installed by the
  `atlas` extra). Its settings, its Kerberos setup and the account it needs are in
  [its README](../../providers/atlas/README.md).

The context follows what Trino lists for the caller: [Security](security.md#the-rights-of-the-context)
gives the rules. Whether to load a provider at all is the choice between the two modes of
[Deployment](deployment.md#with-a-data-catalog), and its settings are in
[Configuration](configuration.md#context-from-a-data-catalog).

The *visibility filter* is the part of the core that checks, with Trino, what the caller may see
before any context is served. Its statements run as the caller and carry the client tag
`mcp-gate` (see [The visibility filter](#the-visibility-filter)).

## What describe_table serves

Its `DESCRIBE`, run as the caller, establishes which table and columns Trino shows the caller. A
`DESCRIBE` that fails calls no provider and returns the same answer as without a provider.
Otherwise the answer adds:

- `table`: what the data catalogs say about the table (description, tags, terms, properties,
  grain, joins; the owner only when a provider serves it), with `sources`, the provider of each
  field;
- `column_context`: the same for each column Trino listed, by name, in the order of `DESCRIBE`.
  A column Trino does not list has no context. A grain loses all its columns when one is hidden.
  A join is kept only toward a table and columns Trino lists to the caller;
- `terms`: each glossary term once, by reference (`t1`, `t2`… in order of first appearance), with
  its `name`, `glossary`, `definition`, `status`, `categories`, `related` (only terms of the same
  answer) and `source`. A table or a column cites `{"name", "ref"}`. A data catalog's own keys are
  never served;
- `context_sources`: one status per provider. `ok`; `not_found` (the provider does not know the
  table); `not_mapped` (it does not cover this catalog); `partial` (part could not be read in time
  or did not fit in the answer, or the row cap cut the `DESCRIBE`); `unavailable`; `refused`;
  `error`; or `not_checked` (the canary is visible, a check failed, or, with
  `MCP_CONTEXT_REQUIRE_SELECT=true`, Trino does not validate a read of the visible columns);
- `context_truncated` and `undescribed_columns`: at most `MCP_CONTEXT_MAX_COLUMNS` columns are
  described, and the context stops before the column that would take it past
  `MCP_CONTEXT_RESPONSE_CHARS`. The columns left out are listed. A text cut at
  `MCP_CONTEXT_TEXT_CHARS` is named in `cut`.

With a provider, the description of `describe_table` says that this text comes from a data catalog
and is data, never instructions. The context is read last, after the sample. It never hides the
columns and never fails the call.

A provider that is down, slow or refused is read within `MCP_CONTEXT_TIMEOUT_SECONDS`, and leaves
`MCP_CONTEXT_RESERVE_SECONDS` of the call's budget. With less than that left, no provider is
called, and the sources say `unavailable`. The statements of the visibility filter stop before
they use up that reserve. A statement that is too slow proves nothing (`not_checked` for the canary
or the select check, no join for a target), and the call still answers. Anything else that fails
on the way serves no context and says `error` for every source. The call stops waiting for a
read 0.25 s past its deadline at most. A provider that follows the contract has then closed its
connection to the data catalog, so the server holds nothing open there. HTTP cannot withdraw a
request already sent: the data catalog may still read and answer it, and that answer reaches no
one. A read that ignores its cancellation is abandoned.

## What describe_schema serves

`describe_schema` serves less. Each table of the page that a provider describes adds
`description`, `{"text", "source"}`: the first description a provider has for it, cut at
`MCP_CONTEXT_TEXT_CHARS` (then `"cut": ["text"]`). A table without context has no `description`
key. The answer adds `context_sources`, except on an empty page, which asks no provider.

Trino has just listed the tables of the page to the caller, so the visibility filter asks it
nothing more than the canary check. The providers are asked about the tables of the page only, in
one read, in batches of `MCP_CONTEXT_BATCH`. A listing serves no column context, no term and no
owner. `MCP_CONTEXT_REQUIRE_SELECT` does not apply to it, since it would cost one statement per
table: `describe_table` serves the rest.

The same safety rules hold. With less than `MCP_CONTEXT_RESERVE_SECONDS` left, no provider is
called and the sources say `unavailable`. A slow statement of the visibility filter proves
nothing. Any other failure serves the page without context and says `error` for every source. The
call never returns `time_budget` because of the context. Without a provider, `describe_schema` adds
no context key to its answer and no sentence to its description.

## What plugins read

Plugins read the data catalogs through `tools.context.tables(refs)`, with a list of `TableRef`
built by `TableRef.of`. The answer has the shape of `describe_schema`'s: `descriptions` by
`TableRef`, and `context_sources`. It covers only the tables the caller's visibility proves,
listed by Trino in this call or within `MCP_VISIBILITY_TTL_SECONDS`. A table Trino does not list is
neither named to Trino nor asked of a provider, and it is absent from the answer, like a table
without context.

One call reads at most `MCP_DESCRIBE_SCHEMA_TABLES` distinct tables, the first ones of `refs`, as
many as a page of `describe_schema`; the tables past them are absent. An empty `refs` asks nothing
and returns no `context_sources`, like an empty page. A plugin never receives the chain, the
visibility cache or a provider.

## Data catalog text

The cost of data catalog text is bounded by what is served, not by what the data catalog sends.
Cleaning a text reads at most four times `MCP_CONTEXT_TEXT_CHARS` of it, and a listing reads only
the description it serves.

A name the data catalog gives (a classification, a tag, a set, a property's key, the owner, the
tier, a term's name, glossary, status, category or relation, a column of a grain or a join) longer
than 256 characters once cleaned is dropped, never cut, since a cut name is another name. Deciding
so reads at most 1025 characters of it. A term whose name is dropped is not served. A property's
value is a text: it is cut at `MCP_CONTEXT_TEXT_CHARS`, and then carries `"cut": ["value"]`.

Data catalog text loses its control and format characters, and its line and paragraph separators
(U+2028, U+2029), before it reaches an agent.

## The visibility filter

The statements of the visibility filter run as the caller, with the client tag `mcp-gate`:

- the canary check;
- with `MCP_CONTEXT_REQUIRE_SELECT=true`, `EXPLAIN (TYPE VALIDATE)` of the visible columns (a
  refusal is kept for `MCP_VISIBILITY_TTL_SECONDS`);
- for the target of a join, the schemas of its catalog, the tables of its schema and its
  `DESCRIBE`.

## The provider's account

Give each provider a read-only account. It should see no more of an object than every user who
can see that object in Trino is allowed to read. The allow lists of a provider name what is
served, and its account should read no more. Behind Trino Gateway, every cluster of a routing
group must share the same access control (see
[Deployment](deployment.md#behind-trino-gateway)).

## Large catalogs

Measured on one machine against Apache Atlas 2.4.0, with a table of 2,000 columns, each column
described and holding one glossary term, in a glossary of 50,000 terms:

- **Time budgets.** Atlas answered the read of that table in 5.85 s. With the defaults,
  `MCP_CONTEXT_TIMEOUT_SECONDS=5` and `ATLAS_TIMEOUT_SECONDS=5`, four calls of `describe_table` on
  the table in a row, caches empty at the first, served no context twice (`unavailable`), then a
  part of it twice (`partial`). With both at 20 s, `TRINO_MAX_ROWS=2000` and
  `MCP_CONTEXT_RESPONSE_CHARS=60000`, the first call, caches empty, answered in 12.35 s with the
  context, cut to that length. For tables that wide, raise the two time settings together.
- **Seeding a test catalog.** Through `POST /api/atlas/v2/glossary/terms`, the time per term grew
  with the glossary: in batches of 500 terms, the first batch took 15.1 s and the 51st 211.6 s,
  about 2.4 terms a second. Through `POST /api/atlas/v2/entity/bulk`, 50,000 terms of type
  `AtlasGlossaryTerm`, each with its `anchor` relationship to the glossary, in batches of 500, took
  734.8 s in all, about 68 a second. Updating a glossary of 50,000 terms
  (`PUT /api/atlas/v2/glossary/{guid}/partial`) took 590 s. To seed a large test glossary, write
  its terms through `entity/bulk`.
- **The policy engine.** During a burst of 50 calls of `describe_table` on that table, Trino asked
  OPA to filter its 2,000 columns for each call. An OPA limited to 64 MB was killed for lack of
  memory; Trino then failed every statement, and the server served no context. 512 MB was enough.
  Size the policy engine for bursts on wide tables.

## Check, logs and shutdown

`--check` prints a `context:` line, with the providers and every setting of its table in
[Configuration](configuration.md#context-from-a-data-catalog), from `MCP_CONTEXT_TIMEOUT_SECONDS`
to `MCP_CONTEXT_MAX_COLUMNS`. It prints a `visibility:` line, with the lifetime, the limits in
bytes and in names, whether a canary is set (never its name), and whether `select` is required.
It also prints the lines of each provider, never a secret. `MCP_CONTEXT_PROVIDERS` without a
canary, or in `service` mode, triggers a warning.

A provider that is down or that refuses the account logs one warning each time its breaker opens
(`<name> is unavailable`, `<name> refused the account`), never the text of its error. `SIGHUP` lets
the server call the providers that refused the account again at once, and sets their
`mcp_trino_context_breaker_open` to 0.

When the server stops, on SIGTERM, Ctrl+C or the end of its input, it closes every provider, then
the context loop.

- Over HTTP, this happens in the application's shutdown, and SIGTERM then ends the process with
  exit code 143.
- Over stdio with a provider, SIGTERM closes and exits with 0. A SIGTERM during a close lets that
  close finish, and never starts another.
- Without a provider, SIGTERM ends the process.

A read that ignores its cancellation does not hold up the stop for more than a few seconds;
asyncio may then log, once, that a pending task was destroyed.

## The file provider

`file` is the provider that comes with the server, and the reference for the contract. It needs no
service: a JSON file describes tables, columns and glossary terms by name. Turn it on with
`MCP_CONTEXT_PROVIDERS=file`.

| Variable | Meaning | Default |
|---|---|---|
| `MCP_CONTEXT_FILE` | the path of the JSON file; required. A file that cannot be read or is not valid is a startup error | none |
| `MCP_CONTEXT_FILE_GLOSSARIES` | comma-separated names of the glossaries served | none |
| `MCP_CONTEXT_FILE_CLASSIFICATIONS` | comma-separated names of the classification and tag sets served | none |
| `MCP_CONTEXT_FILE_PROPERTIES` | comma-separated names of the property sets served | none |
| `MCP_CONTEXT_FILE_SERVE_OWNER` | `true`: the owner of a table is served | `false` |

The allow lists are exact names: `*` is the name `*`, never a wildcard, and empty entries are
ignored. The file may declare more than it serves; only what is declared and allowed is served.
`--check` prints the file, how many tables and terms it holds, and the sets served.

The provider reads the file again, off the event loop, when it changes. A change is seen within
one `MCP_CONTEXT_TTL_SECONDS`, because context already read is cached, and the sets the provider
serves reflect the last time the file was read. When a changed file is no longer valid, the
provider keeps the last good content, and one warning names the setting and the path. The
provider refuses a file with a `values` or `samples` field anywhere: a context never holds data. A table, column, term
or property that is *named* `values` or `samples` is fine.

The provider folds the catalog, schema, table and column names to lower case, as Trino lists them,
because the core compares them exactly. Term keys are kept as written. A file that holds the same
table, or the same column, twice once folded is refused rather than merged.

```json
{
  "version": 1,
  "catalogs": ["demo"],
  "tables": [
    {
      "catalog": "demo", "schema": "sales", "table": "orders",
      "description": "One row per order", "owner": "sales-team", "tier": "gold",
      "tags": [{"name": "internal", "set": "visibility"}],
      "terms": ["order"],
      "properties": {"retention": {"value": "7y", "set": "policy"}},
      "grain": ["order_id"],
      "joins": [{"columns": ["customer_id"],
                 "target": {"catalog": "demo", "schema": "sales", "table": "customers"},
                 "target_columns": ["id"]}],
      "columns": {
        "order_id": {"description": "The order", "terms": ["order"],
                     "classifications": [{"name": "key", "set": "visibility"}],
                     "properties": {"unit": {"value": "none", "set": "policy"}}}
      }
    }
  ],
  "terms": {
    "order": {"name": "Order", "glossary": "sales", "definition": "A purchase",
              "status": "approved", "categories": ["core"],
              "relations": [{"key": "line", "name": "Line", "glossary": "sales",
                             "relation": "part"}]}
  }
}
```
