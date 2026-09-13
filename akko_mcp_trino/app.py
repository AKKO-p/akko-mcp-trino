"""Assemble the ASGI application that is actually served.

There is exactly one code path that builds the transport app and mounts the
identity guard on it, whatever the transport. That is deliberate.

The guard once lived only on the ``streamable-http`` branch, while the default
transport was ``sse``: the branch that ran in production had no guard at all,
and every call reached Trino under the service account with its full scope. A
control that exists in the code, is tested, and is never reached is worse than
no control, because it looks like one. This module makes that shape impossible:
choosing a transport never decides whether the guard is present.
"""

from __future__ import annotations

from typing import Any

from .agents import AgentRegistry
from .config import Config
from .discovery import ProtectedResource
from .identity import set_current_bearer, set_current_principal
from .middleware import AuthIdentityMiddleware
from .ratelimit import RateLimiter
from .revocation import IntrospectionCheck

_HTTP_TRANSPORTS = ("sse", "streamable-http")
_TRANSPORTS = _HTTP_TRANSPORTS + ("stdio",)


def transports_supported() -> tuple[str, ...]:
    """Every transport the server can serve: the two HTTP ones and stdio."""
    return _TRANSPORTS


def run_stdio(config: Config, mcp: Any, *, auth_provider: Any) -> None:
    """Serve over stdio for a local host, as one user.

    There is no request, hence no header: the identity is ``MCP_USER_TOKEN``,
    verified by the same provider a bearer header would be, and bound to the
    whole process. In strict mode a missing or invalid token refuses to start,
    which is the stdio equivalent of a 401. Nothing here writes to stdout:
    stdout is the protocol channel.
    """
    principal = None
    if auth_provider is not None and config.user_token:
        principal = auth_provider.verify({"authorization": f"Bearer {config.user_token}"})
    if config.auth_required and principal is None:
        raise PermissionError(
            "MCP_AUTH_REQUIRED=true and MCP_USER_TOKEN is missing or invalid: "
            "refusing to serve over stdio without a verified identity"
        )
    set_current_principal(principal)
    set_current_bearer(config.user_token if principal else None)
    mcp.run(transport="stdio")


def build_asgi_app(config: Config, mcp: Any, *, auth_provider: Any) -> Any:
    """Return the transport app for ``config.transport`` with the guard mounted.

    ``mcp`` is anything exposing ``sse_app()`` and ``streamable_http_app()`` —
    a real MCPServer in production, a stand-in under test. An unknown transport
    raises rather than falling back: a typo in configuration must not silently
    pick a transport.
    """
    if config.transport == "sse":
        app = mcp.sse_app()
    elif config.transport == "streamable-http":
        app = mcp.streamable_http_app()
    else:
        raise ValueError(
            f"no ASGI app for MCP_TRANSPORT {config.transport!r}; "
            f"expected one of {_HTTP_TRANSPORTS} (stdio uses run_stdio)"
        )
    # Both are built from the same Config the transport came from: a registry
    # or a discovery document that could not start is refused here, not at
    # the first request.
    agents = AgentRegistry.from_env({"MCP_AGENT_KEYS": config.agent_keys})
    discovery = ProtectedResource.from_config(config)
    limiter = RateLimiter.from_env(
        {
            "MCP_RATE_LIMIT_USER": str(config.rate_limit_user),
            "MCP_RATE_LIMIT_AGENT": str(config.rate_limit_agent),
            "MCP_RATE_LIMIT_WINDOW_SECONDS": str(config.rate_limit_window_seconds),
        }
    )
    revocation = IntrospectionCheck.from_env(
        {
            "MCP_INTROSPECTION_URL": config.introspection_url,
            "MCP_INTROSPECTION_CLIENT_ID": config.introspection_client_id,
            "MCP_INTROSPECTION_CLIENT_SECRET": config.introspection_client_secret,
            "MCP_INTROSPECTION_TTL_SECONDS": str(config.introspection_ttl_seconds),
        }
    )
    app.add_middleware(
        AuthIdentityMiddleware,
        auth_provider=auth_provider,
        require_auth=config.auth_required,
        agents=agents,
        discovery=discovery,
        limiter=limiter,
        revocation=revocation,
    )
    return app
