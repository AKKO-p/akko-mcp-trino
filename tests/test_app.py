"""The runnable entrypoint: the identity guard sits on whichever transport is served.

This is the defect class the project exists to make impossible. The middleware
once lived only on the ``streamable-http`` branch while the default transport
was ``sse``; every call on the served transport then ran under the service
account. The guard must be mounted by the same code path for both, and a test
must say so for each.
"""

from __future__ import annotations

import contextvars
import dataclasses
from dataclasses import replace
from unittest.mock import Mock

import pytest
from starlette.applications import Starlette

from akko_mcp_trino.app import build_asgi_app, run_stdio, transports_supported
from akko_mcp_trino.audit import InMemoryAudit
from akko_mcp_trino.auth import Principal
from akko_mcp_trino.config import Config
from akko_mcp_trino.middleware import AuthIdentityMiddleware
from akko_mcp_trino.tools import audited


def _config(transport: str) -> Config:
    return Config(
        trino_host="h",
        trino_identity_mode="service",
        query_timeout_seconds=0.0,
        call_timeout_seconds=0.0,
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
    """Stands in for MCPServer: returns a bare ASGI app per transport and records which."""

    def __init__(self):
        self.served = None
        self.security = "unset"

    def sse_app(self, *, transport_security=None):
        self.served = "sse"
        self.security = transport_security
        return Starlette()

    def streamable_http_app(self, *, transport_security=None):
        self.served = "streamable-http"
        self.security = transport_security
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


# ---- stdio: a local host launches the server; the user's token comes from the environment


class _FakeStdioMCP:
    def __init__(self):
        self.ran_with = None
        self.subject_seen = "unset"

    def run(self, transport):
        from akko_mcp_trino.identity import current_bearer, current_subject

        self.ran_with = transport
        self.subject_seen = current_subject()
        self.bearer_seen = current_bearer()


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
    assert mcp.subject_seen == "alice", "the tools would not see the verified caller"


def test_stdio_binds_the_token_as_the_bearer_only_once_verified():
    """Over stdio the verified ``MCP_USER_TOKEN`` is the bearer the ``jwt`` mode sends;
    a token the provider refused is never bound."""
    from akko_mcp_trino.app import run_stdio
    from akko_mcp_trino.auth import Principal
    from akko_mcp_trino.identity import set_current_bearer, set_current_principal

    cfg = Config(**{**_config("stdio").__dict__, "user_token": "tok"})
    try:
        verified = _FakeStdioMCP()
        run_stdio(cfg, verified, auth_provider=_Auth(Principal(subject="alice")))
        refused = _FakeStdioMCP()
        run_stdio(cfg, refused, auth_provider=_Auth(None))
    finally:
        set_current_bearer(None)
        set_current_principal(None)
    assert verified.bearer_seen == "tok" and refused.bearer_seen is None


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


@pytest.mark.parametrize("token, required", [("", False), ("valid", True), ("invalid", False)])
def test_stdio_uses_trinos_identity_for_admission_and_audit(token, required):
    mcp, audit = _FakeStdioMCP(), InMemoryAudit()
    mcp.run = audited(audit, mcp.run)
    client = Mock(query=Mock(return_value={"rows": [["trino-alice"]]}))
    guard = Mock(before_auth=Mock(return_value=None), after_auth=Mock(return_value=None))
    contextvars.copy_context().run(
        run_stdio,
        replace(
            _config("stdio"),
            trino_identity_mode="oauth2",
            trino_http_scheme="https",
            auth_enabled=required,
            auth_required=required,
            user_token=token,
        ),
        mcp,
        auth_provider=_Auth(Principal("mcp-bob") if token == "valid" else None),
        client=client,
        extensions=[guard],
    )
    client.query.assert_called_once_with("SELECT current_user")
    assert mcp.ran_with == "stdio"
    assert mcp.subject_seen == audit.events[0].subject == "trino-alice"
    assert mcp.bearer_seen == ("valid" if required else None)
    assert guard.after_auth.call_args.args[0].principal.subject == "trino-alice"


@pytest.mark.parametrize("rows", [[], [[]], [[None]], [[42]], [[""]], [[" "]]])
def test_stdio_refuses_an_empty_or_invalid_trino_identity(rows):
    cfg = replace(_config("stdio"), trino_identity_mode="oauth2", trino_http_scheme="https")
    mcp, client = Mock(), Mock(query=Mock(return_value={"rows": rows}))
    with pytest.raises(PermissionError, match="authenticated user"):
        run_stdio(cfg, mcp, auth_provider=None, client=client)
    mcp.run.assert_not_called()


@pytest.mark.parametrize(
    "client, required, error",
    [
        (None, False, ValueError),
        (Mock(query=Mock(side_effect=RuntimeError)), False, RuntimeError),
        (Mock(), True, PermissionError),
    ],
)
def test_stdio_refuses_missing_client_failed_login_or_missing_required_token(
    client, required, error
):
    cfg = replace(
        _config("stdio"),
        trino_identity_mode="oauth2",
        trino_http_scheme="https",
        auth_required=required,
    )
    mcp = Mock()
    with pytest.raises(error):
        run_stdio(cfg, mcp, auth_provider=None, client=client)
    mcp.run.assert_not_called()


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


_JWT_ENV = {
    "TRINO_HOST": "trino.example",
    "TRINO_HTTP_SCHEME": "https",
    "MCP_JWKS_URL": "https://idp/certs",
    "MCP_OIDC_ISSUER": "https://idp",
    "MCP_OIDC_AUDIENCE": "mcp-server",
    "TRINO_TOKEN_EXCHANGE_URL": "https://idp/token",
    "TRINO_TOKEN_EXCHANGE_CLIENT_ID": "mcp-server",
    "TRINO_TOKEN_EXCHANGE_CLIENT_SECRET": "exchange-secret",
    "TRINO_TOKEN_EXCHANGE_AUDIENCE": "trino",
}


def _clean_identity_env(monkeypatch):
    for name in (
        "TRINO_IDENTITY_MODE",
        "MCP_AUTH_ENABLED",
        "MCP_AUTH_REQUIRED",
        "MCP_TRANSPORT",
        "MCP_EXTENSIONS",
        "MCP_TOOL_PLUGINS",
        "MCP_RESOURCE_URL",
        "TRINO_PASSWORD",
    ):
        monkeypatch.delenv(name, raising=False)


def test_cli_check_reports_effective_config_without_secrets(monkeypatch, capsys):
    from akko_mcp_trino.__main__ import check_config

    _clean_identity_env(monkeypatch)
    for name, value in _JWT_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("MCP_USER_TOKEN", "tok-secret")
    from akko_mcp_trino.config import Config

    code = check_config(Config.from_env())
    out = capsys.readouterr().out
    assert code == 0, out
    assert "trino.example" in out and "https://idp/certs" in out
    assert "token exchange: https://idp/token as client mcp-server (secret set)" in out
    assert "tok-secret" not in out and "exchange-secret" not in out, "a secret leaked"
    assert "extensions: none" in out and "WARNING" not in out


def test_cli_check_of_the_defaults_names_what_the_safe_mode_needs(monkeypatch, capsys):
    """Unconfigured, the server would not start: the default mode needs an identity
    provider to verify callers and to exchange their tokens, and --check names it."""
    from akko_mcp_trino.__main__ import check_config
    from akko_mcp_trino.config import Config

    _clean_identity_env(monkeypatch)
    for name in _JWT_ENV:
        monkeypatch.delenv(name, raising=False)
    assert check_config(Config.from_env()) == 2
    out = capsys.readouterr().out
    assert "MCP_JWKS_URL" in out and "TRINO_IDENTITY_MODE=jwt requires TRINO_HTTP_SCHEME" in out


def test_cli_check_warns_of_service_mode_chosen_explicitly(monkeypatch, capsys):
    from akko_mcp_trino.__main__ import check_config
    from akko_mcp_trino.config import Config

    _clean_identity_env(monkeypatch)
    monkeypatch.setenv("TRINO_IDENTITY_MODE", "service")
    monkeypatch.setenv("MCP_AUTH_ENABLED", "false")
    monkeypatch.setenv("MCP_AUTH_REQUIRED", "false")
    assert check_config(Config.from_env()) == 0
    out = capsys.readouterr().out
    assert "WARNING: TRINO_IDENTITY_MODE=service: every query runs as the Trino user" in out
    assert "WARNING: MCP_AUTH_ENABLED=false" in out


def test_cli_check_reports_settings_no_server_can_serve(monkeypatch, capsys):
    from akko_mcp_trino.__main__ import check_config
    from akko_mcp_trino.config import Config

    _clean_identity_env(monkeypatch)
    monkeypatch.setenv("TRINO_IDENTITY_MODE", "service")
    monkeypatch.setenv("MCP_AUTH_ENABLED", "false")
    monkeypatch.setenv("MCP_AUTH_REQUIRED", "false")
    monkeypatch.setenv("TRINO_MAX_ROWS", "0")
    assert check_config(Config.from_env()) == 2
    assert "PROBLEM: TRINO_MAX_ROWS=0" in capsys.readouterr().out


def test_cli_check_fails_on_an_invalid_configuration(monkeypatch, capsys):
    from akko_mcp_trino.__main__ import check_config
    from akko_mcp_trino.config import Config

    monkeypatch.setenv("MCP_AUTH_ENABLED", "true")
    monkeypatch.delenv("MCP_JWKS_URL", raising=False)
    assert check_config(Config.from_env()) == 2
    assert "MCP_JWKS_URL" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("env", "named"),
    [
        ({"TRINO_IDENTITY_MODE": "service-account"}, "TRINO_IDENTITY_MODE"),
        ({"MCP_EXTENSIONS": "no-such-extension"}, "no-such-extension"),
        ({"MCP_TRANSPORT": "stdio", "MCP_AUTH_REQUIRED": "true"}, "MCP_USER_TOKEN"),
        ({"MCP_TRANSPORT": "websocket"}, "MCP_TRANSPORT"),
    ],
)
def test_cli_check_fails_where_the_server_would_refuse_to_start(monkeypatch, capsys, env, named):
    """--check says 0 only when the server would start: the identity mode, the
    extensions, the stdio token and the transport are checked there too, not first
    at startup."""
    from akko_mcp_trino.__main__ import check_config
    from akko_mcp_trino.config import Config

    for name in ("MCP_USER_TOKEN", "TRINO_IDENTITY_MODE", "MCP_EXTENSIONS", "MCP_TRANSPORT"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert check_config(Config.from_env()) == 2
    assert named in capsys.readouterr().out


# ---- Host header: the SDK's DNS rebinding protection is for localhost servers


def test_network_server_does_not_filter_the_host_header_by_default():
    """The SDK enables DNS rebinding protection on its own when it believes it
    serves localhost, and then answers 421 to any other Host: a pod reached as
    `akko-mcp-trino.akko.svc` would refuse every call. This server binds
    0.0.0.0 behind the identity guard, so the Host filter is off unless asked."""
    for transport in ("sse", "streamable-http"):
        mcp = _FakeMCP()
        build_asgi_app(_config(transport), mcp, auth_provider=object())
        assert mcp.security is not None, "transport_security left to the SDK's localhost default"
        assert mcp.security.enable_dns_rebinding_protection is False


def test_allowed_hosts_turn_the_host_filter_on_with_exactly_those_hosts():
    mcp = _FakeMCP()
    cfg = dataclasses.replace(
        _config("streamable-http"), allowed_hosts="mcp.example.org, mcp.example.org:3000"
    )
    build_asgi_app(cfg, mcp, auth_provider=object())
    assert mcp.security.enable_dns_rebinding_protection is True
    assert mcp.security.allowed_hosts == ["mcp.example.org", "mcp.example.org:3000"]


def test_cli_check_reports_tool_plugins_and_refuses_an_unknown_one(monkeypatch, capsys):
    from akko_mcp_trino.__main__ import check_config
    from akko_mcp_trino.config import Config

    monkeypatch.setenv("MCP_TOOL_PLUGINS", "nope")
    assert check_config(Config.from_env()) == 2
    out = capsys.readouterr().out
    assert "tool plugins: nope" in out and "unknown tool plugin 'nope'" in out


def test_cli_check_reports_the_page_of_a_listing_and_the_catalog_bound(monkeypatch, capsys):
    from akko_mcp_trino.__main__ import check_config
    from akko_mcp_trino.config import Config

    monkeypatch.setenv("TRINO_MAX_ROWS", "40")
    monkeypatch.setenv("MCP_MAX_CATALOGS", "0")
    assert check_config(Config.from_env()) == 2
    out = capsys.readouterr().out
    assert "listings: 40 names a page; list_catalogs reads up to 0 catalogs" in out
    assert "MCP_MAX_CATALOGS=0: list_catalogs must be allowed 1 catalog or more" in out


def test_cli_check_reports_the_describe_schema_page(monkeypatch, capsys):
    from akko_mcp_trino.__main__ import check_config
    from akko_mcp_trino.config import Config

    monkeypatch.setenv("MCP_DESCRIBE_SCHEMA_TABLES", "8")
    monkeypatch.setenv("MCP_DESCRIBE_SCHEMA_COLUMNS", "0")
    assert check_config(Config.from_env()) == 2
    out = capsys.readouterr().out
    assert "describe_schema: up to 8 tables a page, 0 columns in all, a wider table whole" in out
    assert "MCP_DESCRIBE_SCHEMA_COLUMNS=0: describe_schema must be allowed 1 column" in out


def test_cli_check_reports_the_query_source_and_budget(monkeypatch, capsys):
    from akko_mcp_trino.__main__ import check_config
    from akko_mcp_trino.config import Config

    monkeypatch.delenv("TRINO_SOURCE", raising=False)
    monkeypatch.delenv("TRINO_QUERY_TIMEOUT_SECONDS", raising=False)
    monkeypatch.setenv("MCP_SERVER_NAME", "agents")
    monkeypatch.delenv("MCP_CALL_TIMEOUT_SECONDS", raising=False)
    check_config(Config.from_env())
    out = capsys.readouterr().out
    assert "trino queries: source agents, time budget: 60 s" in out
    assert "tool call time budget: 120 s, every query of the call included" in out
    monkeypatch.setenv("TRINO_QUERY_TIMEOUT_SECONDS", "0")
    monkeypatch.setenv("MCP_CALL_TIMEOUT_SECONDS", "0")
    check_config(Config.from_env())
    out = capsys.readouterr().out
    assert "time budget: Trino's" in out and "tool call time budget: none" in out
    monkeypatch.setenv("TRINO_SOURCE", "gateway")
    monkeypatch.setenv("TRINO_QUERY_TIMEOUT_SECONDS", "45")
    monkeypatch.setenv("MCP_CALL_TIMEOUT_SECONDS", "120")
    check_config(Config.from_env())
    out = capsys.readouterr().out
    assert "source gateway, time budget: 45 s" in out
    assert "tool call time budget: 120 s, every query of the call included" in out


def test_a_server_that_names_its_resource_publishes_its_metadata_on_every_transport():
    """MCP_RESOURCE_URL names this server (RFC 8707): the served app publishes its
    protected resource metadata (RFC 9728) and a 401 points to it."""
    from starlette.testclient import TestClient

    for transport in ("sse", "streamable-http"):
        cfg = dataclasses.replace(
            _config(transport),
            auth_enabled=True,
            auth_required=True,
            oidc_issuer="https://idp/realms/r",
            resource_url="https://mcp.example.com/mcp",
        )
        client = TestClient(build_asgi_app(cfg, _FakeMCP(), auth_provider=_Nobody()))
        meta = client.get("/.well-known/oauth-protected-resource/mcp").json()
        assert meta["resource"] == "https://mcp.example.com/mcp"
        assert meta["authorization_servers"] == ["https://idp/realms/r"]
        assert client.post("/mcp").headers["www-authenticate"] == (
            'Bearer resource_metadata="https://mcp.example.com/.well-known/'
            'oauth-protected-resource/mcp"'
        )


class _Nobody:
    def verify(self, headers):
        return None
