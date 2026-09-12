"""The runnable entrypoint: the identity guard sits on whichever transport is served.

This is the defect class the project exists to make impossible. The middleware
once lived only on the ``streamable-http`` branch while the default transport
was ``sse``; every call on the served transport then ran under the service
account. The guard must be mounted by the same code path for both, and a test
must say so for each.
"""
from __future__ import annotations

from starlette.applications import Starlette

from core.app import build_asgi_app, transports_supported
from core.config import Config
from core.middleware import AuthIdentityMiddleware


def _config(transport: str) -> Config:
    return Config(
        trino_host="h", trino_port=8080, trino_user="u", trino_catalog="c",
        max_rows=100, read_only=True, auth_enabled=False, server_name="trino-mcp",
        health_port=3001, jwks_url="", oidc_issuer="", oidc_audience="",
        transport=transport, mcp_port=3000, auth_required=False,
    )


class _FakeMCP:
    """Stands in for FastMCP: returns a bare ASGI app per transport and records which."""

    def __init__(self):
        self.served = None

    def sse_app(self):
        self.served = "sse"
        return Starlette()

    def streamable_http_app(self):
        self.served = "streamable-http"
        return Starlette()


def _has_guard(app) -> bool:
    return any(m.cls is AuthIdentityMiddleware for m in getattr(app, "user_middleware", []))


def test_both_transports_are_guarded():
    for transport in transports_supported():
        mcp = _FakeMCP()
        app = build_asgi_app(_config(transport), mcp, auth_provider=object())
        assert mcp.served == transport, "the wrong transport app was built"
        assert _has_guard(app), f"transport {transport!r} is served without the identity guard"


def test_supported_transports_are_exactly_the_two_fastmcp_offers():
    assert set(transports_supported()) == {"sse", "streamable-http"}


def test_unknown_transport_is_refused_not_defaulted():
    """A typo in configuration must not silently pick a transport."""
    import pytest

    with pytest.raises(ValueError):
        build_asgi_app(_config("stdio"), _FakeMCP(), auth_provider=None)


def test_strictness_follows_config():
    """require_auth on the middleware is the config's auth_required, nothing else."""
    cfg = _config("sse")
    strict = Config(**{**cfg.__dict__, "auth_required": True})
    app = build_asgi_app(strict, _FakeMCP(), auth_provider=object())
    layer = next(m for m in app.user_middleware if m.cls is AuthIdentityMiddleware)
    assert layer.kwargs.get("require_auth") is True


def test_entrypoint_imports_without_side_effects():
    """``python -m core`` must be importable without starting anything.

    Importing the module proves every name the glue refers to exists; it must
    not open a port or touch Trino, which is why ``main`` is only called under
    ``__main__``.
    """
    import importlib

    module = importlib.import_module("core.__main__")
    assert callable(module.main)


def test_agent_registry_and_discovery_are_wired_from_config():
    """The served app enforces X-Agent-Key and serves RFC 9728 when configured."""
    cfg = Config(**{**_config("sse").__dict__, "auth_required": True,
                    "resource_url": "https://mcp.example.com",
                    "agent_keys": "cursor:k1", "oidc_issuer": "https://idp"})
    app = build_asgi_app(cfg, _FakeMCP(), auth_provider=object())
    layer = next(m for m in app.user_middleware if m.cls is AuthIdentityMiddleware)
    assert layer.kwargs["agents"].resolve("k1") == "cursor"
    assert layer.kwargs["discovery"].document()["resource"] == "https://mcp.example.com"


def test_malformed_agent_keys_refuse_to_start():
    import pytest

    cfg = Config(**{**_config("sse").__dict__, "agent_keys": "no-colon"})
    with pytest.raises(ValueError):
        build_asgi_app(cfg, _FakeMCP(), auth_provider=None)


def test_rate_limiter_is_wired_from_config():
    cfg = Config(**{**_config("sse").__dict__, "rate_limit_user": 5, "rate_limit_agent": 50,
                    "rate_limit_window_seconds": 30})
    app = build_asgi_app(cfg, _FakeMCP(), auth_provider=object())
    layer = next(m for m in app.user_middleware if m.cls is AuthIdentityMiddleware)
    lim = layer.kwargs["limiter"]
    assert lim.enabled and (lim.user.limit, lim.agent.limit, lim.user.seconds) == (5, 50, 30)
