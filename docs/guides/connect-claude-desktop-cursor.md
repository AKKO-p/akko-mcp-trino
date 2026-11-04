# Connect Claude Desktop or Cursor to akko-mcp-trino

This guide connects two MCP hosts, Claude Desktop and Cursor, to akko-mcp-trino 0.7.2.
It covers three paths:

| Path | Transport | Who the query runs as | For |
|---|---|---|---|
| [1. Try it in five minutes](#1-try-it-in-five-minutes-the-sandbox) | streamable HTTP, `http://localhost:3000/mcp` | the sandbox user whose token you paste | a first look, on your laptop |
| [2. Your own Trino, from your workstation](#2-your-own-trino-from-your-workstation-stdio-and-oauth2) | stdio, the host starts the server | you, signed in by Trino's own browser login (`oauth2`) | one person and a Trino with OAuth2 |
| [3. A shared server for a team](#3-a-shared-server-for-a-team-streamable-http-and-a-token-per-user) | streamable HTTP over https | each user, from their own bearer token (`jwt`, the default) | a team, one server |

> **What we verified, and what we did not.** Every JSON block below was run, as written
> apart from the placeholders, by a real MCP client: the official MCP Python SDK started the
> `command`, `args` and `env` of each stdio entry, or sent the `url` and `headers` of each
> HTTP entry, against the sandbox with akko-mcp-trino 0.7.0 from PyPI and Docker Hub. It
> listed the eight tools and called `search_columns`. The keys of each block are the ones
> the hosts' documentation names, and the repository's tests check them. We did not drive
> the Claude Desktop or Cursor user interfaces themselves: the menus named here come from
> their documentation. [The details are at the end](#what-was-verified).

## Where the configuration lives

| Host | File | Notes |
|---|---|---|
| Claude Desktop, macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` | open it from **Settings > Developer > Edit Config**; quit Claude Desktop completely and start it again after each change |
| Claude Desktop, Windows | `%APPDATA%\Claude\claude_desktop_config.json` | same |
| Claude Desktop, Linux | `~/.config/Claude/claude_desktop_config.json` | same |
| Cursor, every project | `~/.cursor/mcp.json`, in your home directory (`%USERPROFILE%\.cursor\mcp.json` on Windows) | |
| Cursor, one project | `.cursor/mcp.json` at the project's root | |

Both files hold one object, `mcpServers`, with one entry per server. The examples name the
entry `trino`. If your file already has other servers, add the entry next to them.

**How each host reaches the server.** Cursor connects to a streamable HTTP server itself:
an entry with a `url` and `headers`. Claude Desktop's configuration file starts local
servers only: an entry with a `command`, its `args` and `env`. Its remote connectors,
added under **Customize > Connectors**, connect from Anthropic's cloud, not from your
computer ([Claude Help Center](https://support.claude.com/en/articles/11175166-getting-started-with-custom-connectors-using-remote-mcp)),
so they cannot reach a server on `localhost` or on a private network unless your
organization uses [MCP tunnels](https://claude.com/docs/connectors/mcp-tunnels/overview).
Their request headers are meant for one credential shared by everyone; signing each person
in there goes through OAuth, with a server reachable from the internet
([Claude's documentation](https://claude.com/docs/connectors/custom)). So for an HTTP
server, this guide has Claude Desktop start a small local bridge, [`mcp-remote`](https://github.com/punkpeye/mcp-remote),
which forwards to the server's URL with your header. `mcp-remote` 0.15.2 needs Node.js
20.18.1 or later (`node --version`), and Claude Desktop uses the Node.js installed on the
system.

## 1. Try it in five minutes: the sandbox

The [sandbox](https://github.com/AKKO-p/akko-sandbox) runs Keycloak, Trino, OPA and
akko-mcp-trino on your machine, with three demo users. You need Docker with Compose v2,
about 2.5 GB of free memory, and the ports `8080`, `3000` and `6274` free.

```bash
git clone https://github.com/AKKO-p/akko-sandbox.git
cd akko-sandbox
docker compose up -d --wait
./token.sh carol_analyst
```

`token.sh` asks Keycloak for `carol_analyst`'s access token, issued for the MCP server, and
prints it. It is valid for one hour. Paste it in place of `<the user's access token>`.

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

**Claude Desktop**, in `claude_desktop_config.json` ([`examples/claude-desktop.json`](../../examples/claude-desktop.json)):

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
space: `mcp-remote` replaces `${AUTH_HEADER}` itself, and its documentation warns that
Claude Desktop on Windows and Cursor mangle spaces inside `args`.

### Check that it works

1. **The tools appear.** In Cursor, the server is listed under **Customize**, with its
   tools under *Available Tools*. In Claude Desktop, open the **+** menu of the message box
   (*Add files, connectors, and more*), then **Connectors**, **Manage connectors**, and
   select `trino`. You should see eight tools: `search_columns`, `list_catalogs`,
   `list_schemas`, `list_tables`, `describe_schema`, `describe_table`, `explain_query`,
   `execute_query`.
2. **Ask where the data is.** *"Which column holds the customers' e-mail address?"* The
   model calls `search_columns` with a word such as `email`, and the server answers, for
   carol:

   ```
   {"tables": [{"catalog": "demo", "schema": "crm", "table": "customers", ...}],
    "matches": [{"catalog": "demo", "schema": "crm", "table": "customers", "column": "email", "type": "varchar", ...}],
    "schemas_searched": 4, "not_searched": [], "complete": true, "next_cursor": null}
   ```
3. **Ask for the data.** *"List our customers with their e-mail and country."* With
   carol's token, five European customers come back, their addresses masked
   (`***@example.com`). With `./token.sh alice_admin`, all ten, in clear. With
   `./token.sh dave_viewer`, the search finds nothing and says it is complete: the policy
   hides the table from him, so it is not his to see.

To switch user, paste another token and restart the server: in Cursor, turn it off and on
again under **Customize**; in Claude Desktop, restart the application. Stop the sandbox with
`docker compose down -v`.

## 2. Your own Trino, from your workstation: stdio and `oauth2`

Here the host starts the server itself, on your computer, and the server signs you in to
Trino with Trino's own browser login. Nothing to deploy, no token to paste. You need:

- Trino over https, with its OAuth2 authenticator configured
  (`http-server.authentication.type` includes `OAUTH2`); akko-mcp-trino supports Trino 351
  and later, and this path was verified with Trino 482;
- [uv](https://docs.astral.sh/uv/), whose `uvx` downloads and runs akko-mcp-trino from
  PyPI, or Docker (below).

**Cursor**, in `~/.cursor/mcp.json` ([`examples/cursor-oauth2.json`](../../examples/cursor-oauth2.json)):

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

**Claude Desktop**: the same entry without `"type": "stdio"`
([`examples/claude-desktop-oauth2.json`](../../examples/claude-desktop-oauth2.json)).

Replace `TRINO_HOST`, `TRINO_PORT` and `TRINO_CATALOG` with yours. If Trino's certificate
is signed by your company's own authority, add `"TRINO_VERIFY": "/path/to/ca.pem"`.
`MCP_AUTH_ENABLED` and `MCP_AUTH_REQUIRED` are `false` because there is no MCP token in this
path: Trino authenticates you, and every query runs as the user Trino names.

The server says so when it starts, in the host's log, and in the instructions it gives the
host at initialization:

```
WARNING:akko_mcp_trino.__main__:MCP_AUTH_ENABLED=false: no caller is verified; anyone who reaches this server queries Trino
```

Here that is expected. Over stdio, the only thing that reaches the server is the host
process that started it on your computer, and Trino's own browser login decides who you
are: no query runs before you sign in, and each runs as you. The same setting on a server
other people can reach over the network would let anyone query Trino through it: never
turn the door off there, and keep `oauth2` for stdio, which the server enforces.

**With Docker instead of uv**, the same variables go to `docker run` (for Cursor, add
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

In a container, the server cannot open your browser: read the login URL in the host's log
(below). For `TRINO_VERIFY`, mount the authority into the container too
(`"-v", "/path/to/ca.pem:/ca.pem:ro", "-e", "TRINO_VERIFY=/ca.pem"`).

### What happens at the first question

Starting the server and listing its tools never logs in, so the host shows the eight tools
at once. The first tool call does: the server opens Trino's login page in your browser and
writes its URL to stderr, which the hosts keep in their logs (Claude Desktop:
`mcp-server-trino.log` in `~/Library/Logs/Claude`, `%APPDATA%\Claude\logs` or
`~/.config/Claude/logs`; Cursor: the **Output** panel, **MCP Logs**). Sign in. The server
then checks who you are with `SELECT current_user` and runs the call.

**The host may give up while you sign in.** If the first answer is a timeout, sign in, then
ask the same question again: the login is kept, and the second call runs. You stay the
user of that process until it stops; to switch users, restart the server.

If your Trino authenticates with JWT rather than OAuth2, the host can still start the
server over stdio, with your access token in `MCP_USER_TOKEN`: see
[`examples/mcp-stdio.json`](../../examples/mcp-stdio.json) and
[Local hosts over stdio](../../README.md#local-hosts-over-stdio).

## 3. A shared server for a team: streamable HTTP and a token per user

One server, deployed once, serves the whole team; each person's host sends that person's
own bearer token. That is the default mode, `jwt`: the server verifies the token (issued
for the server by your identity provider, then IdP), exchanges it at the IdP for a token
issued for Trino, and Trino runs the query as that user, with their own rights. Deploy the
server as in [Install and run](../../README.md#install-and-run), behind https, with
`MCP_RESOURCE_URL` set to its public URL.

The configurations below put each user's token in a header written by hand. That is a
shortcut for testing and for a first rollout, not the way a team should run day to day:
the token expires, often within the hour (one hour in the sandbox), and each user then has
to fetch a new one, paste it and restart the server in the host.

**Cursor**:

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

Cursor resolves `${env:NAME}` in `url` and `headers`, so the header can read
`"Bearer ${env:AKKO_MCP_TOKEN}"` and keep the token out of the file, as long as Cursor is
started with that variable set.

**Claude Desktop**, through the bridge:

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

If the server's certificate is signed by your company's own authority, add
`"NODE_EXTRA_CA_CERTS": "/path/to/ca.pem"` to `env`: the bridge does not inherit it from
your shell. `mcp-remote` can also read the header from a file (`--header-file`, one
`Name: value` per line) instead of the configuration.

**Getting and renewing the token.** Each user asks the IdP for an access token whose
audience is the server (`MCP_OIDC_AUDIENCE` or `MCP_RESOURCE_URL`), the way your
organization hands out tokens to command-line tools. A token issued for Trino is refused:
the server only accepts tokens issued for itself. When the host answers `401`, the token
has expired: fetch a new one, replace it, and restart the server in the host.

**What a team should do instead.** Each user's host should obtain and renew that user's
own token itself, through MCP authorization: with `MCP_RESOURCE_URL` set, the server
publishes the metadata that points a host to the IdP. Cursor documents OAuth sign-in for
remote servers, and signing in is what `mcp-remote` does when no header is given. We have
not verified either flow with this server, so this guide shows the header only. What a team
must never do is share one token between people: the server would see one user, Trino
would apply one user's rights, and the audit would name that user for everyone.

**Why not one shared identity?** In `service` mode every query runs as one Trino account,
whoever asks. Everyone then reads what that account reads, Trino's row filters and masks
can no longer tell users apart, and the audit names the account rather than the person.
So `service` is never the default: the server warns at start and tells every host, and it
only fits data that every caller may read anyway.

## Check a configuration without the host

[`examples/check_host_config.py`](../../examples/check_host_config.py) reads the same file
the host reads, starts or reaches the server the way the host would, lists the tools and
calls `search_columns`. It is not part of the package: run it from a clone of this
repository, with Python 3.12 or later.

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

It resolves Cursor's `${env:NAME}` from your environment, as Cursor does, and stops if
one is not set. For a file that holds several servers, name the entry with `--server trino`.

## Common errors

| What you see | Why | Fix |
|---|---|---|
| `401`, `this server requires a bearer token issued for it by the identity provider: send Authorization: Bearer <access token>` | no header, or a header without the `Bearer ` prefix | write `Bearer ` then the token, with the space |
| `401`, `the bearer token is invalid, expired, or not issued for this server` | the token expired (one hour in the sandbox), or its audience is another client, Trino's included | ask for a new token, issued for the server, replace it and restart the server in the host; in the sandbox, `./token.sh <user>` without a second argument |
| `401`, and the server logs `token refused: no sub claim` (or the claim `MCP_JWT_USER_CLAIM` names) | the token has no such claim, or its value is empty or not text | set `MCP_JWT_USER_CLAIM` to the claim your identity provider puts the user in (`preferred_username` for Keycloak) |
| the bridge logs `Dynamic Client Registration rejected (HTTP 401)` | the server refused the token, and `mcp-remote` tried to sign in by itself | fix the token as above; if it keeps trying, delete `~/.mcp-auth` and restart the host |
| the bridge logs `Non-HTTPS URLs are only allowed for localhost or when --allow-http flag is provided` | an `http://` URL that is not `localhost` | serve the server over https (path 3); `--allow-http` only on a trusted private network |
| the bridge logs `ReferenceError: File is not defined`, or `npm warn EBADENGINE` for `undici` | Node.js is older than 20.18.1 | install Node.js 20.18.1 or later; Claude Desktop uses the system's |
| the bridge logs `unable to get local issuer certificate` | the server's certificate is signed by an authority Node.js does not know | `NODE_EXTRA_CA_CERTS` in the entry's `env` |
| `TRINO_IDENTITY_MODE=oauth2 requires TRINO_HTTP_SCHEME=https` | `oauth2` over http | `TRINO_HTTP_SCHEME=https`, and Trino's https port |
| `TRINO_IDENTITY_MODE=oauth2 requires MCP_TRANSPORT=stdio` | `oauth2` on an HTTP transport | `oauth2` is for path 2 only; a team server uses `jwt` |
| `MCP_AUTH_REQUIRED=true and MCP_USER_TOKEN is missing or invalid: refusing to serve over stdio without a verified identity` | a stdio entry in `jwt` mode without a token | add `MCP_USER_TOKEN`, or use `oauth2` with `MCP_AUTH_ENABLED` and `MCP_AUTH_REQUIRED` at `false` |
| the first question of path 2 times out | the host stopped waiting while the browser login was open | sign in, then ask again |
| the server does not start, `spawn uvx ENOENT` or `npx` not found in the host's log | the host does not see your shell's `PATH` | give the full path in `command` (`which uvx`, `which npx`) |
| queries fail on an old Trino | akko-mcp-trino speaks the protocol of Trino 351 and later | check the version (`curl https://<trino>/v1/info`) and upgrade Trino |

## What was verified

On 1 and 2 November 2026, with the sandbox at commit `dac01db` on a Linux host, akko-mcp-trino
0.7.0 from PyPI (`uvx akko-mcp-trino==0.7.0`) and Docker Hub (`akkoai/akko-mcp-trino:0.7.0`),
the official MCP Python SDK 2.3.0, Node.js 22 and 20.18.1, and `mcp-remote` 0.15.2:

| Configuration | Run as | Result |
|---|---|---|
| Cursor, path 1 (`url`, `headers`) | streamable HTTP, carol's token | 8 tools; `search_columns("email")` finds `demo.crm.customers.email` |
| Claude Desktop, path 1 (`npx mcp-remote`) | stdio, the bridge started by the client, on Node.js 22 and on Node.js 20.18.1 | the same |
| Cursor and Claude Desktop, path 2 (`uvx`, `oauth2`) | stdio, Trino 482 with its OAuth2 authenticator, a scripted browser signing in at Keycloak | the warning quoted above at start; 8 tools before any login; the first call opens the login, then answers; `SELECT current_user` returns `carol_analyst` |
| path 2, the host giving up | a first call with a 5 second timeout during the login, then a second call | the first times out, the second answers as `carol_analyst` |
| path 2 with Docker | stdio, `docker run -i` | 8 tools, the search answers |
| Cursor and Claude Desktop, path 3 | https behind a TLS proxy, a private authority (`NODE_EXTRA_CA_CERTS` for the bridge) | 8 tools, the search answers |
| the errors in the table | `curl` and the same clients | the messages quoted above |

For the sandbox runs, only the placeholders changed: the token, Trino's name inside the
sandbox network (`trino`), its catalog `demo` and its demo authority (`TRINO_VERIFY`); the
Docker run also had `--network` and a volume for that authority. Cursor's `${env:NAME}` was
resolved by the checker, the way Cursor's documentation describes. The
Trino OAuth2 authenticator was added to the sandbox for this test only: the sandbox itself
authenticates with JWT. The rows of the errors table on `PATH` and on an old Trino come from
the documentation, not from a run. What we did not do is open Claude Desktop or Cursor and
click through them.

Sources: [Connect to local MCP servers](https://modelcontextprotocol.io/docs/develop/connect-local-servers)
(Claude Desktop's file, logs and tools), [Add a connector that isn't in the directory](https://claude.com/docs/connectors/custom)
(Claude's remote connectors), [Cursor, Model Context Protocol](https://cursor.com/docs/context/mcp)
(Cursor's files, keys and variables), [mcp-remote](https://github.com/punkpeye/mcp-remote) (the bridge).
