"""Caractérisation de core.config (P1 — mêmes variables d'env qu'avant)."""
import pytest

from core.config import Config

_ENV_KEYS = [
    "TRINO_HOST", "TRINO_PORT", "TRINO_USER", "TRINO_CATALOG",
    "TRINO_MAX_ROWS", "TRINO_READ_ONLY", "MCP_AUTH_ENABLED",
    "MCP_SERVER_NAME", "MCP_HEALTH_PORT",
    "MCP_JWKS_URL", "MCP_OIDC_ISSUER", "MCP_OIDC_AUDIENCE",
    "MCP_TRANSPORT", "MCP_PORT", "MCP_AUTH_REQUIRED",
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
    # défauts SÛRS : transport SSE inchangé, auth non strict (opt-in)
    assert c.transport == "sse" and c.mcp_port == 3000 and c.auth_required is False


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
