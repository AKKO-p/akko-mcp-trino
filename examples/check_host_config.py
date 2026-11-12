"""Check an MCP host configuration without the host: list the tools, run one search.

Reads the same JSON file Claude Desktop, Cursor or VS Code reads, starts or
reaches the server exactly as the host would (the entry's command, args and env
over stdio, or its url and headers over streamable HTTP), lists the tools and
calls search_columns, then describe_table when --describe names a table.
Cursor's ${env:NAME} references are resolved from the environment, as Cursor
resolves them, and so are VS Code's ${input:ID}, from the variable INPUT_<ID>.

    git clone https://github.com/AKKO-p/akko-mcp-trino.git && cd akko-mcp-trino
    python3 -m venv .venv && . .venv/bin/activate      # Python 3.12 or later
    pip install akko-mcp-trino==0.8.0
    python3 examples/check_host_config.py ~/.cursor/mcp.json
    python3 examples/check_host_config.py claude_desktop_config.json --server trino --search email
    python3 examples/check_host_config.py .vscode/mcp.json --describe demo.crm.customers

It is not part of the package: run it from a clone of the repository.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

_ENV_REF = re.compile(r"\$\{env:([A-Za-z_][A-Za-z0-9_]*)\}")
_INPUT_REF = re.compile(r"\$\{input:([A-Za-z0-9_-]+)\}")


def _env(match: re.Match) -> str:
    name = match.group(1)
    if name not in os.environ:
        sys.exit(f"${{env:{name}}} is not set: export it, as Cursor would need it too")
    return os.environ[name]


def _input(match: re.Match) -> str:
    """VS Code prompts for ${input:ID}; here the answer comes from INPUT_<ID>."""
    name = "INPUT_" + re.sub(r"\W", "_", match.group(1)).upper()
    if name not in os.environ:
        sys.exit(f"${{input:{match.group(1)}}} has no answer: export {name}")
    return os.environ[name]


def _resolve(value):
    """Cursor's ${env:NAME} and VS Code's ${input:ID}, resolved; any other text, Claude
    Desktop's included, unchanged."""
    if isinstance(value, str):
        return _INPUT_REF.sub(_input, _ENV_REF.sub(_env, value))
    if isinstance(value, list):
        return [_resolve(v) for v in value]
    if isinstance(value, dict):
        return {k: _resolve(v) for k, v in value.items()}
    return value


def entry(path: str, name: str | None) -> tuple[str, dict]:
    with open(os.path.expanduser(path), encoding="utf-8") as f:
        config = json.load(f)
    # Claude Desktop, Cursor and Claude Code name the object mcpServers; VS Code names it servers.
    servers = config["mcpServers"] if "mcpServers" in config else config["servers"]
    if name is None:
        if len(servers) != 1:
            sys.exit(f"{path} holds {len(servers)} servers: name one with --server")
        name = next(iter(servers))
    return name, _resolve(servers[name])


async def _check(session: ClientSession, search: str, describe: str | None) -> None:
    init = await session.initialize()
    print(f"server: {init.server_info.name} {init.server_info.version}")
    tools = (await session.list_tools()).tools
    print(f"{len(tools)} tools: {', '.join(t.name for t in tools)}")
    result = await session.call_tool("search_columns", {"text": search})
    text = result.content[0].text if result.content else ""
    print(f"search_columns({search!r}){' -> error' if result.is_error else ''}:")
    print(text)
    if describe:
        catalog, schema, table = describe.split(".")
        args = {"catalog": catalog, "schema": schema, "table": table}
        result = await session.call_tool("describe_table", args)
        text = result.content[0].text if result.content else ""
        print(f"describe_table({describe!r}){' -> error' if result.is_error else ''}:")
        print(text)


async def run(cfg: dict, search: str, describe: str | None = None) -> None:
    if "url" in cfg:
        async with create_mcp_http_client(headers=cfg.get("headers") or {}) as http:
            async with streamable_http_client(cfg["url"], http_client=http) as (read, write):
                async with ClientSession(read, write) as session:
                    await _check(session, search, describe)
    else:
        params = StdioServerParameters(
            command=cfg["command"], args=cfg.get("args", []), env=cfg.get("env")
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await _check(session, search, describe)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "config", help="claude_desktop_config.json, .cursor/mcp.json or .vscode/mcp.json"
    )
    parser.add_argument("--server", help="the entry under mcpServers (default: the only one)")
    parser.add_argument("--search", default="email", help="the text given to search_columns")
    parser.add_argument("--describe", help="a table to describe, as catalog.schema.table")
    args = parser.parse_args()
    if args.describe and args.describe.count(".") != 2:
        sys.exit("--describe takes catalog.schema.table")
    name, cfg = entry(args.config, args.server)
    print(f"entry: {name} ({'streamable HTTP' if 'url' in cfg else 'stdio'})")
    anyio.run(run, cfg, args.search, args.describe)


if __name__ == "__main__":
    main()
