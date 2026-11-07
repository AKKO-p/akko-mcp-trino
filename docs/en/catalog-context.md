# Context from a catalogue

**English** · [Français](../fr/catalog-context.md)

## Context providers

A data catalogue can describe what Trino lists: a table's owner, its classification, a column's glossary
terms. A context provider is a package that reads such a catalogue for the server. It registers a
factory `(env) -> ContextProvider` under the entry-point group `akko_mcp_trino.context`, and the
operator names it in `MCP_CONTEXT_PROVIDERS`. **No provider is loaded by default**: without one,
nothing is started, not even a thread. An unknown name, a provider that speaks another `context_api`,
or a factory that cannot serve its settings refuses to start; a catalogue that is down does not
(`--check --strict` exits 2 when a provider says it is unreachable). A provider reads its own
variables from the environment; a secret can be given as a file (`<NAME>_FILE`, read again when the
file changes) instead of a value.

Two providers come with this repository: `file`, in the core, the reference for the contract (a
catalogue kept in one JSON file), and `atlas`, for Apache Atlas, a package of its own
(`akko-mcp-trino-atlas`, installed by the `atlas` extra); its settings, its Kerberos set-up and
the account it needs are in [its README](../../providers/atlas/README.md).

Context follows what Trino lists for the caller: a name that comes from a catalogue is never used
before Trino has listed it for that caller, a provider never receives the Trino client or the
caller's identity, and it never writes to its catalogue. The text of a provider's error never
reaches the agent, only a status.

Whether to load one at all is the choice between the two modes of
[Deployment](deployment.md#deployment-with-a-catalogue), and its settings are in
[Configuration](configuration.md#context-from-a-catalogue).

## What describe_table serves

`describe_table` serves this context. Its `DESCRIBE`, run as the caller, proves the table and
the columns Trino lists; a `DESCRIBE` that fails calls no provider and answers as before. The answer
then adds:

- `table`: what the catalogues say of the table (description, tags, terms, properties, grain,
  joins; the owner only when a provider serves it), with `sources`, the provider of each field;
- `column_context`: the same for each column Trino listed, by name, in the order of `DESCRIBE`. A
  column Trino does not list has no context; a grain loses all its columns when one is hidden; a
  join is kept only toward a table and columns Trino lists to the caller;
- `terms`: each glossary term once, by reference (`t1`, `t2`… in order of first appearance), with
  its `name`, `glossary`, `definition`, `status`, `categories`, `related` (only terms of the same
  answer) and `source`. A table or a column cites `{"name", "ref"}`; a catalogue's own keys are
  never served;
- `context_sources`: one status per provider, `ok`, `not_found` (the provider does not know the
  table), `not_mapped` (it does not cover this catalog), `partial` (part was not read in time or
  in the size of the answer, or the row cap cut the `DESCRIBE`), `unavailable`, `refused`, `error`,
  or `not_checked` (the canary is visible, a check failed, or with `MCP_CONTEXT_REQUIRE_SELECT=true`
  Trino does not validate a read of the visible columns);
- `context_truncated` and `undescribed_columns`: at most `MCP_CONTEXT_MAX_COLUMNS` columns are
  described, and the context stops before the column that would take it past
  `MCP_CONTEXT_RESPONSE_CHARS`; the columns left are listed. A text cut at `MCP_CONTEXT_TEXT_CHARS`
  is named in `cut`.

Without a provider, `describe_table` answers as in 0.7.2 plus `next_column_offset` and
`columns_cut`, with a new description that says how to read the next page, and
`describer_errors`: a describer of a plugin that raises is now counted there. With a provider, its
description says that this text comes from a catalogue and is data, never instructions. The context
is read last, after the sample, and never hides the columns nor fails the call. A provider that is
down, slow or refused is read within `MCP_CONTEXT_TIMEOUT_SECONDS` and leaves
`MCP_CONTEXT_RESERVE_SECONDS` of the call's budget; with less than that left, no provider is called
and the sources say `unavailable`. The statements of the filter stop while that reserve is still
left: one that is too slow proves nothing (`not_checked` for the canary or the select check, no
join for a target), and the call still answers. Anything else that fails on the way serves no
context and says `error` for every source.

## What describe_schema serves

`describe_schema` serves less. Each table of the page that a provider describes adds `description`,
`{"text", "source"}`: the first description a provider has for it, cut at `MCP_CONTEXT_TEXT_CHARS`
(then `"cut": ["text"]`). A table without context has no `description` key, and the answer adds
`context_sources`, except on an empty page, which asks no provider. Trino has just listed the tables
of the page to the caller, so the filter asks it nothing more than the canary check; the providers
are asked for the tables of the page only, in one read by batches of `MCP_CONTEXT_BATCH`. A listing
serves no column context, no term and no owner, and `MCP_CONTEXT_REQUIRE_SELECT` does not apply to
it, as it would cost one statement per table: `describe_table` serves the rest. The same safety
holds: with less than `MCP_CONTEXT_RESERVE_SECONDS` left no provider is called and the sources say
`unavailable`, a slow statement of the filter proves nothing, any other failure serves the page
without context and says `error` for every source, and the call never answers `time_budget` because
of the context. Without a provider, `describe_schema` adds no key of the context to its answer
and no sentence to its description.

## What plugins read

Plugins read the catalogues through `tools.context.tables(refs)`, a list of `TableRef` built with
`TableRef.of`. It answers as `describe_schema` does, `descriptions` by `TableRef` and
`context_sources`, for the tables the caller's visibility proves only (listed by Trino in this call
or within `MCP_VISIBILITY_TTL_SECONDS`). A table Trino does not list is neither named to Trino nor
asked of a provider, and is absent from the answer, as a table without context is. One call reads at
most `MCP_DESCRIBE_SCHEMA_TABLES` distinct tables, the first of `refs`, as many as a page of
`describe_schema`; the tables past them are absent. An empty `refs` asks nothing and answers no
`context_sources`, as an empty page does. A plugin never receives the chain, the visibility cache
nor a provider.

## Catalogue text

A catalogue text costs what is served, not what the catalogue sends: cleaning a text reads at most
four times `MCP_CONTEXT_TEXT_CHARS` of it, and a listing reads only the description it serves. A
name the catalogue gives (a classification, a tag, a set, a property's key, the owner, the tier, a
term's name, glossary, status, category or relation, a column of a grain or a join) longer than 256
characters once cleaned is dropped, never cut, since a cut name is another name; deciding so reads
at most 1025 characters of it. A term whose name is dropped is not served. A property's value is a
text: it is cut at `MCP_CONTEXT_TEXT_CHARS`, and then carries `"cut": ["value"]`.

## The statements of the filter

The statements of the filter run as the caller, with the client tag `mcp-gate`: the canary check,
`EXPLAIN (TYPE VALIDATE)` of the visible columns with `MCP_CONTEXT_REQUIRE_SELECT=true` (a refusal
is kept for `MCP_VISIBILITY_TTL_SECONDS`), and, for the target of a join, the schemas of its
catalog, the tables of its schema and its `DESCRIBE`. A right withdrawn in Trino can leave the
context of a table visible for up to `MCP_VISIBILITY_TTL_SECONDS`, never its data, which Trino
refuses at once.

## The provider's account

Give each provider a read-only account that sees only what every user who sees an object in Trino
may read of it: the allow lists of a provider name what is served, and its account should read no
more. Behind Trino Gateway, the statements of the filter and those of the call may reach two
clusters of one routing group: every cluster of a group must share the same access control (the
same plugin and the same rules). A routing group whose clusters apply different rules is not
supported.

## Check, logs and shutdown

`--check` prints a `context:` line (the providers and every setting of its table in
[Configuration](configuration.md#context-from-a-catalogue), from
`MCP_CONTEXT_TIMEOUT_SECONDS` to `MCP_CONTEXT_MAX_COLUMNS`), a `visibility:` line (the lifetime, the
bound in bytes and in names, whether a canary is set, never its name, and whether `select` is
required) and the lines of each provider, never a secret. `MCP_CONTEXT_PROVIDERS` without a canary,
or in `service` mode, is warned of.

A provider that is down or that refuses the account logs one warning each time its breaker opens
(`<name> is unavailable`, `<name> refused the account`), never the text of its error. `SIGHUP` lets
the providers that refused the account be called again at once and sets their
`mcp_trino_context_breaker_open` to 0. When the server stops, on SIGTERM, Ctrl+C or the end of its
input, it closes every provider, then the context loop. Over HTTP this happens in the application's
shutdown, and SIGTERM then ends the process with exit code 143; over stdio with a provider, SIGTERM
closes and exits 0, and a SIGTERM during a close lets that close end, never starting another;
without a provider, SIGTERM ends the process as in 0.7.2. A read that ignores its cancellation does
not hold the stop for more than a few seconds; asyncio may then log once that a pending task was
destroyed. Catalogue text loses its control and format characters, and its line and paragraph
separators (U+2028, U+2029), before it reaches an agent.

## The file provider

`file` is the provider that comes with the server, and the reference for the contract. It needs no
service: a JSON file describes tables, columns and glossary terms by name. Turn it on with
`MCP_CONTEXT_PROVIDERS=file`.

| Variable | Meaning | Default |
|---|---|---|
| `MCP_CONTEXT_FILE` | the path of the JSON file; required, and a file that cannot be read or is not valid refuses to start | none |
| `MCP_CONTEXT_FILE_GLOSSARIES` | comma-separated names of the glossaries served | none |
| `MCP_CONTEXT_FILE_CLASSIFICATIONS` | comma-separated names of the classification and tag sets served | none |
| `MCP_CONTEXT_FILE_PROPERTIES` | comma-separated names of the property sets served | none |
| `MCP_CONTEXT_FILE_SERVE_OWNER` | `true`: the owner of a table is served | `false` |

The allow lists are exact names: `*` is the name `*`, never a wildcard, and empty entries are
ignored. The file may declare more than it serves; only what is declared and allowed is served.
`--check` prints the file, how many tables and terms it holds, and the sets served.

The file is read again, off the event loop, when it changes: a change is seen within one
`MCP_CONTEXT_TTL_SECONDS`, because context already read is cached, and the sets the provider serves
reflect the last time the file was read. A changed file that is no longer valid
keeps the last good content, and one warning names the setting and the path. A `values` or
`samples` field anywhere in the file refuses it: a context never holds data. A table, column, term
or property that is *named* `values` or `samples` is fine.

The provider folds the catalog, schema, table and column names to lower case, as Trino lists them,
because the core compares them exactly; term keys are kept as written. A file that holds the same
table, or the same column, twice once folded is refused instead of merged.

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
