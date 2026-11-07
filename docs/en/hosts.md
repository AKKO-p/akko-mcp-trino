# Connect an MCP host

**English** · [Français](../fr/hosts.md)

The host sends the user's access token, issued for this server. The server verifies it and names
the caller in the audit line. This page connects two MCP hosts, Claude Desktop and Cursor, then
any stdio host, the Python SDK and an agent. It covers three paths:

| Path | Transport | Who the query runs as | For |
|---|---|---|---|
| [1. Try it in five minutes](#1-try-it-in-five-minutes-the-sandbox) | streamable HTTP, `http://localhost:3000/mcp` | the sandbox user whose token you paste | a first look, on your laptop |
| [2. Your own Trino, from your workstation](#2-your-own-trino-from-your-workstation-stdio-and-oauth2) | stdio, the host starts the server | you, signed in by Trino's own browser login (`oauth2`) | one person and a Trino with OAuth2 |
| [3. A shared server for a team](#3-a-shared-server-for-a-team-streamable-http-and-a-token-per-user) | streamable HTTP over https | each user, from their own bearer token (`jwt`, the default) | a team, one server |

> **What we verified, and what we did not.** We ran every JSON block of the three paths, with only
> the placeholders changed, through the official MCP Python SDK. It started each stdio entry, or
> sent each HTTP entry's `url` and `headers`, against the sandbox with akko-mcp-trino 0.7.0 from
> PyPI and Docker Hub. It listed the 8 tools of 0.7 and called `search_columns`. The pins were
> raised to 0.7.2 afterwards, and the nine tools listed below are those of `main`; neither was
> part of that run. The keys of each block are the ones the hosts document, and the repository's
> tests check them. We did not drive the Claude Desktop or Cursor interfaces: the menu names come
> from their documentation. The [appendix](#appendix-what-was-verified) gives the details.

## Where the configuration lives

| Host | File | Notes |
|---|---|---|
| Claude Desktop, macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` | open it from **Settings > Developer > Edit Config**; quit Claude Desktop completely and start it again after each change |
| Claude Desktop, Windows | `%APPDATA%\Claude\claude_desktop_config.json` | same |
| Claude Desktop, Linux | `~/.config/Claude/claude_desktop_config.json` | same |
| Cursor, every project | `~/.cursor/mcp.json`, in your home directory (`%USERPROFILE%\.cursor\mcp.json` on Windows) | |
| Cursor, one project | `.cursor/mcp.json` at the project's root | |

Both files hold one object, `mcpServers`, with one entry per server. The examples name the entry
`trino`. If your file already has other servers, add the entry next to them.

**How each host reaches the server.** Cursor connects to a streamable HTTP server itself, with an
entry that has a `url` and `headers`. Claude Desktop's configuration file starts local servers
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

### Check that it works

In Cursor, the server is listed under **Customize**, with its tools under *Available Tools*. In
Claude Desktop, open the **+** menu of the message box (*Add files, connectors, and more*), then
**Connectors**, **Manage connectors**, and select `trino`. You should see nine tools:
`search_columns`, `list_catalogs`, `list_schemas`, `list_tables`, `describe_schema`,
`describe_table`, `profile_table`, `explain_query` and `execute_query`. The 0.7 releases have 8,
without `profile_table`. Then ask the two questions of the
[quickstart](quickstart.md#ask-a-question).

To switch users, paste another token and restart the server. In Cursor, turn it off and on again
under **Customize**; in Claude Desktop, restart the application.

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
its URL to stderr, which the hosts keep in their logs. Claude Desktop writes
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

**Getting and renewing the token.** Each user asks the identity provider for an access token
whose audience is the server (`MCP_OIDC_AUDIENCE` or `MCP_RESOURCE_URL`), the way your
organization hands out tokens to command-line tools. The server refuses a token issued for Trino,
because it only accepts tokens issued for itself. When the host reports a `401`, the token has
expired: fetch a new one, replace it, and restart the server in the host.

**What a team should do instead.** Each user's host should obtain and renew that user's token
itself, through MCP authorization. With `MCP_RESOURCE_URL` set, the server publishes the metadata
that points a host to the identity provider. Cursor documents OAuth sign-in for remote servers,
and signing in is what `mcp-remote` does when no header is given. We have not verified either
flow with this server, so this page shows the header only. A team must never share one token
between people: the server would see one user, Trino would apply that user's rights, and the
audit would name that user for everyone. The same reasoning rules out one shared identity; see
[Design notes](design-notes.md#why-not-one-shared-identity).

## Local hosts over stdio

Claude Desktop, Cursor and Mistral Vibe can start the server themselves. There is no HTTP
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

## Check a configuration without the host

[`examples/check_host_config.py`](../../examples/check_host_config.py) reads the same file the
host reads, starts or reaches the server the way the host would, lists the tools and calls
`search_columns`. It is not part of the package: run it from a clone of this repository, with
Python 3.12 or later.

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

This output is from 0.7.2, which has 8 tools. From 0.8, the list also includes `profile_table`.

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

On November 1 and 2, 2026, the runs used the sandbox at commit `dac01db` on a Linux host,
akko-mcp-trino 0.7.0 from PyPI (`uvx akko-mcp-trino==0.7.0`) and Docker Hub
(`akkoai/akko-mcp-trino:0.7.0`), the official MCP Python SDK 2.3.0, Node.js 22 and 20.18.1, and
`mcp-remote` 0.15.2. The pins of this page were raised to 0.7.2 afterwards.

| Configuration | Run as | Result |
|---|---|---|
| Cursor, path 1 (`url`, `headers`) | streamable HTTP, Carol's token | 8 tools in 0.7; `search_columns("email")` finds `demo.crm.customers.email` |
| Claude Desktop, path 1 (`npx mcp-remote`) | stdio, the bridge started by the client, on Node.js 22 and on Node.js 20.18.1 | the same |
| Cursor and Claude Desktop, path 2 (`uvx`, `oauth2`) | stdio, the sandbox's Trino with its OAuth2 authenticator, a scripted browser signing in at Keycloak | the warning quoted above at start; 8 tools in 0.7 before any login; the first call opens the login, then answers; `SELECT current_user` returns `carol_analyst` |
| path 2, the host giving up | a first call with a 5-second timeout during the login, then a second call | the first times out, the second answers as `carol_analyst` |
| path 2 with Docker | stdio, `docker run -i` | 8 tools in 0.7, the search answers |
| Cursor and Claude Desktop, path 3 | https behind a TLS proxy, a private authority (`NODE_EXTRA_CA_CERTS` for the bridge) | 8 tools in 0.7, the search answers |
| the errors in the table | `curl` and the same clients | the messages quoted above |

For the sandbox runs, only the placeholders changed: the token, Trino's name inside the sandbox
network (`trino`), its catalog `demo` and its demo authority (`TRINO_VERIFY`). The Docker run
also had `--network` and a volume for that authority. The checker resolved Cursor's
`${env:NAME}`, the way Cursor's documentation describes. The Trino OAuth2 authenticator was added
to the sandbox for this test only: the sandbox itself authenticates with JWT. The rows of the
errors table on `PATH` and on an old Trino come from the documentation, not from a run. We did
not open Claude Desktop or Cursor and click through them.

Sources: [Connect to local MCP servers](https://modelcontextprotocol.io/docs/develop/connect-local-servers)
(Claude Desktop's file, logs and tools),
[Add a connector that isn't in the directory](https://claude.com/docs/connectors/custom) (Claude's
remote connectors), [Cursor, Model Context Protocol](https://cursor.com/docs/context/mcp)
(Cursor's files, keys and variables) and [mcp-remote](https://github.com/punkpeye/mcp-remote) (the
bridge).
