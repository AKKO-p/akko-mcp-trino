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
    "MCP_AUTH_ENABLED",
    "MCP_SERVER_NAME",
    "MCP_HEALTH_PORT",
    "MCP_JWKS_URL",
    "MCP_OIDC_ISSUER",
    "MCP_OIDC_AUDIENCE",
    "MCP_TRANSPORT",
    "MCP_PORT",
    "MCP_AUTH_REQUIRED",
    "MCP_RESOURCE_URL",
    "MCP_AGENT_KEYS",
    "MCP_RATE_LIMIT_USER",
    "MCP_RATE_LIMIT_AGENT",
    "MCP_RATE_LIMIT_WINDOW_SECONDS",
    "MCP_INTROSPECTION_URL",
    "MCP_INTROSPECTION_CLIENT_ID",
    "MCP_INTROSPECTION_CLIENT_SECRET",
    "MCP_INTROSPECTION_TTL_SECONDS",
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
    monkeypatch.setenv("TRINO_HOST", "akko-trino")
    monkeypatch.setenv("TRINO_PORT", "9090")
    monkeypatch.setenv("TRINO_USER", "mcp-trino")
    monkeypatch.setenv("TRINO_CATALOG", "iceberg")
    monkeypatch.setenv("TRINO_MAX_ROWS", "10000")
    monkeypatch.setenv("TRINO_READ_ONLY", "false")
    monkeypatch.setenv("MCP_AUTH_ENABLED", "true")
    c = Config.from_env(server_name="akko-trino")
    assert c.trino_host == "akko-trino"
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
    assert Config.from_env(server_name="akko-trino").server_name == "custom"


def test_resource_url_and_agent_keys_env(monkeypatch):
    monkeypatch.setenv("MCP_RESOURCE_URL", "https://mcp.example.com/")
    monkeypatch.setenv("MCP_AGENT_KEYS", "cursor:k1")
    c = Config.from_env()
    assert c.resource_url == "https://mcp.example.com", (
        "trailing slash would double in the metadata URL"
    )
    assert c.agent_keys == "cursor:k1"


def test_resource_url_and_agent_keys_default_off():
    c = Config.from_env()
    assert c.resource_url == "" and c.agent_keys == ""


def test_rate_limit_env(monkeypatch):
    monkeypatch.setenv("MCP_RATE_LIMIT_USER", "10")
    monkeypatch.setenv("MCP_RATE_LIMIT_AGENT", "100")
    monkeypatch.setenv("MCP_RATE_LIMIT_WINDOW_SECONDS", "30")
    c = Config.from_env()
    assert (c.rate_limit_user, c.rate_limit_agent, c.rate_limit_window_seconds) == (10, 100, 30)


def test_rate_limit_defaults_off():
    c = Config.from_env()
    assert (c.rate_limit_user, c.rate_limit_agent, c.rate_limit_window_seconds) == (0, 0, 60)


def test_introspection_env(monkeypatch):
    monkeypatch.setenv("MCP_INTROSPECTION_URL", "https://idp/introspect")
    monkeypatch.setenv("MCP_INTROSPECTION_CLIENT_ID", "mcp")
    monkeypatch.setenv("MCP_INTROSPECTION_CLIENT_SECRET", "s")
    monkeypatch.setenv("MCP_INTROSPECTION_TTL_SECONDS", "10")
    c = Config.from_env()
    assert (
        c.introspection_url,
        c.introspection_client_id,
        c.introspection_client_secret,
        c.introspection_ttl_seconds,
    ) == ("https://idp/introspect", "mcp", "s", 10)


def test_introspection_defaults_off():
    c = Config.from_env()
    assert c.introspection_url == "" and c.introspection_ttl_seconds == 30


def test_user_token_env_for_stdio(monkeypatch):
    monkeypatch.setenv("MCP_USER_TOKEN", "tok")
    assert Config.from_env().user_token == "tok"
