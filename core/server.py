"""Assemblage du serveur MCP (cœur générique). Aucune référence AKKO.

`build_server` crée le FastMCP, le client Trino, enregistre les outils génériques,
puis applique les enregistreurs d'outils additionnels (extensions, ex. AKKO ai_*).
"""
from __future__ import annotations

from typing import Any, Callable, Iterable, Tuple

from .config import Config
from .tools import register_query_tools
from .trino_client import TrinoClient

# Un enregistreur d'outils : reçoit (mcp, client) et ajoute ses @mcp.tool().
ToolRegistrar = Callable[[Any, TrinoClient], None]


def _default_mcp_factory(name: str):  # pragma: no cover - factory de prod (FastMCP réel), prouvée au déploiement
    # Import paresseux : FastMCP n'est nécessaire qu'à l'assemblage réel, ce qui
    # garde build_server testable (mcp_factory injectable) sans charger FastMCP.
    from mcp.server.fastmcp import FastMCP
    return FastMCP(name)


def build_server(
    config: Config,
    *,
    extra_tool_registrars: Iterable[ToolRegistrar] = (),
    mcp_factory: Callable[[str], Any] = _default_mcp_factory,
    metrics: Any = None,
) -> Tuple[Any, TrinoClient]:
    """Assemble le serveur. `mcp_factory` est injectable (DI) pour tester l'assemblage
    sans dépendre de la version de FastMCP. `metrics` (optionnel) instrumente le client."""
    mcp = mcp_factory(config.server_name)
    client = TrinoClient(config, metrics=metrics)
    register_query_tools(mcp, client, read_only=config.read_only)
    for registrar in extra_tool_registrars:
        registrar(mcp, client)
    return mcp, client
