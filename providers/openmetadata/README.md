# akko-mcp-trino-openmetadata

The [OpenMetadata](https://open-metadata.org) context provider for
[akko-mcp-trino](https://github.com/AKKO-p/akko-mcp-trino). It hands the agent
what the catalogue knows about a table — description, owners, tier, tags,
primary key as grain, foreign keys as joins, column descriptions and
classifications such as `PII.Sensitive` — alongside the columns Trino gives.

It reads metadata only, with a bot token. The data itself still flows through
Trino under the user's identity; knowing a column is PII changes nothing about
the mask the engine applies.

```bash
pip install akko-mcp-trino-openmetadata
export MCP_CONTEXT_PROVIDERS=openmetadata,trino-comments
export OPENMETADATA_URL=http://openmetadata:8585
export OPENMETADATA_TOKEN=<a bot token with read access>
export OPENMETADATA_SERVICE=<the database service name of your Trino in OpenMetadata>
akko-mcp-trino
```

| Variable | Meaning | Default |
|---|---|---|
| `OPENMETADATA_URL` | the OpenMetadata server | — |
| `OPENMETADATA_TOKEN` | a bot token; it needs read access to tables | — |
| `OPENMETADATA_SERVICE` | the name of the database service that catalogues your Trino (table FQNs are `service.catalog.schema.table`) | — |
| `OPENMETADATA_TIMEOUT_SECONDS` | per-request timeout | `5` |

An unknown table, a refused token or a catalogue that is down all mean
"nothing known": the agent gets Trino alone, never an error. One table fetch
serves all its columns for thirty seconds.

Apache 2.0. Part of the akko-mcp-trino repository (`providers/openmetadata`).
