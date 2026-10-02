"""Server configuration, read from the environment.

Nothing product-specific is hardcoded: the defaults here are neutral, and a
product built on this core injects its own values through the environment.
An extension reads its own settings from the environment it is given (see
`akko_mcp_trino.extensions`); this class holds the core's only.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    """Every setting the server reads, with neutral defaults; built from the environment by
    ``from_env``.
    """

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
    # Host header filter (the SDK's DNS rebinding protection). Empty, the
    # default, serves any Host: this is a network service bound on 0.0.0.0
    # behind the identity guard, reached as a service name in a cluster and
    # as a public name on the internet. A comma-separated list turns the
    # filter on for exactly those hosts (`host` or `host:port`; `host:*` for
    # any port is the only wildcard). Once on, a request that carries an
    # `Origin` header is refused 403: no origin is allowed.
    allowed_hosts: str = ""
    # stdio only: the user's access token, verified like a bearer header would
    # be. A local host (Claude Desktop, Cursor, Vibe) launches the server for
    # one person; that token names the verified caller of the whole process.
    user_token: str = ""
    # How the server reaches Trino. `service` (the only mode of the core): every
    # query runs as the one identity the administrator configured, `TRINO_USER`,
    # with `TRINO_PASSWORD` over https when it is set (nothing, for a local Trino).
    # An extension (`MCP_EXTENSIONS`) may add other modes; an unknown mode refuses
    # to start.
    trino_http_scheme: str = "http"
    trino_verify: str = "true"  # true, false, or a path to a CA bundle
    trino_request_timeout: float = 30.0
    # Name the server gives Trino as the query source (X-Trino-Source); empty =
    # the server name, so an operator tells this server's queries apart in Trino's log.
    trino_source: str = ""
    # Time budget of one query, in seconds, enforced by this server: past it the
    # query is cancelled in Trino. 0 leaves the limit to Trino's own settings.
    query_timeout_seconds: float = 0.0
    trino_identity_mode: str = "service"
    # The configured identity's password (Basic authentication, https only); empty sends none.
    trino_password: str = ""
    # Time budget of one tool call, every Trino query of the call included: past
    # it no query starts, the one running is cancelled, and the agent gets an
    # error, never a partial answer. 0 sets none.
    call_timeout_seconds: float = 0.0
    # Tools another package adds (entry point group `akko_mcp_trino.tools`),
    # by plugin name, comma-separated. Empty serves the generic tools only.
    tool_plugins: str = ""
    # Extensions another package adds (entry point group `akko_mcp_trino.extensions`),
    # by name, comma-separated: identity modes, request guards, audit, routes and
    # tools, all at once (see `akko_mcp_trino.extensions`). Empty loads none.
    extensions: str = ""
    # Tolerance on exp/nbf/iat for clock drift between the issuer and this server.
    jwt_leeway_seconds: int = 30

    @staticmethod
    def from_env(*, server_name: str = "trino-mcp") -> "Config":
        """Read every setting from the environment."""
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
            transport=os.environ.get("MCP_TRANSPORT", "streamable-http"),
            mcp_port=int(os.environ.get("MCP_PORT", "3000")),
            auth_required=os.environ.get("MCP_AUTH_REQUIRED", "false").lower() == "true",
            allowed_hosts=os.environ.get("MCP_ALLOWED_HOSTS", ""),
            user_token=os.environ.get("MCP_USER_TOKEN", ""),
            trino_http_scheme=os.environ.get("TRINO_HTTP_SCHEME", "http").lower(),
            trino_verify=os.environ.get("TRINO_VERIFY", "true"),
            trino_request_timeout=float(os.environ.get("TRINO_REQUEST_TIMEOUT_SECONDS", "30")),
            trino_source=os.environ.get("TRINO_SOURCE", ""),
            query_timeout_seconds=float(os.environ.get("TRINO_QUERY_TIMEOUT_SECONDS", "0")),
            trino_identity_mode=os.environ.get("TRINO_IDENTITY_MODE", "service").lower(),
            trino_password=os.environ.get("TRINO_PASSWORD", ""),
            call_timeout_seconds=float(os.environ.get("MCP_CALL_TIMEOUT_SECONDS", "0")),
            tool_plugins=os.environ.get("MCP_TOOL_PLUGINS", ""),
            extensions=os.environ.get("MCP_EXTENSIONS", ""),
            jwt_leeway_seconds=int(os.environ.get("MCP_JWT_LEEWAY_SECONDS", "30")),
        )
