"""Check an MCP host configuration without the host: list the tools, run one search.

Reads the same JSON file Claude Desktop or Cursor reads, starts or reaches the
server exactly as the host would (the entry's command, args and env over stdio,
or its url and headers over streamable HTTP), lists the tools and calls
search_columns. Cursor's ${env:NAME} references are resolved from the
environment, as Cursor resolves them.

    git clone https://github.com/AKKO-p/akko-mcp-trino.git && cd akko-mcp-trino
    python3 -m venv .venv && . .venv/bin/activate      # Python 3.12 or later
    pip install akko-mcp-trino==0.7.1
    python3 examples/check_host_config.py ~/.cursor/mcp.json
    python3 examples/check_host_config.py claude_desktop_config.json --server trino --search email

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


def _env(match: re.Match) -> str:
    name = match.group(1)
    if name not in os.environ:
        sys.exit(f"${{env:{name}}} is not set: export it, as Cursor would need it too")
    return os.environ[name]


def _resolve(value):
    """Cursor's ${env:NAME}, resolved; any other text, Claude Desktop's included, unchanged."""
    if isinstance(value, str):
        return _ENV_REF.sub(_env, value)
    if isinstance(value, list):
        return [_resolve(v) for v in value]
    if isinstance(value, dict):
        return {k: _resolve(v) for k, v in value.items()}
    return value


def entry(path: str, name: str | None) -> tuple[str, dict]:
    with open(os.path.expanduser(path), encoding="utf-8") as f:
        servers = json.load(f)["mcpServers"]
    if name is None:
        if len(servers) != 1:
            sys.exit(f"{path} holds {len(servers)} servers: name one with --server")
        name = next(iter(servers))
    return name, _resolve(servers[name])


async def _check(session: ClientSession, search: str) -> None:
    init = await session.initialize()
    print(f"server: {init.server_info.name} {init.server_info.version}")
    tools = (await session.list_tools()).tools
    print(f"{len(tools)} tools: {', '.join(t.name for t in tools)}")
    result = await session.call_tool("search_columns", {"text": search})
    text = result.content[0].text if result.content else ""
    print(f"search_columns({search!r}){' -> error' if result.is_error else ''}:")
    print(text)


async def run(cfg: dict, search: str) -> None:
    if "url" in cfg:
        http = create_mcp_http_client(headers=cfg.get("headers") or {})
        async with streamable_http_client(cfg["url"], http_client=http) as (read, write):
            async with ClientSession(read, write) as session:
                await _check(session, search)
    else:
        params = StdioServerParameters(
            command=cfg["command"], args=cfg.get("args", []), env=cfg.get("env")
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await _check(session, search)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("config", help="claude_desktop_config.json, mcp.json or .cursor/mcp.json")
    parser.add_argument("--server", help="the entry under mcpServers (default: the only one)")
    parser.add_argument("--search", default="email", help="the text given to search_columns")
    args = parser.parse_args()
    name, cfg = entry(args.config, args.server)
    print(f"entry: {name} ({'streamable HTTP' if 'url' in cfg else 'stdio'})")
    anyio.run(run, cfg, args.search)


if __name__ == "__main__":
    main()
