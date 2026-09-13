"""Server assembly.

`build_server` creates the MCPServer instance and the Trino client, registers the
five generic tools, then applies any extra tool registrars a product wants to
add. That registrar hook is the only extension point, and it is deliberate.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable, Tuple

from .config import Config
from .tools import register_query_tools
from .trino_client import TrinoClient

# A tool registrar receives (mcp, client) and adds its own @mcp.tool() functions.
ToolRegistrar = Callable[[Any, TrinoClient], None]


def _default_mcp_factory(
    name: str,
):  # pragma: no cover - production factory (real MCPServer), proven by running it
    # Lazy import: the SDK server is only needed for real assembly, which keeps
    # build_server testable through an injected mcp_factory.
    from mcp.server.mcpserver import MCPServer

    return MCPServer(name)


def build_server(
    config: Config,
    *,
    extra_tool_registrars: Iterable[ToolRegistrar] = (),
    mcp_factory: Callable[[str], Any] = _default_mcp_factory,
    metrics: Any = None,
    audit: Any = None,
    context: Any = None,
) -> Tuple[Any, TrinoClient]:
    """Assemble the server. `mcp_factory` is injectable so assembly can be tested
    without depending on an SDK version. `metrics`, when given, instruments the client;
    `audit`, when given, receives one record per tool call;
    `context`, when given, is the ContextProvider (else built from the environment by main)."""
    mcp = mcp_factory(config.server_name)
    client = TrinoClient(config, metrics=metrics)
    register_query_tools(mcp, client, read_only=config.read_only, audit=audit, context=context)
    for registrar in extra_tool_registrars:
        registrar(mcp, client)
    return mcp, client
