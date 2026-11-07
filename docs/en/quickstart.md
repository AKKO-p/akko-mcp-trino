# Quickstart

**English** · [Français](../fr/quickstart.md)

## Start the sandbox

The [sandbox](https://github.com/AKKO-p/akko-sandbox) runs Keycloak, Trino, OPA and
akko-mcp-trino on your machine, with three demo users. You need Docker with Compose v2, about
2.5 GB of free memory, and ports 8080, 3000 and 6274 available.

```bash
git clone https://github.com/AKKO-p/akko-sandbox.git
cd akko-sandbox
docker compose up -d --wait
./demo.sh
```

`demo.sh` asks the same question as two different users, through the official MCP client. It
shows that Trino runs each query as the user whose token it received. It then shows the
refusals: a third user refused by the policy, a write refused by the server before it reaches
Trino, and requests without a valid token refused with `401`.

| User | Login | Sees |
|---|---|---|
| Alice | `alice_admin` | every row and every column, unmasked |
| Carol | `carol_analyst` | European customers only, with masked email addresses |
| Dave | `dave_viewer` | everything except the customers table and the `balance` column of accounts |

## Get a user's token

```bash
./token.sh carol_analyst
```

`token.sh` asks Keycloak for Carol's access token, issued for the MCP server, and prints it. The
token is valid for one hour. Paste it in place of `<the user's access token>`.

## Connect a host

For **Cursor**, put this in `~/.cursor/mcp.json`
([`examples/cursor.json`](../../examples/cursor.json)):

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

Claude Desktop reaches an HTTP server through a local bridge.
[Connect an MCP host](hosts.md#1-try-it-in-five-minutes-the-sandbox) gives its entry and the
entries of other hosts.

## Ask a question

Ask *"Which column holds the customers' email address?"* The model calls `search_columns` with a
word such as `email`, and the server returns, for Carol:

```
{"tables": [{"catalog": "demo", "schema": "crm", "table": "customers", ...}],
 "matches": [{"catalog": "demo", "schema": "crm", "table": "customers", "column": "email", "type": "varchar", ...}],
 "schemas_searched": 4, "not_searched": [], "complete": true, "next_cursor": null}
```

Then ask *"List our customers with their email and country."* With Carol's token, five European
customers come back, with masked addresses (`***@example.com`). With `./token.sh alice_admin`,
all ten come back, unmasked. With `./token.sh dave_viewer`, the search finds nothing and says it
is complete, because the policy hides the table from Dave.

To switch users, paste another token and restart the server in the host. To check a
configuration without a host, see
[Connect an MCP host](hosts.md#check-a-configuration-without-the-host).

## Stop

```bash
docker compose down -v
```

Next, read [Deployment](deployment.md) to run the server against your own Trino, and
[Identity](identity.md) to choose how it reaches Trino.
