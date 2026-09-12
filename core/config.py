"""Server configuration, read from the environment.

Nothing product-specific is hardcoded: the defaults here are neutral, and a
product built on this core injects its own values through the environment.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    trino_host: str
    trino_port: int
    trino_user: str
    trino_catalog: str
    max_rows: int
    read_only: bool
    auth_enabled: bool
    server_name: str
    health_port: int
    jwks_url: str
    oidc_issuer: str
    oidc_audience: str
    transport: str
    mcp_port: int
    auth_required: bool

    @staticmethod
    def from_env(*, server_name: str = "trino-mcp") -> "Config":
        return Config(
            trino_host=os.environ.get("TRINO_HOST", "localhost"),
            trino_port=int(os.environ.get("TRINO_PORT", "8080")),
            trino_user=os.environ.get("TRINO_USER", "trino"),
            trino_catalog=os.environ.get("TRINO_CATALOG", "system"),
            max_rows=int(os.environ.get("TRINO_MAX_ROWS", "100")),
            read_only=os.environ.get("TRINO_READ_ONLY", "true").lower() == "true",
            auth_enabled=os.environ.get("MCP_AUTH_ENABLED", "false").lower() == "true",
            server_name=os.environ.get("MCP_SERVER_NAME", server_name),
            health_port=int(os.environ.get("MCP_HEALTH_PORT", "3001")),
            jwks_url=os.environ.get("MCP_JWKS_URL", ""),
            oidc_issuer=os.environ.get("MCP_OIDC_ISSUER", ""),
            oidc_audience=os.environ.get("MCP_OIDC_AUDIENCE", ""),
            transport=os.environ.get("MCP_TRANSPORT", "sse"),
            mcp_port=int(os.environ.get("MCP_PORT", "3000")),
            auth_required=os.environ.get("MCP_AUTH_REQUIRED", "false").lower() == "true",
        )
