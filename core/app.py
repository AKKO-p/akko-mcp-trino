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
from .middleware import AuthIdentityMiddleware
from .ratelimit import RateLimiter

_TRANSPORTS = ("sse", "streamable-http")


def transports_supported() -> tuple[str, ...]:
    """The transports FastMCP can serve over HTTP, and nothing else."""
    return _TRANSPORTS


def build_asgi_app(config: Config, mcp: Any, *, auth_provider: Any) -> Any:
    """Return the transport app for ``config.transport`` with the guard mounted.

    ``mcp`` is anything exposing ``sse_app()`` and ``streamable_http_app()`` —
    a real FastMCP in production, a stand-in under test. An unknown transport
    raises rather than falling back: a typo in configuration must not silently
    pick a transport.
    """
    if config.transport == "sse":
        app = mcp.sse_app()
    elif config.transport == "streamable-http":
        app = mcp.streamable_http_app()
    else:
        raise ValueError(
            f"unsupported MCP_TRANSPORT {config.transport!r}; expected one of {_TRANSPORTS}"
        )
    # Both are built from the same Config the transport came from: a registry
    # or a discovery document that could not start is refused here, not at
    # the first request.
    agents = AgentRegistry.from_env({"MCP_AGENT_KEYS": config.agent_keys})
    discovery = ProtectedResource.from_config(config)
    limiter = RateLimiter.from_env({
        "MCP_RATE_LIMIT_USER": str(config.rate_limit_user),
        "MCP_RATE_LIMIT_AGENT": str(config.rate_limit_agent),
        "MCP_RATE_LIMIT_WINDOW_SECONDS": str(config.rate_limit_window_seconds),
    })
    app.add_middleware(
        AuthIdentityMiddleware,
        auth_provider=auth_provider,
        require_auth=config.auth_required,
        agents=agents,
        discovery=discovery,
        limiter=limiter,
    )
    return app
