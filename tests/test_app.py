"""The runnable entrypoint: the identity guard sits on whichever transport is served.

This is the defect class the project exists to make impossible. The middleware
once lived only on the ``streamable-http`` branch while the default transport
was ``sse``; every call on the served transport then ran under the service
account. The guard must be mounted by the same code path for both, and a test
must say so for each.
"""

from __future__ import annotations

from starlette.applications import Starlette

from akko_mcp_trino.app import build_asgi_app, transports_supported
from akko_mcp_trino.config import Config
from akko_mcp_trino.middleware import AuthIdentityMiddleware


def _config(transport: str) -> Config:
    return Config(
        trino_host="h",
        trino_port=8080,
        trino_user="u",
        trino_catalog="c",
        max_rows=100,
        read_only=True,
        auth_enabled=False,
        server_name="trino-mcp",
        health_port=3001,
        jwks_url="",
        oidc_issuer="",
        oidc_audience="",
        transport=transport,
        mcp_port=3000,
        auth_required=False,
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


def test_both_http_transports_are_guarded():
    for transport in ("sse", "streamable-http"):
        mcp = _FakeMCP()
        app = build_asgi_app(_config(transport), mcp, auth_provider=object())
        assert mcp.served == transport, "the wrong transport app was built"
        assert _has_guard(app), f"transport {transport!r} is served without the identity guard"


def test_supported_transports_are_the_two_http_ones_and_stdio():
    assert set(transports_supported()) == {"sse", "streamable-http", "stdio"}


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
    """``python -m akko_mcp_trino`` must be importable without starting anything.

    Importing the module proves every name the glue refers to exists; it must
    not open a port or touch Trino, which is why ``main`` is only called under
    ``__main__``.
    """
    import importlib

    module = importlib.import_module("akko_mcp_trino.__main__")
    assert callable(module.main)


def test_agent_registry_and_discovery_are_wired_from_config():
    """The served app enforces X-Agent-Key and serves RFC 9728 when configured."""
    cfg = Config(
        **{
            **_config("sse").__dict__,
            "auth_required": True,
            "resource_url": "https://mcp.example.com",
            "agent_keys": "cursor:k1",
            "oidc_issuer": "https://idp",
        }
    )
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
    cfg = Config(
        **{
            **_config("sse").__dict__,
            "rate_limit_user": 5,
            "rate_limit_agent": 50,
            "rate_limit_window_seconds": 30,
        }
    )
    app = build_asgi_app(cfg, _FakeMCP(), auth_provider=object())
    layer = next(m for m in app.user_middleware if m.cls is AuthIdentityMiddleware)
    lim = layer.kwargs["limiter"]
    assert lim.enabled and (lim.user.limit, lim.agent.limit, lim.user.seconds) == (5, 50, 30)


def test_revocation_check_is_wired_from_config():
    cfg = Config(
        **{
            **_config("sse").__dict__,
            "introspection_url": "https://idp/introspect",
            "introspection_client_id": "mcp",
            "introspection_client_secret": "s",
        }
    )
    app = build_asgi_app(cfg, _FakeMCP(), auth_provider=object())
    layer = next(m for m in app.user_middleware if m.cls is AuthIdentityMiddleware)
    assert layer.kwargs["revocation"] is not None


def test_revocation_check_is_absent_when_not_configured():
    app = build_asgi_app(_config("sse"), _FakeMCP(), auth_provider=object())
    layer = next(m for m in app.user_middleware if m.cls is AuthIdentityMiddleware)
    assert layer.kwargs["revocation"] is None


# ---- stdio: a local host launches the server; the user's token comes from the environment


class _FakeStdioMCP:
    def __init__(self):
        self.ran_with = None
        self.subject_seen = "unset"

    def run(self, transport):
        from akko_mcp_trino.identity import current_subject

        self.ran_with = transport
        self.subject_seen = current_subject()


class _Auth:
    def __init__(self, principal):
        self._p, self.seen = principal, None

    def verify(self, headers):
        self.seen = headers
        return self._p


def test_stdio_is_a_supported_transport():
    assert "stdio" in transports_supported()


def test_stdio_runs_fastmcp_over_stdio_with_the_token_from_the_environment():
    from akko_mcp_trino.app import run_stdio
    from akko_mcp_trino.auth import Principal

    cfg = Config(**{**_config("stdio").__dict__, "auth_required": True, "user_token": "tok"})
    mcp, provider = _FakeStdioMCP(), _Auth(Principal(subject="alice"))
    run_stdio(cfg, mcp, auth_provider=provider)
    assert mcp.ran_with == "stdio"
    assert provider.seen == {"authorization": "Bearer tok"}
    assert mcp.subject_seen == "alice", "the tools would not run under the user's identity"


def test_stdio_refuses_to_start_without_a_verified_identity_in_strict_mode():
    import pytest

    from akko_mcp_trino.app import run_stdio

    cfg = Config(**{**_config("stdio").__dict__, "auth_required": True, "user_token": "bad"})
    with pytest.raises(PermissionError):
        run_stdio(cfg, _FakeStdioMCP(), auth_provider=_Auth(None))


def test_stdio_without_auth_runs_as_the_service_account():
    from akko_mcp_trino.app import run_stdio

    mcp = _FakeStdioMCP()
    run_stdio(_config("stdio"), mcp, auth_provider=None)
    assert mcp.ran_with == "stdio" and mcp.subject_seen is None


def test_build_asgi_app_refuses_stdio():
    """stdio has no ASGI app; asking for one is a wiring error, not a fallback."""
    import pytest

    with pytest.raises(ValueError):
        build_asgi_app(_config("stdio"), _FakeMCP(), auth_provider=None)


# ---- command line: --version and --check must not start a server


def test_cli_version_prints_and_exits(capsys):
    import pytest

    from akko_mcp_trino.__main__ import parse_args

    with pytest.raises(SystemExit) as e:
        parse_args(["--version"])
    assert e.value.code == 0
    out = capsys.readouterr().out
    from akko_mcp_trino import __version__

    assert __version__ in out


def test_cli_check_reports_effective_config_without_secrets(monkeypatch, capsys):
    from akko_mcp_trino.__main__ import check_config

    monkeypatch.setenv("TRINO_HOST", "trino.example")
    monkeypatch.setenv("MCP_AUTH_ENABLED", "true")
    monkeypatch.setenv("MCP_JWKS_URL", "https://idp/certs")
    monkeypatch.setenv("MCP_INTROSPECTION_CLIENT_SECRET", "s3cret")
    monkeypatch.setenv("MCP_USER_TOKEN", "tok-secret")
    monkeypatch.setenv("MCP_AGENT_KEYS", "cursor:k-secret")
    from akko_mcp_trino.config import Config

    code = check_config(Config.from_env())
    out = capsys.readouterr().out
    assert code == 0
    assert "trino.example" in out and "https://idp/certs" in out
    for secret in ("s3cret", "tok-secret", "k-secret"):
        assert secret not in out, "a secret leaked into --check output"
    assert "agent products: 1" in out


def test_cli_check_fails_on_an_invalid_configuration(monkeypatch, capsys):
    from akko_mcp_trino.__main__ import check_config
    from akko_mcp_trino.config import Config

    monkeypatch.setenv("MCP_AUTH_ENABLED", "true")
    monkeypatch.delenv("MCP_JWKS_URL", raising=False)
    assert check_config(Config.from_env()) == 2
    assert "MCP_JWKS_URL" in capsys.readouterr().out


def test_cli_check_reports_malformed_agent_keys_and_introspection(monkeypatch, capsys):
    from akko_mcp_trino.__main__ import check_config
    from akko_mcp_trino.config import Config

    monkeypatch.setenv("MCP_AGENT_KEYS", "no-colon")
    monkeypatch.setenv("MCP_INTROSPECTION_URL", "https://idp/introspect")
    monkeypatch.delenv("MCP_INTROSPECTION_CLIENT_ID", raising=False)
    assert check_config(Config.from_env()) == 2
    out = capsys.readouterr().out
    assert "MCP_AGENT_KEYS" in out and "MCP_INTROSPECTION" in out


def test_cli_check_reports_a_missing_context_file(monkeypatch, capsys):
    from akko_mcp_trino.__main__ import check_config
    from akko_mcp_trino.config import Config

    monkeypatch.setenv("MCP_CONTEXT_PROVIDERS", "file")
    monkeypatch.setenv("MCP_CONTEXT_FILE", "/nowhere/ctx.json")
    assert check_config(Config.from_env()) == 2
    assert "ctx.json" in capsys.readouterr().out


def test_cli_check_hands_the_whole_environment_to_context_plugins(monkeypatch, capsys):
    """A plugin reads its own variables (OPENMETADATA_URL…); the launcher must
    pass the process environment, not only the MCP_CONTEXT_* keys. Found live."""
    from akko_mcp_trino import context as ctx
    from akko_mcp_trino.__main__ import check_config
    from akko_mcp_trino.config import Config

    seen = {}

    class _EP:
        name = "acme"

        def load(self):
            def factory(env, query):
                seen.update(env)
                return ctx.NoContext()

            return factory

    monkeypatch.setattr(ctx, "_entry_points", lambda: [_EP()])
    monkeypatch.setenv("MCP_CONTEXT_PROVIDERS", "acme")
    monkeypatch.setenv("ACME_URL", "https://acme")
    assert check_config(Config.from_env()) == 0
    assert seen.get("ACME_URL") == "https://acme" and seen.get("MCP_CONTEXT_PROVIDERS") == "acme"
