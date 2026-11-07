# Connect an MCP host

**English** · [Français](../fr/hosts.md)

The host sends the user's access token, issued for this server. The server verifies it and names
the caller in the audit line. This page connects four MCP hosts, Claude Desktop, Cursor, Claude
Code and VS Code, then any stdio host, the Python SDK and two agents, one of them on the Mistral
Agents API. It covers three paths:

| Path | Transport | Who the query runs as | For |
|---|---|---|---|
| [1. Try it in five minutes](#1-try-it-in-five-minutes-the-sandbox) | streamable HTTP, `http://localhost:3000/mcp` | the sandbox user whose token you paste | a first look, on your laptop |
| [2. Your own Trino, from your workstation](#2-your-own-trino-from-your-workstation-stdio-and-oauth2) | stdio, the host starts the server | you, signed in by Trino's own browser login (`oauth2`) | one person and a Trino with OAuth2 |
| [3. A shared server for a team](#3-a-shared-server-for-a-team-streamable-http-and-a-token-per-user) | streamable HTTP over https | each user, from their own bearer token (`jwt`, the default) | a team, one server |

> **What we verified, and what we did not.** We ran every JSON block of the three paths, with only
> the placeholders changed, through the official MCP Python SDK, against the sandbox with its data
> catalog. The run used akko-mcp-trino built from `main`, not a release. The SDK started each
> stdio entry, or sent each HTTP entry's `url` and `headers`. It listed the nine tools, called
> `search_columns`, and called `describe_table` on a table the data catalog describes. Claude
> Code's `claude mcp add` and `claude mcp list` ran for real; asking it a question needs a login,
> and we did not run that step. The Mistral agent ran for real, with a Mistral key. The keys of
> each block are the ones the hosts document, and the repository's tests check them. We did not
> drive the Claude Desktop, Cursor or VS Code interfaces: the menu names come from their
> documentation. The pins stay at 0.7.2, the current release. The
> [appendix](#appendix-what-was-verified) gives the versions and the details.

## Where the configuration lives

| Host | File | Notes |
|---|---|---|
| Claude Desktop, macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` | open it from **Settings > Developer > Edit Config**; quit Claude Desktop completely and start it again after each change |
| Claude Desktop, Windows | `%APPDATA%\Claude\claude_desktop_config.json` | same |
| Claude Desktop, Linux | `~/.config/Claude/claude_desktop_config.json` | same |
| Cursor, every project | `~/.cursor/mcp.json`, in your home directory (`%USERPROFILE%\.cursor\mcp.json` on Windows) | |
| Cursor, one project | `.cursor/mcp.json` at the project's root | |
| Claude Code | `~/.claude.json` for the scopes `local` (the default) and `user`; `.mcp.json` at the project's root for the scope `project` | written by `claude mcp add`; `claude mcp list` checks each server |
| VS Code, one workspace | `.vscode/mcp.json` in the workspace | the object is `servers`, not `mcpServers` |
| VS Code, every workspace | the `mcp.json` of your user profile, opened by the command **MCP: Open User Configuration** | same |

The files of Claude Desktop, Cursor and Claude Code hold one object, `mcpServers`, with one entry
per server. VS Code names that object `servers`, and keeps the values it asks you for in `inputs`.
The examples name the entry `trino`. If your file already has other servers, add the entry next
to them.

**How each host reaches the server.** Cursor, Claude Code and VS Code connect to a streamable HTTP
server themselves, with an entry that has a `url` and `headers`. Claude Desktop's configuration file starts local servers
only, with an entry that has a `command`, its `args` and `env`. Its remote connectors, added under
**Customize > Connectors**, connect from Anthropic's cloud, not from your computer
([Claude Help Center](https://support.claude.com/en/articles/11175166-getting-started-with-custom-connectors-using-remote-mcp)).
They cannot reach a server on `localhost` or on a private network unless your organization uses
[MCP tunnels](https://claude.com/docs/connectors/mcp-tunnels/overview). Their headers hold one
credential shared by all users. Per-user sign-in there needs OAuth and a server reachable from
the internet ([Claude's documentation](https://claude.com/docs/connectors/custom)). So, for an
HTTP server, this page has Claude Desktop start a small local bridge,
[`mcp-remote`](https://github.com/punkpeye/mcp-remote), which forwards requests to the server's
URL with your header. `mcp-remote` 0.15.2 needs Node.js 20.18.1 or later (`node --version`), and
Claude Desktop uses the Node.js installed on the system.

## 1. Try it in five minutes: the sandbox

Start the sandbox and get Carol's token as in the [quickstart](quickstart.md), then paste the
token in place of `<the user's access token>`.

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

For **Claude Desktop**, put this in `claude_desktop_config.json`
([`examples/claude-desktop.json`](../../examples/claude-desktop.json)):

```json
{
  "mcpServers": {
    "trino": {
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote@0.15.2",
        "http://localhost:3000/mcp",
        "--header",
        "Authorization:${AUTH_HEADER}"
      ],
      "env": {
        "AUTH_HEADER": "Bearer <the user's access token>"
      }
    }
  }
}
```

The header's value goes in `env`, and `args` holds `Authorization:${AUTH_HEADER}` without a
space. `mcp-remote` replaces `${AUTH_HEADER}` itself, and its documentation warns that Claude
Desktop on Windows and Cursor mangle spaces inside `args`.

For **Claude Code**, add the server from the command line
([Claude Code, MCP](https://code.claude.com/docs/en/mcp)):

```bash
claude mcp add --transport http trino http://localhost:3000/mcp \
  --header "Authorization: Bearer <the user's access token>"
```

```text
Added HTTP MCP server trino with URL: http://localhost:3000/mcp to local config
Headers: {
  "Authorization": "[REDACTED]"
}
```

The default scope, `local`, keeps the entry in `~/.claude.json`, for you and this project only.
`--scope project` writes it to `.mcp.json`, a file meant to be shared: never put a token there.

For **VS Code**, put this in `.vscode/mcp.json`
([`examples/vscode-mcp.json`](../../examples/vscode-mcp.json)):

```json
{
  "inputs": [
    {
      "type": "promptString",
      "id": "trino-token",
      "description": "The user's access token for the MCP server",
      "password": true
    }
  ],
  "servers": {
    "trino": {
      "type": "http",
      "url": "http://localhost:3000/mcp",
      "headers": {
        "Authorization": "Bearer ${input:trino-token}"
      }
    }
  }
}
```

VS Code asks for the token the first time it starts the server, and keeps it out of the file
([VS Code, MCP configuration](https://code.visualstudio.com/docs/copilot/reference/mcp-configuration)).

### Check that it works

In Cursor, the server is listed under **Customize**, with its tools under *Available Tools*. In
Claude Desktop, open the **+** menu of the message box (*Add files, connectors, and more*), then
**Connectors**, **Manage connectors**, and select `trino`. In VS Code, run the command
**MCP: List Servers**. Claude Code checks each server from the command line:

```bash
claude mcp list
```

```text
Checking MCP server health…

trino: http://localhost:3000/mcp (HTTP) - ✔ Connected
```

In a Claude Code session, `/mcp` shows the server's status. You should see nine tools:
`search_columns`, `list_catalogs`, `list_schemas`, `list_tables`, `describe_schema`,
`describe_table`, `profile_table`, `explain_query` and `execute_query`. The 0.7 releases have 8,
without `profile_table`. Then ask the two questions of the
[quickstart](quickstart.md#ask-a-question).

To switch users, paste another token and restart the server. In Cursor, turn it off and on again
under **Customize**; in Claude Desktop, restart the application. In Claude Code, run
`claude mcp remove trino`, then add it again with the new token. VS Code stores the token it
asked for; its documentation does not say how to replace it, and we did not try.

## 2. Your own Trino, from your workstation: stdio and `oauth2`

Here the host starts the server itself, on your computer, and the server signs you in to Trino
with Trino's own browser login. There is nothing to deploy and no token to paste. You need:

- Trino over https, with its OAuth2 authenticator configured
  (`http-server.authentication.type` includes `OAUTH2`); this path was verified in the
  [sandbox](https://github.com/AKKO-p/akko-sandbox), with its pinned images;
- [uv](https://docs.astral.sh/uv/), whose `uvx` downloads and runs akko-mcp-trino from PyPI, or
  Docker (see below).

For **Cursor**, the entry in `~/.cursor/mcp.json` looks like this
([`examples/cursor-oauth2.json`](../../examples/cursor-oauth2.json)):

```json
{
  "mcpServers": {
    "trino": {
      "type": "stdio",
      "command": "uvx",
      "args": [
        "akko-mcp-trino==0.7.2"
      ],
      "env": {
        "MCP_TRANSPORT": "stdio",
        "TRINO_IDENTITY_MODE": "oauth2",
        "TRINO_HOST": "trino.example.internal",
        "TRINO_PORT": "8443",
        "TRINO_HTTP_SCHEME": "https",
        "TRINO_CATALOG": "iceberg",
        "MCP_AUTH_ENABLED": "false",
        "MCP_AUTH_REQUIRED": "false"
      }
    }
  }
}
```

For **Claude Desktop**, use the same entry without `"type": "stdio"`
([`examples/claude-desktop-oauth2.json`](../../examples/claude-desktop-oauth2.json)).

Replace `TRINO_HOST`, `TRINO_PORT` and `TRINO_CATALOG` with yours. If Trino's certificate is
signed by your organization's own authority, add `"TRINO_VERIFY": "/path/to/ca.pem"`.
`MCP_AUTH_ENABLED` and `MCP_AUTH_REQUIRED` are `false` because this path has no MCP token: Trino
authenticates you, and every query runs as the user Trino names.

The server says so when it starts, in the host's log and in the instructions it gives the host
at initialization:

```
WARNING:akko_mcp_trino.__main__:MCP_AUTH_ENABLED=false: no caller is verified; anyone who reaches this server queries Trino
```

Here that is expected. Over stdio, only the host process that started the server on your
computer can talk to it, and Trino's own browser login decides who you are: no query runs before
you sign in, and each runs as you. The same setting on a server that other people reach over the
network would let anyone query Trino through it. Never turn token verification off there, and
keep `oauth2` for stdio, which the server enforces.

**With Docker instead of uv**, pass the same variables to `docker run` (for Cursor, add
`"type": "stdio"` as above):

```json
{
  "mcpServers": {
    "trino": {
      "command": "docker",
      "args": [
        "run", "-i", "--rm",
        "-e", "MCP_TRANSPORT=stdio",
        "-e", "TRINO_IDENTITY_MODE=oauth2",
        "-e", "TRINO_HOST=trino.example.internal",
        "-e", "TRINO_PORT=8443",
        "-e", "TRINO_HTTP_SCHEME=https",
        "-e", "TRINO_CATALOG=iceberg",
        "-e", "MCP_AUTH_ENABLED=false",
        "-e", "MCP_AUTH_REQUIRED=false",
        "akkoai/akko-mcp-trino:0.7.2"
      ]
    }
  }
}
```

In a container, the server cannot open your browser: read the login URL in the host's log (see
below). For `TRINO_VERIFY`, mount the authority into the container too
(`"-v", "/path/to/ca.pem:/ca.pem:ro", "-e", "TRINO_VERIFY=/ca.pem"`).

### What happens at the first question

Starting the server and listing its tools never triggers a login, so the host shows the tools at
once. The first tool call does. The server opens Trino's login page in your browser and writes
its URL to stderr, which the hosts keep in their logs. In the sandbox run, where Trino is
`trino`, the line read:

```text
WARNING:akko_mcp_trino.trino_client:Open this URL to authenticate with Trino: https://trino:8443/oauth2/token/initiate/...
```

Claude Desktop writes
`mcp-server-trino.log` in `~/Library/Logs/Claude`, `%APPDATA%\Claude\logs` or
`~/.config/Claude/logs`; Cursor shows it in the **Output** panel, under **MCP Logs**. Sign in.
The server then checks who you are with `SELECT current_user` and runs the call.

**The host may give up while you sign in.** If the first answer is a timeout, finish signing in,
then ask the same question again: the login is kept, and the second call runs. You stay the user
of that process until it stops. To switch users, restart the server.

If your Trino authenticates with JWT rather than OAuth2, the host can still start the server over
stdio, with your access token in `MCP_USER_TOKEN`: see
[Local hosts over stdio](#local-hosts-over-stdio).

## 3. A shared server for a team: streamable HTTP and a token per user

One server, deployed once, serves the whole team, and each person's host sends that person's own
bearer token. This is the default mode, `jwt`. The server verifies the token, issued for the
server by your identity provider. It exchanges the token at the identity provider for a token
issued for Trino, and Trino runs the query as that user, with their own rights. Deploy the server
as in [Deployment](deployment.md#run), behind https, with `MCP_RESOURCE_URL` set to its public
URL.

The entries below put each user's token in a header written by hand. That is a shortcut for
testing and for a first rollout, not the way a team should work day to day. The token expires,
often within the hour (one hour in the sandbox), and each user then has to fetch a new one,
paste it and restart the server in the host.

For **Cursor**, the entry points at the server's address:

```json
{
  "mcpServers": {
    "trino": {
      "url": "https://mcp.example.com/mcp",
      "headers": {
        "Authorization": "Bearer <the user's access token>"
      }
    }
  }
}
```

Cursor resolves `${env:NAME}` in `url` and `headers`. The header can therefore read
`"Bearer ${env:AKKO_MCP_TOKEN}"` and keep the token out of the file, as long as Cursor starts with
that variable set.

For **Claude Desktop**, go through the bridge:

```json
{
  "mcpServers": {
    "trino": {
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote@0.15.2",
        "https://mcp.example.com/mcp",
        "--header",
        "Authorization:${AUTH_HEADER}"
      ],
      "env": {
        "AUTH_HEADER": "Bearer <the user's access token>"
      }
    }
  }
}
```

If the server's certificate is signed by your organization's own authority, add
`"NODE_EXTRA_CA_CERTS": "/path/to/ca.pem"` to `env`: the bridge does not inherit it from your
shell. `mcp-remote` can also read the header from a file (`--header-file`, one `Name: value` per
line) instead of the configuration.

For **Claude Code** and **VS Code**, use the command and the file of path 1 with the server's
address, `https://mcp.example.com/mcp`.

**Getting and renewing the token.** Each user asks the identity provider for an access token
whose audience is the server (`MCP_OIDC_AUDIENCE` or `MCP_RESOURCE_URL`), the way your
organization hands out tokens to command-line tools. The server refuses a token issued for Trino,
because it only accepts tokens issued for itself. When the host reports a `401`, the token has
expired: fetch a new one, replace it, and restart the server in the host.

**What a team should do instead.** Each user's host should obtain and renew that user's token
itself, through MCP authorization. With `MCP_RESOURCE_URL` set, the server publishes the metadata
that points a host to the identity provider. Cursor documents OAuth sign-in for remote servers,
and signing in is what `mcp-remote` does when no header is given. Neither flow has been verified
with this server, so this page shows the header only. A team must never share one token
between people: the server would see one user, Trino would apply that user's rights, and the
audit would name that user for everyone. The same reasoning rules out one shared identity; see
[Design notes](design-notes.md#why-not-one-shared-identity).

## Local hosts over stdio

Claude Desktop, Cursor, Claude Code, VS Code and Mistral Vibe can start the server themselves. There is no HTTP
request then, so the server takes the caller's identity from the environment: the user's own
access token, issued for this server, verified exactly like a bearer header, and bound to the
process. With `MCP_AUTH_REQUIRED=true`, the default, the server refuses to start without it.
Queries run as the user in `jwt` mode (the token is exchanged for one for Trino, as over http) and
in `impersonation` mode, and as `TRINO_USER` in `service` mode.

The entry below is [`examples/mcp-stdio.json`](../../examples/mcp-stdio.json):

```json
{
  "mcpServers": {
    "trino": {
      "command": "uvx",
      "args": [
        "akko-mcp-trino==0.7.2"
      ],
      "env": {
        "MCP_TRANSPORT": "stdio",
        "TRINO_HOST": "trino.example.internal",
        "TRINO_PORT": "8443",
        "TRINO_HTTP_SCHEME": "https",
        "MCP_JWKS_URL": "https://idp.example.com/realms/data/protocol/openid-connect/certs",
        "MCP_OIDC_ISSUER": "https://idp.example.com/realms/data",
        "MCP_OIDC_AUDIENCE": "akko-mcp",
        "MCP_JWT_USER_CLAIM": "preferred_username",
        "TRINO_TOKEN_EXCHANGE_URL": "https://idp.example.com/realms/data/protocol/openid-connect/token",
        "TRINO_TOKEN_EXCHANGE_CLIENT_ID": "akko-mcp",
        "TRINO_TOKEN_EXCHANGE_CLIENT_SECRET": "<this server's client secret>",
        "TRINO_TOKEN_EXCHANGE_AUDIENCE": "trino",
        "MCP_USER_TOKEN": "<the user's access token, issued for the MCP server>"
      }
    }
  }
}
```

`MCP_JWT_USER_CLAIM` is set for Keycloak; the default is `sub` (see
[The user claim](identity.md#the-user-claim)).

In **Claude Code**, each variable goes in an `--env`, and the command follows `--`:

```bash
claude mcp add trino \
  --env MCP_TRANSPORT=stdio \
  --env TRINO_HOST=trino.example.internal \
  --env TRINO_PORT=8443 \
  --env TRINO_HTTP_SCHEME=https \
  --env MCP_JWKS_URL=https://idp.example.com/realms/data/protocol/openid-connect/certs \
  --env MCP_OIDC_ISSUER=https://idp.example.com/realms/data \
  --env MCP_OIDC_AUDIENCE=akko-mcp \
  --env MCP_JWT_USER_CLAIM=preferred_username \
  --env TRINO_TOKEN_EXCHANGE_URL=https://idp.example.com/realms/data/protocol/openid-connect/token \
  --env TRINO_TOKEN_EXCHANGE_CLIENT_ID=akko-mcp \
  --env "TRINO_TOKEN_EXCHANGE_CLIENT_SECRET=<this server's client secret>" \
  --env TRINO_TOKEN_EXCHANGE_AUDIENCE=trino \
  --env "MCP_USER_TOKEN=<the user's access token, issued for the MCP server>" \
  -- uvx akko-mcp-trino==0.7.2
```

`claude mcp get trino` then shows the entry, the variables in clear, and `Status: ✔ Connected`
once the server answers. In **VS Code**, the same entry goes under `servers`, with
`"type": "stdio"`.

## From Python

With the official SDK, a client looks like this:

```python
import anyio
from mcp import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client


async def main(token: str):
    http = create_mcp_http_client(headers={"Authorization": f"Bearer {token}"})
    url = "http://localhost:3000/mcp"
    async with streamable_http_client(url, http_client=http) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("execute_query", {"sql": "SELECT 1"})
            print(result.content[0].text)


anyio.run(main, "<token>")
```

## From an agent

[`examples/agent.py`](../../examples/agent.py) is a complete agent in eighty lines. It works with
any OpenAI-compatible model (Mistral on La Plateforme, through OpenRouter or LiteLLM, or any
other provider), the MCP tools and the user's token. The model never sees the token: the server
receives it on every tool call and verifies it.

```bash
pip install akko-mcp-trino openai
export LLM_BASE_URL=https://api.mistral.ai/v1
export LLM_API_KEY=...
export LLM_MODEL=mistral-small-latest
export MCP_URL=http://localhost:3000/mcp
export USER_TOKEN=<the user's access token>
python examples/agent.py "Which catalogs can I see, and what is in them?"
```

The [examples](../../examples/README.md) folder has the details.

## From the Mistral Agents API

[`examples/mistral_agents.py`](../../examples/mistral_agents.py) uses the official `mistralai` SDK
and its MCP client. The MCP client runs in your process: it reaches the server with the user's
token in a header and runs each tool call the model asks for. Mistral's Conversations API receives
the question, the tool descriptions and the tool results. It never receives the token and never
reaches the server, so the server can stay on `localhost` or on a private network.

```python
async def main(question: str) -> None:
    client = Mistral(api_key=os.environ["MISTRAL_API_KEY"])
    server = MCPClientStreamableHTTP(
        StreamableHTTPServerParams(
            url=os.environ.get("MCP_URL", "http://localhost:3000/mcp"),
            headers={"Authorization": f"Bearer {os.environ['USER_TOKEN']}"},
        )
    )
    model = os.environ.get("MISTRAL_MODEL", "mistral-medium-latest")
    async with RunContext(model=model) as run_ctx:
        await run_ctx.register_mcp_client(server)
        result = await client.beta.conversations.run_async(run_ctx=run_ctx, inputs=question)
```

The file adds the imports and prints each tool call, the start of each tool result, then the
answer:

```bash
pip install "mistralai[agents]==3.7.0"
export MISTRAL_API_KEY=...
export MCP_URL=http://localhost:3000/mcp
export USER_TOKEN=<the user's access token>
python examples/mistral_agents.py "Which customers do we have, with their e-mail and country?"
```

With Carol's token and `mistral-medium-latest`, the model listed the catalogs, the schemas and
the tables, described `customers`, then read it. Carol gets five European customers, with masked
addresses:

```text
tool call: list_catalogs {}
tool result: [{"type": "text", "text": "{\"catalogs\": [\"demo\"], \"next_offset\": null, \"truncated\": false}"}]
tool call: list_schemas {"catalog": "demo"}
...
tool call: describe_table {"catalog": "demo", "schema": "crm", "table": "customers"}
...
tool call: execute_query {"sql": "SELECT full_name, email, country FROM demo.crm.customers"}
...
Here are the customers with their email addresses and countries:

| **Full Name**   | **Email**               | **Country** |
|-----------------|-------------------------|-------------|
| Lena Vogel      | ***@example.com         | DE          |
| Hugo Martin     | ***@example.com         | FR          |
| Sofia Ruiz      | ***@example.org         | ES          |
| Giulia Conti    | ***@example.org         | IT          |
| Emma de Vries   | ***@example.org         | NL          |
```

## Check a configuration without the host

[`examples/check_host_config.py`](../../examples/check_host_config.py) reads the same file the
host reads, starts or reaches the server the way the host would, lists the tools and calls
`search_columns`. With `--describe`, it also calls `describe_table`. It is not part of the
package: run it from a clone of this repository, with Python 3.12 or later.

```bash
git clone https://github.com/AKKO-p/akko-mcp-trino.git
cd akko-mcp-trino
python3 -m venv .venv && . .venv/bin/activate
pip install akko-mcp-trino==0.7.2
python3 examples/check_host_config.py ~/.cursor/mcp.json --search email --describe demo.crm.customers
```

Against the sandbox with its data catalog (the `catalogs` profile), with Carol's token, the
output was:

```text
entry: trino (streamable HTTP)
server: trino-mcp 0.7.2
9 tools: search_columns, list_catalogs, list_schemas, list_tables, describe_schema, describe_table, profile_table, explain_query, execute_query
search_columns('email'):
{"tables": [{"catalog": "demo", "schema": "crm", "table": "customers", "comment": "People and companies who hold an account"}], "matches": [{"catalog": "demo", "schema": "crm", "table": "customers", "column": "email", "type": "varchar", "comment": null}], "schemas_searched": 5, "not_searched": [], "complete": true, "next_cursor": null}
describe_table('demo.crm.customers'):
{"columns": ["Column", "Type", "Extra", "Comment"], "rows": [["id", "integer", "", ""], ...], "truncated": false, "next_column_offset": null, "columns_cut": false, "table": {"description": "Everyone who holds an account: one row per person or company", "sources": {"description": "atlas"}, "terms": [{"name": "Customer", "ref": "t1"}]}, "column_context": {"id": {"description": "Identifier of the customer", "sources": {"description": "atlas"}}, ...}, "terms": {"t1": {"name": "Customer", "glossary": "Sales", "definition": "A person or a company that holds at least one account", "status": "approved", "source": "atlas"}}, "context_truncated": false, "undescribed_columns": [], "context_sources": {"atlas": "ok"}}
```

The run used `main`, which still reports version 0.7.2. The `description`, the terms and
`context_sources` come from the data catalog, Apache Atlas, through the server: see
[Context from a data catalog](catalog-context.md). A server without a data catalog, such as the stdio entry
above, answers with the columns only.

It resolves Cursor's `${env:NAME}` from your environment, as Cursor does, and stops if one is not
set. For a file that holds several servers, name the entry with `--server trino`.

## Common errors

| What you see | Why | Fix |
|---|---|---|
| `401`, `this server requires a bearer token issued for it by the identity provider: send Authorization: Bearer <access token>` | no header, or a header without the `Bearer ` prefix | write `Bearer `, then the token, with the space |
| `401`, `the bearer token is invalid, expired, or not issued for this server` | the token expired (one hour in the sandbox), or its audience is another client, Trino included | ask for a new token issued for the server, replace it and restart the server in the host; in the sandbox, `./token.sh <user>` without a second argument |
| `401`, and the server logs `token refused: no sub claim` (or the claim `MCP_JWT_USER_CLAIM` names) | the token has no such claim, or its value is empty or not text | set `MCP_JWT_USER_CLAIM` to the claim your identity provider puts the user in (`preferred_username` for Keycloak) |
| the bridge logs `Dynamic Client Registration rejected (HTTP 401)` | the server refused the token, and `mcp-remote` tried to sign in by itself | fix the token as above; if it keeps trying, delete `~/.mcp-auth` and restart the host |
| the bridge logs `Non-HTTPS URLs are only allowed for localhost or when --allow-http flag is provided` | an `http://` URL that is not `localhost` | serve the server over https (path 3); use `--allow-http` only on a trusted private network |
| the bridge logs `ReferenceError: File is not defined`, or `npm warn EBADENGINE` for `undici` | Node.js is older than 20.18.1 | install Node.js 20.18.1 or later; Claude Desktop uses the system's |
| the bridge logs `unable to get local issuer certificate` | the server's certificate is signed by an authority Node.js does not know | set `NODE_EXTRA_CA_CERTS` in the entry's `env` |
| `TRINO_IDENTITY_MODE=oauth2 requires TRINO_HTTP_SCHEME=https` | `oauth2` over http | set `TRINO_HTTP_SCHEME=https`, with Trino's https port |
| `TRINO_IDENTITY_MODE=oauth2 requires MCP_TRANSPORT=stdio` | `oauth2` on an HTTP transport | `oauth2` is for path 2 only; a team server uses `jwt` |
| `MCP_AUTH_REQUIRED=true and MCP_USER_TOKEN is missing or invalid: refusing to serve over stdio without a verified identity` | a stdio entry in `jwt` mode without a token | add `MCP_USER_TOKEN`, or use `oauth2` with `MCP_AUTH_ENABLED` and `MCP_AUTH_REQUIRED` set to `false` |
| the first question of path 2 times out | the host stopped waiting while the browser login was open | sign in, then ask again |
| the server does not start, and the host's log shows `spawn uvx ENOENT` or `npx` not found | the host does not see your shell's `PATH` | give the full path in `command` (`which uvx`, `which npx`) |
| queries fail on an old Trino | akko-mcp-trino speaks the protocol of the Trino versions listed in [Prerequisites](prerequisites.md#compatibility) | check the version (`curl https://<trino>/v1/info`) and upgrade Trino |

## Appendix: what was verified

On November 7, 2026, the runs used akko-mcp-trino built from commit `2236ee9` of `main`, as an
image and as a wheel, on a Linux host. That build reports version 0.7.2. They used the sandbox at
commit `67f936a` with its `catalogs` profile: `trinodb/trino:482`,
`quay.io/keycloak/keycloak:26.7.5`, `openpolicyagent/opa:1.21.1-static`, `postgres:17.11-alpine`
and Apache Atlas 2.4.0, built from the sandbox's `catalogs/atlas/Dockerfile`. The clients were the
official MCP Python SDK 2.3.0 on Python 3.13, Node.js 22.23.4, `mcp-remote` 0.15.2, uv 0.13.7,
Claude Code 2.1.319 (`@anthropic-ai/claude-code` from npm) and `mistralai` 3.7.0. Each run listed
nine tools, found `demo.crm.customers.email` with `search_columns("email")`, and called
`describe_table` on `demo.crm.customers`.

| Configuration | Run as | Result |
|---|---|---|
| Cursor, path 1 (`url`, `headers`) | streamable HTTP, Carol's token | 9 tools; the search answers; `describe_table` carries the data catalog's context, `context_sources` `{"atlas": "ok"}` |
| Claude Desktop, path 1 (`npx mcp-remote`) | stdio, the bridge started by the client, on Node.js 22 | the same |
| VS Code, path 1 (`servers`, `inputs`) | streamable HTTP, the input answered with Carol's token | the same |
| Claude Code, path 1 | `claude mcp add --transport http`, then `claude mcp list` and `claude mcp get` | `✔ Connected`, as quoted above |
| Cursor and Claude Desktop, path 2 (`uvx`, `oauth2`) | stdio, the sandbox's Trino with an OAuth2 authenticator, a scripted browser signing in at Keycloak | the warning quoted above at start; 9 tools before any login; the first call writes the login URL, then answers once signed in; the audit names `carol_analyst` |
| path 2 with Docker | stdio, `docker run -i` | the same |
| Cursor and Claude Desktop, path 3 | https behind a TLS proxy, a private authority (`NODE_EXTRA_CA_CERTS` for the bridge, `SSL_CERT_FILE` for the SDK) | 9 tools; the search answers; `describe_table` carries the context |
| the stdio entry (`MCP_USER_TOKEN`) | stdio, `jwt` mode, token exchange at Keycloak | 9 tools; the search answers; `describe_table` returns the columns, without context, as that entry configures no data catalog |
| Claude Code, the stdio entry | `claude mcp add ... -- uvx ...`, then `claude mcp get` | `✔ Connected` |
| From Python | the block run as it is | `{"columns": ["_col0"], "rows": [[1]], "row_count": 1, "truncated": false}` |
| From the Mistral Agents API | `mistral-medium-latest`, Carol's token | the tool calls and the answer quoted above |

For the sandbox runs, only the placeholders changed: the token, Trino's name inside the sandbox
network (`trino`), its catalog `demo`, the identity provider's addresses and its demo authority
(`TRINO_VERIFY`, and `TRINO_TOKEN_EXCHANGE_VERIFY` for the stdio entry). `akko-mcp-trino==0.7.2`
became the wheel built from `main` (`uvx --from <wheel> akko-mcp-trino`), and the Docker image
the image built from `main`. The Docker run also had `--network` and a volume for the authority.
The checker answered VS Code's `${input:trino-token}` from `INPUT_TRINO_TOKEN`. The Trino OAuth2
authenticator was added to the sandbox for this test only: the sandbox itself authenticates with
JWT.

What did not run on `main`:

- Asking Claude Code a question, and its `/mcp` panel: both need a login to Anthropic, and we used
  none. The commands for that step come from Claude Code's documentation.
- VS Code's interface, and its stdio entry: VS Code cannot be driven here. Its file was run through
  the SDK, with its keys as VS Code's documentation gives them.
- The Claude Desktop, Cursor and VS Code interfaces, menus included.
- [`examples/agent.py`](../../examples/agent.py), with `mistral-small-latest` and then
  `mistral-medium-latest`: the provider answered `429 - Rate limit exceeded` each time. Its first run on `main` failed on a name the SDK 2.3.0
  had changed (`Tool.inputSchema`, now `Tool.input_schema`); the example is fixed.
- Path 2 with the host giving up during the login, and the bridge on Node.js 20.18.1. Both ran on
  November 1 and 2, 2026, with akko-mcp-trino 0.7.0 and the sandbox at commit `dac01db`.
- The rows of the errors table. They come from that same run of 0.7.0, except the rows on `PATH`
  and on an old Trino, which come from the documentation.

Sources: [Connect to local MCP servers](https://modelcontextprotocol.io/docs/develop/connect-local-servers)
(Claude Desktop's file, logs and tools),
[Add a connector that isn't in the directory](https://claude.com/docs/connectors/custom) (Claude's
remote connectors), [Cursor, Model Context Protocol](https://cursor.com/docs/context/mcp)
(Cursor's files, keys and variables), [Claude Code, MCP](https://code.claude.com/docs/en/mcp)
(`claude mcp`, its scopes and files),
[VS Code, MCP configuration](https://code.visualstudio.com/docs/copilot/reference/mcp-configuration)
(`servers`, `inputs` and the keys of an entry) and [mcp-remote](https://github.com/punkpeye/mcp-remote)
(the bridge).
