"""Server assembly.

`build_server` creates the MCPServer instance and the Trino client, registers the
generic tools, then applies any extra tool registrars a product wants to add.
Registrars add tools; extensions (below) add the rest, never in place of the core.

A registrar is ``(mcp, client)`` or ``(mcp, client, tools)``. Either way, every
tool it adds through ``mcp.tool()`` or ``mcp.add_tool()`` is audited like a
built-in one; the three-argument form also receives the `ToolContext` (audit,
a governed ``run``, describers of ``describe_table``).

Extensions (see `akko_mcp_trino.extensions`) are applied here too: their
identity modes, the audit they wrap, the client tags of the queries, and their
tool registrars, which run before the plugins'.
"""

from __future__ import annotations

import inspect
from typing import Any, Callable, Iterable, Sequence, Tuple

from .config import Config
from .extensions import Extension
from .tools import audited, register_query_tools
from .trino_client import TrinoClient, resolve_identity

# A tool registrar receives (mcp, client) or (mcp, client, tools) and adds its tools.
ToolRegistrar = Callable[..., None]


def _default_mcp_factory(
    name: str,
    instructions: str | None = None,
):  # pragma: no cover - production factory (real MCPServer), proven by running it
    # Lazy import: the SDK server is only needed for real assembly, which keeps
    # build_server testable through an injected mcp_factory.
    from mcp.server.mcpserver import MCPServer

    return MCPServer(name, instructions=instructions)


def instructions_for(config: Config) -> str:
    """What the server tells every MCP host at initialization: the warnings of its
    configuration (see ``Config.warnings``), or nothing."""
    warnings = config.warnings()
    if not warnings:
        return ""
    return "WARNING, set by the administrator of this server: " + "; ".join(warnings) + "."


class _AuditedServer:
    """The MCP server as a tool plugin sees it: every tool it adds is audited, and due
    within the same time budget as a built-in one."""

    def __init__(self, mcp: Any, audit: Any, budget_seconds: float) -> None:
        self._mcp, self._audit, self._budget = mcp, audit, budget_seconds

    def tool(self, *args: Any, **kwargs: Any) -> Callable[[Callable], Callable]:
        """The server's ``tool`` decorator, with the audit wrapped around the function."""
        register = self._mcp.tool(*args, **kwargs)
        name = kwargs.get("name") or (args[0] if args else None)

        def deco(fn: Callable) -> Callable:
            return register(audited(self._audit, fn, name=name, budget_seconds=self._budget))

        return deco

    def add_tool(self, fn: Callable, *args: Any, **kwargs: Any) -> Any:
        """The server's ``add_tool``, with the audit wrapped around the function."""
        name = kwargs.get("name") or (args[0] if args else None)
        wrapped = audited(self._audit, fn, name=name, budget_seconds=self._budget)
        return self._mcp.add_tool(wrapped, *args, **kwargs)

    def __getattr__(self, attr: str) -> Any:
        return getattr(self._mcp, attr)


def _takes_tools(registrar: Any) -> bool:
    """True when the registrar accepts a third positional argument, the ToolContext."""
    try:
        inspect.signature(registrar).bind(None, None, None)
    except (TypeError, ValueError):
        return False
    return True


def build_server(
    config: Config,
    *,
    extra_tool_registrars: Iterable[ToolRegistrar] = (),
    mcp_factory: Callable[..., Any] = _default_mcp_factory,
    metrics: Any = None,
    audit: Any = None,
    extensions: Sequence[Extension] = (),
) -> Tuple[Any, TrinoClient]:
    """Assemble the server. `mcp_factory` is injectable so assembly can be tested
    without depending on an SDK version. `metrics`, when given, instruments the client;
    `audit`, when given, receives one record per tool call, a plugin's included, through
    whatever the extensions wrap around it; `extensions` add identity modes, client tags
    and tools (see `akko_mcp_trino.extensions`). An identity mode that cannot serve its
    settings refuses to start, and so does a setting no server can serve
    (``Config.problems``). The warnings of the configuration (``Config.warnings``) go to
    every host in the instructions of the initialization: ``mcp_factory`` then
    receives them as ``instructions``."""
    problems = config.problems()
    if problems:
        raise ValueError("; ".join(problems))
    instructions = instructions_for(config)
    mcp = (
        mcp_factory(config.server_name, instructions=instructions)
        if instructions
        else mcp_factory(config.server_name)
    )
    exts = tuple(extensions)

    def tags() -> list[str]:
        return [tag for ext in exts for tag in ext.client_tags()]

    client = TrinoClient(
        config,
        metrics=metrics,
        identity=resolve_identity(config, exts),
        tags=tags if exts else None,
    )
    if audit is not None:
        for ext in exts:
            audit = ext.wrap_audit(audit)
    tools = register_query_tools(
        mcp,
        client,
        read_only=config.read_only,
        audit=audit,
        call_timeout_seconds=config.call_timeout_seconds,
        session_catalog=config.trino_catalog,
        describe_notes=[ext.describe_table_doc() for ext in exts],
        max_rows=config.max_rows,
        max_catalogs=config.max_catalogs,
        describe_schema_tables=config.describe_schema_tables,
        describe_schema_columns=config.describe_schema_columns,
    )
    plugin_view = _AuditedServer(mcp, audit, config.call_timeout_seconds)
    registrars = [r for ext in exts for r in ext.tool_registrars()]
    for registrar in [*registrars, *extra_tool_registrars]:
        if _takes_tools(registrar):
            registrar(plugin_view, client, tools)
        else:
            registrar(plugin_view, client)
    return mcp, client
