"""Server configuration, read from the environment.

Nothing product-specific is hardcoded: the defaults here are neutral, and a
product built on this core injects its own values through the environment.
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
    # Public URL of this server as MCP hosts see it (RFC 9728 `resource`).
    # Empty disables discovery: the 401 then carries no `WWW-Authenticate`.
    resource_url: str = ""
    # `name:key` pairs naming the agent products allowed to call. Empty
    # disables the check, as in the CDP Agent Gateway blueprint.
    agent_keys: str = ""
    # Host header filter (the SDK's DNS rebinding protection). Empty, the
    # default, serves any Host: this is a network service bound on 0.0.0.0
    # behind the identity guard, reached as a service name in a cluster and
    # as a public name on the internet. A comma-separated list turns the
    # filter on for exactly those hosts (`host` or `host:port`, `*` wildcards).
    allowed_hosts: str = ""
    # Requests per window per user subject and per agent product; 0 disables.
    rate_limit_user: int = 0
    rate_limit_agent: int = 0
    rate_limit_window_seconds: int = 60
    # RFC 7662 introspection for revocation before expiry; empty URL disables.
    introspection_url: str = ""
    introspection_client_id: str = ""
    introspection_client_secret: str = ""
    introspection_ttl_seconds: int = 30
    # stdio only: the user's access token, verified like a bearer header would
    # be. A local host (Claude Desktop, Cursor, Vibe) launches the server for
    # one person; that person's token is the identity of the whole process.
    user_token: str = ""
    # How the server reaches Trino. `impersonate` (default): connect as
    # TRINO_USER (Basic auth with TRINO_PASSWORD over https when set) and set
    # X-Trino-User to the caller; Trino's impersonation rules must allow it.
    # `jwt`: send the caller's own verified bearer to Trino (JWT authenticator
    # on Trino's side); no impersonation right needed, https required.
    trino_http_scheme: str = "http"
    trino_password: str = ""
    trino_verify: str = "true"  # true, false, or a path to a CA bundle
    trino_request_timeout: float = 30.0
    trino_identity_mode: str = "impersonate"
    # Context providers, comma-separated in priority order: trino-comments, file
    # (with MCP_CONTEXT_FILE), none. A product plugs its own catalogue in code.
    context_providers: str = ""
    context_file: str = ""
    context_ttl_seconds: float = 60.0
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
            resource_url=os.environ.get("MCP_RESOURCE_URL", "").rstrip("/"),
            agent_keys=os.environ.get("MCP_AGENT_KEYS", ""),
            allowed_hosts=os.environ.get("MCP_ALLOWED_HOSTS", ""),
            rate_limit_user=int(os.environ.get("MCP_RATE_LIMIT_USER", "0")),
            rate_limit_agent=int(os.environ.get("MCP_RATE_LIMIT_AGENT", "0")),
            rate_limit_window_seconds=int(os.environ.get("MCP_RATE_LIMIT_WINDOW_SECONDS", "60")),
            introspection_url=os.environ.get("MCP_INTROSPECTION_URL", ""),
            introspection_client_id=os.environ.get("MCP_INTROSPECTION_CLIENT_ID", ""),
            introspection_client_secret=os.environ.get("MCP_INTROSPECTION_CLIENT_SECRET", ""),
            introspection_ttl_seconds=int(os.environ.get("MCP_INTROSPECTION_TTL_SECONDS", "30")),
            user_token=os.environ.get("MCP_USER_TOKEN", ""),
            trino_http_scheme=os.environ.get("TRINO_HTTP_SCHEME", "http").lower(),
            trino_password=os.environ.get("TRINO_PASSWORD", ""),
            trino_verify=os.environ.get("TRINO_VERIFY", "true"),
            trino_request_timeout=float(os.environ.get("TRINO_REQUEST_TIMEOUT_SECONDS", "30")),
            trino_identity_mode=os.environ.get("TRINO_IDENTITY_MODE", "impersonate").lower(),
            context_providers=os.environ.get("MCP_CONTEXT_PROVIDERS", ""),
            context_file=os.environ.get("MCP_CONTEXT_FILE", ""),
            context_ttl_seconds=float(os.environ.get("MCP_CONTEXT_TTL_SECONDS", "60")),
            jwt_leeway_seconds=int(os.environ.get("MCP_JWT_LEEWAY_SECONDS", "30")),
        )
