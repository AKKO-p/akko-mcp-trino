# Quickstart

**English** · [Français](../fr/quickstart.md)

## Start the sandbox

The [sandbox](https://github.com/AKKO-p/akko-sandbox) runs Keycloak, Trino, OPA and
akko-mcp-trino on your machine, with three demo users. You need Docker with Compose v2,
about 2.5 GB of free memory, and the ports `8080`, `3000` and `6274` free.

```bash
git clone https://github.com/AKKO-p/akko-sandbox.git
cd akko-sandbox
docker compose up -d --wait
./demo.sh
```

`demo.sh` sends the same question for two users through the official MCP client, shows that
Trino runs each query as the user whose token it received, and shows the refusals: a third
user refused by the policy, a write refused by the server before Trino, and requests without a
valid token refused with `401`.

| User | Sees |
|---|---|
| `alice_admin` | every row, every column in clear |
| `carol_analyst` | European customers only, e-mail addresses masked |
| `dave_viewer` | everything except the customers table and the accounts' `balance` column |

## Get a user's token

```bash
./token.sh carol_analyst
```

`token.sh` asks Keycloak for `carol_analyst`'s access token, issued for the MCP server, and
prints it. It is valid for one hour. Paste it in place of `<the user's access token>`.

## Connect a host

**Cursor**, in `~/.cursor/mcp.json` ([`examples/cursor.json`](../../examples/cursor.json)):

```json
{
  "mcpServers": {
    "trino": {
      "url": "http://localhost:3000/mcp",
      "headers": {
        "Authorization": "Bearer <the user's access token>"
      }
    }
  }
}
```

Claude Desktop reaches an HTTP server through a local bridge:
[Connect an MCP host](hosts.md#1-try-it-in-five-minutes-the-sandbox) gives its entry, and the
other hosts.

## Ask a question

*"Which column holds the customers' e-mail address?"* The model calls `search_columns` with a
word such as `email`, and the server answers, for carol:

```
{"tables": [{"catalog": "demo", "schema": "crm", "table": "customers", ...}],
 "matches": [{"catalog": "demo", "schema": "crm", "table": "customers", "column": "email", "type": "varchar", ...}],
 "schemas_searched": 4, "not_searched": [], "complete": true, "next_cursor": null}
```

*"List our customers with their e-mail and country."* With carol's token, five European
customers come back, their addresses masked (`***@example.com`). With `./token.sh alice_admin`,
all ten, in clear. With `./token.sh dave_viewer`, the search finds nothing and says it is
complete: the policy hides the table from him, so it is not his to see.

To switch user, paste another token and restart the server in the host.

## Check it without a host

[`examples/check_host_config.py`](../../examples/check_host_config.py) reads the same file the
host reads, reaches the server the way the host would, lists the tools and calls
`search_columns`. Run it from a clone of this repository:

```bash
git clone https://github.com/AKKO-p/akko-mcp-trino.git
cd akko-mcp-trino
python3 -m venv .venv && . .venv/bin/activate
pip install akko-mcp-trino==0.7.2
python3 examples/check_host_config.py ~/.cursor/mcp.json --search email
```

```
entry: trino (streamable HTTP)
server: trino-mcp 0.7.2
8 tools: search_columns, list_catalogs, list_schemas, list_tables, describe_schema, describe_table, explain_query, execute_query
search_columns('email'):
{"tables": [...], "matches": [...], "complete": true, "next_cursor": null}
```

This is the output of 0.7.2, which served 8 tools; from 0.8 the list adds `profile_table`.

## Stop

```bash
docker compose down -v
```

Then read [Deployment](deployment.md) to run the server against your own Trino, and
[Identity](identity.md) to choose how it reaches Trino.
