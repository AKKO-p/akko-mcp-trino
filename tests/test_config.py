"""akko_mcp_trino.config: every setting comes from the environment, with neutral defaults."""

import pytest

from akko_mcp_trino.config import Config

_ENV_KEYS = [
    "TRINO_HOST",
    "TRINO_PORT",
    "TRINO_USER",
    "TRINO_CATALOG",
    "TRINO_MAX_ROWS",
    "TRINO_READ_ONLY",
    "TRINO_PASSWORD",
    "MCP_AUTH_ENABLED",
    "MCP_SERVER_NAME",
    "MCP_HEALTH_PORT",
    "MCP_JWKS_URL",
    "MCP_OIDC_ISSUER",
    "MCP_OIDC_AUDIENCE",
    "MCP_TRANSPORT",
    "MCP_PORT",
    "MCP_AUTH_REQUIRED",
    "MCP_USER_TOKEN",
]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in _ENV_KEYS:
        monkeypatch.delenv(k, raising=False)


def test_defaults_are_neutral():
    c = Config.from_env()
    assert c.trino_host == "localhost"
    assert c.trino_port == 8080
    assert c.trino_user == "trino"
    assert c.trino_catalog == "system"
    assert c.max_rows == 100
    assert c.read_only is True
    assert c.auth_enabled is False
    assert c.server_name == "trino-mcp"
    assert c.health_port == 3001
    assert c.jwks_url == "" and c.oidc_issuer == "" and c.oidc_audience == ""
    # streamable-http is the current MCP standard (Le Chat, Cursor, Claude); strict auth is opt-in
    assert c.transport == "streamable-http" and c.mcp_port == 3000 and c.auth_required is False


def test_oidc_env_overrides(monkeypatch):
    monkeypatch.setenv("MCP_JWKS_URL", "https://kc/jwks")
    monkeypatch.setenv("MCP_OIDC_ISSUER", "https://kc/realms/akko")
    monkeypatch.setenv("MCP_OIDC_AUDIENCE", "akko-mcp")
    c = Config.from_env()
    assert c.jwks_url == "https://kc/jwks"
    assert c.oidc_issuer == "https://kc/realms/akko"
    assert c.oidc_audience == "akko-mcp"


def test_transport_and_auth_required_env(monkeypatch):
    monkeypatch.setenv("MCP_TRANSPORT", "streamable-http")
    monkeypatch.setenv("MCP_PORT", "3000")
    monkeypatch.setenv("MCP_AUTH_REQUIRED", "true")
    c = Config.from_env()
    assert c.transport == "streamable-http" and c.auth_required is True


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("TRINO_HOST", "trino-coordinator")
    monkeypatch.setenv("TRINO_PORT", "9090")
    monkeypatch.setenv("TRINO_USER", "mcp-trino")
    monkeypatch.setenv("TRINO_CATALOG", "iceberg")
    monkeypatch.setenv("TRINO_MAX_ROWS", "10000")
    monkeypatch.setenv("TRINO_READ_ONLY", "false")
    monkeypatch.setenv("MCP_AUTH_ENABLED", "true")
    c = Config.from_env(server_name="trino-coordinator")
    assert c.trino_host == "trino-coordinator"
    assert c.trino_port == 9090
    assert c.trino_user == "mcp-trino"
    assert c.trino_catalog == "iceberg"
    assert c.max_rows == 10000
    assert c.read_only is False
    assert c.auth_enabled is True


def test_read_only_flag_parsing(monkeypatch):
    monkeypatch.setenv("TRINO_READ_ONLY", "TRUE")
    assert Config.from_env().read_only is True
    monkeypatch.setenv("TRINO_READ_ONLY", "no")
    assert Config.from_env().read_only is False


def test_server_name_env_wins(monkeypatch):
    monkeypatch.setenv("MCP_SERVER_NAME", "custom")
    assert Config.from_env(server_name="trino-coordinator").server_name == "custom"


def test_user_token_env_for_stdio(monkeypatch):
    monkeypatch.setenv("MCP_USER_TOKEN", "tok")
    assert Config.from_env().user_token == "tok"


def test_trino_connection_env(monkeypatch):
    monkeypatch.setenv("TRINO_HTTP_SCHEME", "https")
    monkeypatch.setenv("TRINO_VERIFY", "/ca.pem")
    monkeypatch.setenv("TRINO_REQUEST_TIMEOUT_SECONDS", "12")
    monkeypatch.setenv("TRINO_IDENTITY_MODE", "Other")
    c = Config.from_env()
    assert (
        c.trino_http_scheme,
        c.trino_verify,
        c.trino_request_timeout,
        c.trino_identity_mode,
    ) == ("https", "/ca.pem", 12.0, "other")


def test_trino_connection_defaults():
    """The core's one mode, the configured identity, is the default, with no password."""
    c = Config.from_env()
    assert (
        c.trino_http_scheme,
        c.trino_verify,
        c.trino_request_timeout,
        c.trino_identity_mode,
        c.trino_password,
    ) == ("http", "true", 30.0, "service", "")


def test_trino_password_env(monkeypatch):
    monkeypatch.setenv("TRINO_PASSWORD", "s3cret")
    assert Config.from_env().trino_password == "s3cret"


def test_jwt_leeway_env(monkeypatch):
    monkeypatch.setenv("MCP_JWT_LEEWAY_SECONDS", "5")
    assert Config.from_env().jwt_leeway_seconds == 5
    monkeypatch.delenv("MCP_JWT_LEEWAY_SECONDS")
    assert Config.from_env().jwt_leeway_seconds == 30


def test_trino_source_and_query_budget_default_off_and_are_configurable(monkeypatch):
    monkeypatch.delenv("TRINO_SOURCE", raising=False)
    monkeypatch.delenv("TRINO_QUERY_TIMEOUT_SECONDS", raising=False)
    c = Config.from_env()
    assert (c.trino_source, c.query_timeout_seconds) == ("", 0.0)
    monkeypatch.setenv("TRINO_SOURCE", "agents-gateway")
    monkeypatch.setenv("TRINO_QUERY_TIMEOUT_SECONDS", "45")
    c = Config.from_env()
    assert (c.trino_source, c.query_timeout_seconds) == ("agents-gateway", 45.0)


def test_the_bound_of_a_call_defaults_off_and_is_configurable(monkeypatch):
    monkeypatch.delenv("MCP_CALL_TIMEOUT_SECONDS", raising=False)
    assert Config.from_env().call_timeout_seconds == 0.0
    monkeypatch.setenv("MCP_CALL_TIMEOUT_SECONDS", "120")
    assert Config.from_env().call_timeout_seconds == 120.0


def test_extensions_default_to_none_and_are_named_in_order(monkeypatch):
    monkeypatch.delenv("MCP_EXTENSIONS", raising=False)
    assert Config.from_env().extensions == ""
    monkeypatch.setenv("MCP_EXTENSIONS", "first, second")
    assert Config.from_env().extensions == "first, second"
