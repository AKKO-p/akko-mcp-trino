"""Tool plugins: tools another package adds, chosen by name at start.

The generic server ships the read-only Trino tools. A product built on it
(one adds its AI functions, another its own tools) registers a
registrar ``(mcp, client) -> None`` or ``(mcp, client, tools) -> None`` under
the entry point group ``akko_mcp_trino.tools`` and names it in
``MCP_TOOL_PLUGINS``. Nothing is loaded that is not named, and an unknown name
refuses to start: a typo must not silently serve fewer tools than the operator
believes. Every tool a registrar adds is audited like a built-in one; the
three-argument form also receives the ``ToolContext`` (see
``akko_mcp_trino.tools``).
"""

from __future__ import annotations

from importlib.metadata import entry_points
from typing import Any, Callable, Mapping, Sequence

ENTRY_POINT_GROUP = "akko_mcp_trino.tools"

Registrar = Callable[..., None]


def _entry_points() -> Sequence[Any]:
    """The registrars other packages register under ``akko_mcp_trino.tools``."""
    return list(entry_points(group=ENTRY_POINT_GROUP))


def tool_registrars(env: Mapping[str, str]) -> list[Registrar]:
    """The registrars named by ``MCP_TOOL_PLUGINS`` (comma-separated), in that order."""
    names = [n.strip() for n in env.get("MCP_TOOL_PLUGINS", "").split(",") if n.strip()]
    if not names:
        return []
    installed = {ep.name: ep for ep in _entry_points()}
    registrars: list[Registrar] = []
    for name in names:
        if name not in installed:
            raise ValueError(
                f"unknown tool plugin {name!r}; installed plugins: {sorted(installed) or 'none'}"
            )
        registrars.append(installed[name].load())
    return registrars
