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
    "TRINO_IDENTITY_MODE",
    "TRINO_QUERY_TIMEOUT_SECONDS",
    "MCP_CALL_TIMEOUT_SECONDS",
    "MCP_RESOURCE_URL",
    "TRINO_TOKEN_EXCHANGE_URL",
    "TRINO_TOKEN_EXCHANGE_CLIENT_ID",
    "TRINO_TOKEN_EXCHANGE_CLIENT_SECRET",
    "TRINO_TOKEN_EXCHANGE_AUDIENCE",
    "TRINO_TOKEN_EXCHANGE_SCOPE",
    "TRINO_TOKEN_EXCHANGE_VERIFY",
    "TRINO_CLIENT_CERT",
    "TRINO_CLIENT_KEY",
    "MCP_MAX_CATALOGS",
    "MCP_DESCRIBE_SCHEMA_TABLES",
    "MCP_DESCRIBE_SCHEMA_COLUMNS",
    "MCP_SEARCH_MIN_LENGTH",
    "MCP_SEARCH_RESERVE_SECONDS",
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
    assert c.auth_enabled is True
    assert c.server_name == "trino-mcp"
    assert c.health_port == 3001
    assert c.jwks_url == "" and c.oidc_issuer == "" and c.oidc_audience == ""
    # streamable-http is the current MCP standard (Le Chat, Cursor, Claude); a verified
    # caller is required unless the administrator says otherwise
    assert c.transport == "streamable-http" and c.mcp_port == 3000 and c.auth_required is True


def test_the_default_runs_every_query_as_the_verified_caller_and_warns_of_nothing():
    """Safe by default: each query runs as the caller (``jwt``), a verified token is
    required, and there is nothing to warn about."""
    c = Config.from_env()
    assert (c.trino_identity_mode, c.auth_enabled, c.auth_required) == ("jwt", True, True)
    assert c.warnings() == []


def test_service_mode_and_an_open_door_are_warned_of_when_chosen(monkeypatch):
    monkeypatch.setenv("TRINO_IDENTITY_MODE", "service")
    monkeypatch.setenv("TRINO_USER", "reader")
    assert Config.from_env().warnings() == [
        "TRINO_IDENTITY_MODE=service: every query runs as the Trino user 'reader', "
        "whoever calls; Trino cannot tell callers apart"
    ]
    monkeypatch.setenv("MCP_AUTH_REQUIRED", "false")
    assert "a request without a valid token is served too" in Config.from_env().warnings()[1]
    monkeypatch.setenv("MCP_AUTH_ENABLED", "false")
    assert "no caller is verified" in Config.from_env().warnings()[1]


def test_a_door_that_requires_tokens_it_cannot_verify_is_a_problem(monkeypatch):
    monkeypatch.setenv("MCP_AUTH_ENABLED", "false")
    assert Config.from_env().problems() == [
        "MCP_AUTH_REQUIRED=true but MCP_AUTH_ENABLED=false: no token can be verified and "
        "every request would be refused; enable MCP_AUTH_ENABLED, or set "
        "MCP_AUTH_REQUIRED=false to serve without verifying callers"
    ]


def test_bounds_no_server_can_serve_are_problems(monkeypatch):
    assert Config.from_env().problems() == []
    monkeypatch.setenv("TRINO_MAX_ROWS", "0")
    monkeypatch.setenv("TRINO_QUERY_TIMEOUT_SECONDS", "-1")
    monkeypatch.setenv("MCP_CALL_TIMEOUT_SECONDS", "-2")
    assert Config.from_env().problems() == [
        "TRINO_MAX_ROWS=0: the row cap must be 1 or more",
        "TRINO_QUERY_TIMEOUT_SECONDS=-1: a time budget is 0 (none) or more",
        "MCP_CALL_TIMEOUT_SECONDS=-2: a time budget is 0 (none) or more",
    ]


def test_max_catalogs_defaults_to_ten_thousand_and_is_read_from_the_environment(monkeypatch):
    assert Config.from_env().max_catalogs == 10000
    monkeypatch.setenv("MCP_MAX_CATALOGS", "250")
    assert Config.from_env().max_catalogs == 250


def test_max_catalogs_below_one_is_refused_at_start(monkeypatch):
    monkeypatch.setenv("MCP_MAX_CATALOGS", "0")
    assert Config.from_env().problems() == [
        "MCP_MAX_CATALOGS=0: list_catalogs must be allowed 1 catalog or more"
    ]


def test_describe_schema_budgets_default_and_are_read_from_the_environment(monkeypatch):
    c = Config.from_env()
    assert (c.describe_schema_tables, c.describe_schema_columns) == (20, 1000)
    monkeypatch.setenv("MCP_DESCRIBE_SCHEMA_TABLES", "5")
    monkeypatch.setenv("MCP_DESCRIBE_SCHEMA_COLUMNS", "300")
    c = Config.from_env()
    assert (c.describe_schema_tables, c.describe_schema_columns) == (5, 300)


def test_describe_schema_budgets_below_one_are_refused_at_start(monkeypatch):
    monkeypatch.setenv("MCP_DESCRIBE_SCHEMA_TABLES", "0")
    monkeypatch.setenv("MCP_DESCRIBE_SCHEMA_COLUMNS", "-1")
    assert Config.from_env().problems() == [
        "MCP_DESCRIBE_SCHEMA_TABLES=0: a page of describe_schema must hold 1 table or more",
        "MCP_DESCRIBE_SCHEMA_COLUMNS=-1: describe_schema must be allowed 1 column or more",
    ]


def test_a_describe_schema_page_above_the_maximum_is_refused_at_start(monkeypatch):
    """A page is one UNION ALL branch per table, twice: its cost grows with the page, and
    a page far too large breaks Trino's analyzer. The maximum itself is served."""
    from akko_mcp_trino.config import MAX_DESCRIBE_SCHEMA_TABLES

    assert MAX_DESCRIBE_SCHEMA_TABLES == 100
    monkeypatch.setenv("MCP_DESCRIBE_SCHEMA_TABLES", "100")
    assert Config.from_env().problems() == []
    monkeypatch.setenv("MCP_DESCRIBE_SCHEMA_TABLES", "101")
    assert Config.from_env().problems() == [
        "MCP_DESCRIBE_SCHEMA_TABLES=101: a page of describe_schema holds 100 tables at most"
    ]


def test_token_exchange_resource_and_client_certificate_env(monkeypatch):
    c = Config.from_env()
    assert (c.resource_url, c.token_exchange_url, c.token_exchange_verify) == ("", "", "true")
    assert (c.trino_client_cert, c.trino_client_key) == ("", "")
    for name, value in {
        "MCP_RESOURCE_URL": "https://mcp.example/mcp",
        "TRINO_TOKEN_EXCHANGE_URL": "https://idp/token",
        "TRINO_TOKEN_EXCHANGE_CLIENT_ID": "mcp",
        "TRINO_TOKEN_EXCHANGE_CLIENT_SECRET": "s",
        "TRINO_TOKEN_EXCHANGE_AUDIENCE": "trino",
        "TRINO_TOKEN_EXCHANGE_SCOPE": "openid",
        "TRINO_TOKEN_EXCHANGE_VERIFY": "/ca.pem",
        "TRINO_CLIENT_CERT": "/c.pem",
        "TRINO_CLIENT_KEY": "/k.pem",
    }.items():
        monkeypatch.setenv(name, value)
    c = Config.from_env()
    assert (
        c.resource_url,
        c.token_exchange_url,
        c.token_exchange_client_id,
        c.token_exchange_client_secret,
        c.token_exchange_audience,
        c.token_exchange_scope,
        c.token_exchange_verify,
        c.trino_client_cert,
        c.trino_client_key,
    ) == (
        "https://mcp.example/mcp",
        "https://idp/token",
        "mcp",
        "s",
        "trino",
        "openid",
        "/ca.pem",
        "/c.pem",
        "/k.pem",
    )


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
    """The caller's identity (``jwt``) is the default mode, with no password."""
    c = Config.from_env()
    assert (
        c.trino_http_scheme,
        c.trino_verify,
        c.trino_request_timeout,
        c.trino_identity_mode,
        c.trino_password,
    ) == ("http", "true", 30.0, "jwt", "")


def test_trino_password_env(monkeypatch):
    monkeypatch.setenv("TRINO_PASSWORD", "s3cret")
    assert Config.from_env().trino_password == "s3cret"


def test_jwt_leeway_env(monkeypatch):
    monkeypatch.setenv("MCP_JWT_LEEWAY_SECONDS", "5")
    assert Config.from_env().jwt_leeway_seconds == 5
    monkeypatch.delenv("MCP_JWT_LEEWAY_SECONDS")
    assert Config.from_env().jwt_leeway_seconds == 30


def test_trino_source_defaults_off_and_the_query_budget_to_a_minute(monkeypatch):
    monkeypatch.delenv("TRINO_SOURCE", raising=False)
    monkeypatch.delenv("TRINO_QUERY_TIMEOUT_SECONDS", raising=False)
    c = Config.from_env()
    assert (c.trino_source, c.query_timeout_seconds) == ("", 60.0)
    monkeypatch.setenv("TRINO_SOURCE", "agents-gateway")
    monkeypatch.setenv("TRINO_QUERY_TIMEOUT_SECONDS", "45")
    c = Config.from_env()
    assert (c.trino_source, c.query_timeout_seconds) == ("agents-gateway", 45.0)


def test_the_bound_of_a_call_defaults_to_two_minutes_and_is_configurable(monkeypatch):
    monkeypatch.delenv("MCP_CALL_TIMEOUT_SECONDS", raising=False)
    assert Config.from_env().call_timeout_seconds == 120.0
    monkeypatch.setenv("MCP_CALL_TIMEOUT_SECONDS", "0")
    assert Config.from_env().call_timeout_seconds == 0.0


def test_extensions_default_to_none_and_are_named_in_order(monkeypatch):
    monkeypatch.delenv("MCP_EXTENSIONS", raising=False)
    assert Config.from_env().extensions == ""
    monkeypatch.setenv("MCP_EXTENSIONS", "first, second")
    assert Config.from_env().extensions == "first, second"


def test_search_settings_default_and_are_read_from_the_environment(monkeypatch):
    c = Config.from_env()
    assert (c.search_min_length, c.search_reserve_seconds) == (3, 2.0)
    monkeypatch.setenv("MCP_SEARCH_MIN_LENGTH", "5")
    monkeypatch.setenv("MCP_SEARCH_RESERVE_SECONDS", "0.5")
    c = Config.from_env()
    assert (c.search_min_length, c.search_reserve_seconds) == (5, 0.5)


def test_search_settings_no_search_can_serve_are_refused_at_start(monkeypatch):
    monkeypatch.setenv("MCP_SEARCH_MIN_LENGTH", "0")
    monkeypatch.setenv("MCP_SEARCH_RESERVE_SECONDS", "-1")
    assert Config.from_env().problems() == [
        "MCP_SEARCH_MIN_LENGTH=0: search_columns must ask for 1 character or more",
        "MCP_SEARCH_RESERVE_SECONDS=-1: a reserve is 0 seconds or more",
    ]


def test_a_reserve_that_takes_the_whole_call_budget_is_refused(monkeypatch):
    """A reserve of the whole budget leaves the search no time to read anything."""
    monkeypatch.setenv("MCP_CALL_TIMEOUT_SECONDS", "10")
    monkeypatch.setenv("MCP_SEARCH_RESERVE_SECONDS", "10")
    assert Config.from_env().problems() == [
        "MCP_SEARCH_RESERVE_SECONDS=10 is not below MCP_CALL_TIMEOUT_SECONDS=10: "
        "search_columns would have no time left to search"
    ]
    monkeypatch.setenv("MCP_CALL_TIMEOUT_SECONDS", "0")
    assert Config.from_env().problems() == []


def test_no_call_budget_is_warned_of_since_a_search_of_every_catalog_is_refused(monkeypatch):
    monkeypatch.setenv("MCP_CALL_TIMEOUT_SECONDS", "0")
    assert Config.from_env().warnings() == [
        "MCP_CALL_TIMEOUT_SECONDS=0: a tool call has no time budget, so search_columns "
        "refuses to search every catalog and searches a named catalog only"
    ]


def test_a_reserve_of_zero_with_a_call_budget_is_warned_of(monkeypatch):
    """Without a reserve, a search's last read may reach the call's deadline: the call then
    answers its own time_budget error, with nothing of what the search found."""
    monkeypatch.setenv("MCP_SEARCH_RESERVE_SECONDS", "0")
    assert Config.from_env().warnings() == [
        "MCP_SEARCH_RESERVE_SECONDS=0: a search of search_columns may run to the end of the "
        "call's budget, and the call then answers its time_budget error with nothing it found"
    ]
    monkeypatch.setenv("MCP_CALL_TIMEOUT_SECONDS", "0")
    assert "MCP_SEARCH_RESERVE_SECONDS" not in " ".join(Config.from_env().warnings())
