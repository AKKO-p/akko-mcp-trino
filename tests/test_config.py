"""akko_mcp_trino.config: every setting comes from the environment, with neutral defaults."""

import pytest

from akko_mcp_trino.config import Config

_ENV_KEYS = [
    "MCP_AUDIT_MAX_NAMES",
    "MCP_JWT_CLIENT_CLAIMS",
    "MCP_JWT_SCOPE_CLAIMS",
    "MCP_EXTRA_READ_COMMANDS",
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
    "MCP_SEARCH_MAX_WORDS",
    "MCP_SEARCH_MAX_WORD_LENGTH",
    "MCP_SEARCH_RESERVE_SECONDS",
    "MCP_CONTEXT_PROVIDERS",
    "MCP_CONTEXT_TIMEOUT_SECONDS",
    "MCP_CONTEXT_RESERVE_SECONDS",
    "MCP_CONTEXT_MAX_CONCURRENCY",
    "MCP_CONTEXT_RETRY_AFTER_SECONDS",
    "MCP_CONTEXT_REFUSED_RETRY_SECONDS",
    "MCP_CONTEXT_BATCH",
    "MCP_CONTEXT_TTL_SECONDS",
    "MCP_CONTEXT_CACHE_BYTES",
    "MCP_CONTEXT_TERMS_CACHE_BYTES",
    "MCP_VISIBILITY_TTL_SECONDS",
    "MCP_VISIBILITY_CACHE_BYTES",
    "MCP_VISIBILITY_MAX_NAMES",
    "MCP_CONTEXT_REQUIRE_SELECT",
    "MCP_CONTEXT_CANARY",
    "MCP_CONTEXT_TEXT_CHARS",
    "MCP_CONTEXT_RESPONSE_CHARS",
    "MCP_CONTEXT_MAX_COLUMNS",
    "MCP_PROFILE_MAX_COLUMNS",
    "MCP_PROFILE_SAMPLE_ROWS",
    "MCP_PROFILE_MODES",
    "MCP_PROFILE_VALUE_CHARS",
    "MCP_PROFILE_DESCRIBE_ROWS",
    "MCP_PROFILE_CLIENT_TAG",
    "MCP_PROFILE_TIMEOUT_SECONDS",
    "MCP_PROFILE_MAX_CONCURRENT",
    "MCP_DESCRIBE_MAX_COLUMNS",
    "MCP_SEARCH_CONCURRENCY",
    "MCP_PROFILE_TYPE_CHARS",
    "MCP_SEARCH_MAX_THREADS",
    "MCP_VISIBILITY_ENABLED",
    "MCP_MAX_SQL_CHARS",
    "MCP_MAX_SQL_NESTING",
    "MCP_SQL_PARSE_SECONDS",
    "MCP_SQL_PARSE_WORKERS",
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
    # streamable-http is the current MCP standard transport; a verified
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


def test_search_word_bounds_default_and_are_read_from_the_environment(monkeypatch):
    c = Config.from_env()
    assert (c.search_max_words, c.search_max_word_length) == (8, 64)
    monkeypatch.setenv("MCP_SEARCH_MAX_WORDS", "3")
    monkeypatch.setenv("MCP_SEARCH_MAX_WORD_LENGTH", "20")
    c = Config.from_env()
    assert (c.search_max_words, c.search_max_word_length) == (3, 20)
    assert c.problems() == []


def test_search_word_bounds_no_search_can_serve_are_refused_at_start(monkeypatch):
    monkeypatch.setenv("MCP_SEARCH_MAX_WORDS", "0")
    monkeypatch.setenv("MCP_SEARCH_MAX_WORD_LENGTH", "2")
    assert Config.from_env().problems() == [
        "MCP_SEARCH_MAX_WORDS=0: search_columns must take 1 word or more",
        "MCP_SEARCH_MAX_WORD_LENGTH=2 is below MCP_SEARCH_MIN_LENGTH=3: no word could be searched",
    ]


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


def test_extra_read_commands_are_none_by_default_and_said_when_opened(monkeypatch):
    """0.7.1: the reads Trino answers past a row filter or a column mask stay closed
    unless named; once named, every start and every host is told."""
    assert Config.from_env().extra_read_commands == ""
    assert not any("MCP_EXTRA_READ_COMMANDS" in w for w in Config.from_env().warnings())
    monkeypatch.setenv("MCP_EXTRA_READ_COMMANDS", "show stats, EXPLAIN")
    c = Config.from_env()
    assert c.problems() == []
    assert (
        "MCP_EXTRA_READ_COMMANDS=EXPLAIN, SHOW STATS: these reads can show facts beyond a "
        "row filter or a column mask"
    ) in c.warnings()


def test_an_unknown_extra_read_command_refuses_to_start(monkeypatch):
    monkeypatch.setenv("MCP_EXTRA_READ_COMMANDS", "SHOW STATS, DROP")
    c = Config.from_env()
    assert len(c.problems()) == 1 and "unknown command 'DROP'" in c.problems()[0]
    assert not any("MCP_EXTRA_READ_COMMANDS" in w for w in c.warnings())


def test_the_context_settings_have_their_defaults_and_names():
    c = Config.from_env()
    assert (c.context_providers, c.context_timeout_seconds, c.context_reserve_seconds) == (
        "",
        5.0,
        0.5,
    )
    assert (c.context_max_concurrency, c.context_retry_after_seconds) == (32, 30.0)
    assert (c.context_refused_retry_seconds, c.context_batch, c.context_ttl_seconds) == (
        3600.0,
        50,
        900.0,
    )
    assert (c.context_cache_bytes, c.context_terms_cache_bytes) == (134217728, 16777216)
    assert (c.visibility_ttl_seconds, c.visibility_cache_bytes) == (60.0, 33554432)
    assert (c.visibility_max_names, c.context_require_select, c.context_canary) == (
        10000,
        False,
        "",
    )
    assert (c.context_text_chars, c.context_response_chars, c.context_max_columns) == (
        500,
        60000,
        500,
    )
    assert (c.profile_max_columns, c.profile_sample_rows, c.visibility_switch) == (50, 100000, "")
    assert c.problems() == [] and c.warnings() == []


def test_every_context_setting_is_read_from_its_variable(monkeypatch):
    for name, value in {
        "MCP_CONTEXT_PROVIDERS": "catalog",
        "MCP_CONTEXT_TIMEOUT_SECONDS": "7",
        "MCP_CONTEXT_RESERVE_SECONDS": "1.5",
        "MCP_CONTEXT_MAX_CONCURRENCY": "3",
        "MCP_CONTEXT_RETRY_AFTER_SECONDS": "4",
        "MCP_CONTEXT_REFUSED_RETRY_SECONDS": "5",
        "MCP_CONTEXT_BATCH": "6",
        "MCP_CONTEXT_TTL_SECONDS": "8",
        "MCP_CONTEXT_CACHE_BYTES": "9",
        "MCP_CONTEXT_TERMS_CACHE_BYTES": "10",
        "MCP_VISIBILITY_TTL_SECONDS": "11",
        "MCP_VISIBILITY_CACHE_BYTES": "12",
        "MCP_VISIBILITY_MAX_NAMES": "13",
        "MCP_CONTEXT_REQUIRE_SELECT": "TRUE",
        "MCP_CONTEXT_CANARY": "a.b.c",
        "MCP_CONTEXT_TEXT_CHARS": "14",
        "MCP_CONTEXT_RESPONSE_CHARS": "15",
        "MCP_CONTEXT_MAX_COLUMNS": "16",
        "MCP_PROFILE_MAX_COLUMNS": "17",
        "MCP_PROFILE_SAMPLE_ROWS": "18",
        "MCP_VISIBILITY_ENABLED": "true",
    }.items():
        monkeypatch.setenv(name, value)
    c = Config.from_env()
    assert (c.context_providers, c.context_timeout_seconds, c.context_reserve_seconds) == (
        "catalog",
        7.0,
        1.5,
    )
    assert (c.context_max_concurrency, c.context_retry_after_seconds) == (3, 4.0)
    assert (c.context_refused_retry_seconds, c.context_batch, c.context_ttl_seconds) == (
        5.0,
        6,
        8.0,
    )
    assert (c.context_cache_bytes, c.context_terms_cache_bytes) == (9, 10)
    assert (c.visibility_ttl_seconds, c.visibility_cache_bytes, c.visibility_max_names) == (
        11.0,
        12,
        13,
    )
    assert c.context_require_select is True and c.context_canary == "a.b.c"
    assert (c.context_text_chars, c.context_response_chars, c.context_max_columns) == (14, 15, 16)
    assert (c.profile_max_columns, c.profile_sample_rows, c.visibility_switch) == (17, 18, "true")
    assert c.problems() == []


def test_the_visibility_switch_of_the_old_extension_refuses_to_start(monkeypatch):
    monkeypatch.setenv("MCP_VISIBILITY_ENABLED", "false")
    assert Config.from_env().problems() == [
        "MCP_VISIBILITY_ENABLED was removed in 0.8: the filter by Trino's rights cannot be "
        "turned off"
    ]
    monkeypatch.setenv("MCP_VISIBILITY_ENABLED", " False ")
    assert len(Config.from_env().problems()) == 1


def test_a_context_timeout_of_zero_is_refused(monkeypatch):
    monkeypatch.setenv("MCP_CONTEXT_TIMEOUT_SECONDS", "0")
    assert Config.from_env().problems() == [
        "MCP_CONTEXT_TIMEOUT_SECONDS=0: a context read needs more than 0 seconds"
    ]


def test_a_negative_reserve_or_ttl_is_refused(monkeypatch):
    monkeypatch.setenv("MCP_CONTEXT_RESERVE_SECONDS", "-1")
    monkeypatch.setenv("MCP_CONTEXT_TTL_SECONDS", "-2")
    monkeypatch.setenv("MCP_VISIBILITY_TTL_SECONDS", "-3")
    monkeypatch.setenv("MCP_CONTEXT_RETRY_AFTER_SECONDS", "-4")
    monkeypatch.setenv("MCP_CONTEXT_REFUSED_RETRY_SECONDS", "-5")
    assert Config.from_env().problems() == [
        "MCP_CONTEXT_RESERVE_SECONDS=-1: a reserve is 0 seconds or more",
        "MCP_CONTEXT_RETRY_AFTER_SECONDS=-4: a delay is 0 seconds or more",
        "MCP_CONTEXT_REFUSED_RETRY_SECONDS=-5: a delay is 0 seconds or more",
        "MCP_CONTEXT_TTL_SECONDS=-2: a lifetime is 0 seconds or more",
        "MCP_VISIBILITY_TTL_SECONDS=-3: a lifetime is 0 seconds or more",
    ]


@pytest.mark.parametrize("reserve", ["0", "0.29"])
def test_a_provider_needs_a_reserve_of_the_call_kept_back(monkeypatch, reserve):
    """The chain waits a little past its deadline for a provider to hand back control: a
    reserve shorter than that lets the context end past the call's own deadline."""
    monkeypatch.setenv("MCP_CONTEXT_RESERVE_SECONDS", reserve)
    assert Config.from_env().problems() == [], "without a provider the reserve is unused"
    monkeypatch.setenv("MCP_CONTEXT_PROVIDERS", "file")
    assert Config.from_env().problems() == [
        f"MCP_CONTEXT_RESERVE_SECONDS={float(reserve):g}: with a context provider, part of "
        "each call's budget must be kept back so that the context ends before the call "
        "does; set 0.3 or more"
    ]
    monkeypatch.setenv("MCP_CONTEXT_RESERVE_SECONDS", "0.3")
    assert Config.from_env().problems() == []


def test_the_least_reserve_covers_the_wait_of_the_chain_past_its_deadline():
    from akko_mcp_trino.config import MIN_CONTEXT_RESERVE_SECONDS
    from akko_mcp_trino.context import chain

    assert MIN_CONTEXT_RESERVE_SECONDS > chain._GRACE_SECONDS


@pytest.mark.parametrize(
    "name",
    [
        "MCP_CONTEXT_MAX_CONCURRENCY",
        "MCP_CONTEXT_BATCH",
        "MCP_CONTEXT_CACHE_BYTES",
        "MCP_CONTEXT_TERMS_CACHE_BYTES",
        "MCP_VISIBILITY_CACHE_BYTES",
        "MCP_VISIBILITY_MAX_NAMES",
        "MCP_CONTEXT_TEXT_CHARS",
        "MCP_CONTEXT_RESPONSE_CHARS",
        "MCP_CONTEXT_MAX_COLUMNS",
        "MCP_PROFILE_MAX_COLUMNS",
        "MCP_PROFILE_SAMPLE_ROWS",
        "MCP_PROFILE_VALUE_CHARS",
        "MCP_PROFILE_DESCRIBE_ROWS",
    ],
)
def test_a_bound_below_one_is_refused(monkeypatch, name):
    monkeypatch.setenv(name, "0")
    assert Config.from_env().problems() == [f"{name}=0: a bound is 1 or more"]


@pytest.mark.parametrize(
    "canary", ["orders", "a.b", "a.b.c.d", "a..c", "a.b.c d", "a.b;c.d", "system.runtime.queries"]
)
def test_a_canary_must_name_one_table(monkeypatch, canary):
    monkeypatch.setenv("MCP_CONTEXT_CANARY", canary)
    (problem,) = Config.from_env().problems()
    assert problem.startswith("MCP_CONTEXT_CANARY=")
    assert "catalog.schema.table" in problem


def test_a_canary_of_three_identifiers_is_accepted(monkeypatch):
    monkeypatch.setenv("MCP_CONTEXT_CANARY", "sales.crm.accounts")
    assert Config.from_env().problems() == []


def test_context_warnings_come_only_with_a_provider(monkeypatch):
    monkeypatch.setenv("MCP_CONTEXT_PROVIDERS", "catalog")
    assert Config.from_env().warnings() == [
        "MCP_CONTEXT_CANARY is not set: nothing checks that Trino filters its listings, "
        "and context follows what Trino lists"
    ]
    monkeypatch.setenv("MCP_CONTEXT_CANARY", "a.b.c")
    assert Config.from_env().warnings() == []
    monkeypatch.setenv("TRINO_IDENTITY_MODE", "service")
    monkeypatch.setenv("TRINO_USER", "svc")
    assert Config.from_env().warnings()[-1] == "context visibility is the service account's"
    monkeypatch.delenv("MCP_CONTEXT_PROVIDERS")
    assert "context visibility" not in " ".join(Config.from_env().warnings())


def test_a_providers_list_of_nothing_warns_of_nothing(monkeypatch):
    monkeypatch.setenv("MCP_CONTEXT_PROVIDERS", " , ")
    assert Config.from_env().warnings() == []


# ---- profile_table: modes, value length, DESCRIBE rows, client tag, time budget


def test_the_profile_settings_have_their_defaults():
    c = Config.from_env()
    assert (c.profile_modes, c.profile_value_chars, c.profile_describe_rows) == (
        "sample,bernoulli,full",
        200,
        5000,
    )
    assert (c.profile_client_tag, c.profile_timeout_seconds) == ("mcp-profile", 0.0)
    assert c.allowed_profile_modes() == ("sample", "bernoulli", "full")
    assert c.problems() == []


def test_the_profile_settings_are_read_from_their_variables(monkeypatch):
    for name, value in {
        "MCP_PROFILE_MODES": " Sample , full,sample",
        "MCP_PROFILE_VALUE_CHARS": "21",
        "MCP_PROFILE_DESCRIBE_ROWS": "22",
        "MCP_PROFILE_CLIENT_TAG": "agents-profile",
        "MCP_PROFILE_TIMEOUT_SECONDS": "2.5",
    }.items():
        monkeypatch.setenv(name, value)
    c = Config.from_env()
    assert c.allowed_profile_modes() == ("sample", "full"), "folded, once each, in order"
    assert (c.profile_value_chars, c.profile_describe_rows) == (21, 22)
    assert (c.profile_client_tag, c.profile_timeout_seconds) == ("agents-profile", 2.5)
    assert c.problems() == []


@pytest.mark.parametrize("modes", ["", " , ", "sample,stats", "all"])
def test_profile_modes_empty_or_unknown_refuse_to_start(monkeypatch, modes):
    monkeypatch.setenv("MCP_PROFILE_MODES", modes)
    assert Config.from_env().problems() == [
        f"MCP_PROFILE_MODES={modes!r}: name one mode at least, comma-separated, among "
        "sample, bernoulli and full"
    ]


@pytest.mark.parametrize("tag", ["", " ", "a,b", "a b", "é", "x" * 101])
def test_a_profile_client_tag_trino_cannot_carry_refuses_to_start(monkeypatch, tag):
    monkeypatch.setenv("MCP_PROFILE_CLIENT_TAG", tag)
    assert Config.from_env().problems() == [
        f"MCP_PROFILE_CLIENT_TAG={tag.strip()!r}: a client tag is 1 to 100 letters, digits "
        "and _ . : = / -, without a comma"
    ]


@pytest.mark.parametrize("seconds", ["-1", "nan", "inf"])
def test_a_negative_or_infinite_profile_timeout_refuses_to_start(monkeypatch, seconds):
    monkeypatch.setenv("MCP_PROFILE_TIMEOUT_SECONDS", seconds)
    assert Config.from_env().problems() == [
        f"MCP_PROFILE_TIMEOUT_SECONDS={float(seconds):g}: a time budget is 0 (the call's) "
        "or a number of seconds above 0"
    ]


def test_a_profile_timeout_past_the_call_budget_refuses_to_start(monkeypatch):
    monkeypatch.setenv("MCP_CALL_TIMEOUT_SECONDS", "5")
    monkeypatch.setenv("MCP_PROFILE_TIMEOUT_SECONDS", "6")
    assert Config.from_env().problems() == [
        "MCP_PROFILE_TIMEOUT_SECONDS=6 is past MCP_CALL_TIMEOUT_SECONDS=5: the aggregate "
        "query runs within its call; set it to 5 or less, or 0 for the call's budget"
    ]
    monkeypatch.setenv("MCP_PROFILE_TIMEOUT_SECONDS", "5")
    assert Config.from_env().problems() == []
    monkeypatch.setenv("MCP_CALL_TIMEOUT_SECONDS", "0")
    monkeypatch.setenv("TRINO_QUERY_TIMEOUT_SECONDS", "0")
    monkeypatch.setenv("MCP_PROFILE_TIMEOUT_SECONDS", "600")
    assert Config.from_env().problems() == [], "without any other budget, any budget of its own"


def test_profile_calls_at_once_are_four_by_default_and_read_from_their_variable(monkeypatch):
    assert Config.from_env().profile_max_concurrent == 4
    monkeypatch.setenv("MCP_PROFILE_MAX_CONCURRENT", "64")
    assert Config.from_env().profile_max_concurrent == 64
    assert Config.from_env().problems() == []


@pytest.mark.parametrize("count", ["0", "65"])
def test_profile_calls_at_once_outside_1_to_64_refuse_to_start(monkeypatch, count):
    monkeypatch.setenv("MCP_PROFILE_MAX_CONCURRENT", count)
    assert Config.from_env().problems() == [
        f"MCP_PROFILE_MAX_CONCURRENT={count}: profile_table runs 1 to 64 calls at once"
    ]


def test_profile_columns_past_the_hard_cap_refuse_to_start(monkeypatch):
    from akko_mcp_trino.config import MAX_PROFILE_MAX_COLUMNS

    assert MAX_PROFILE_MAX_COLUMNS == 500
    monkeypatch.setenv("MCP_PROFILE_MAX_COLUMNS", "500")
    assert Config.from_env().problems() == []
    monkeypatch.setenv("MCP_PROFILE_MAX_COLUMNS", "501")
    assert Config.from_env().problems() == [
        "MCP_PROFILE_MAX_COLUMNS=501: profile_table profiles 500 columns at most"
    ]


def test_a_profile_timeout_past_the_query_budget_refuses_to_start(monkeypatch):
    monkeypatch.setenv("MCP_CALL_TIMEOUT_SECONDS", "900")
    monkeypatch.setenv("TRINO_QUERY_TIMEOUT_SECONDS", "60")
    monkeypatch.setenv("MCP_PROFILE_TIMEOUT_SECONDS", "600")
    assert Config.from_env().problems() == [
        "MCP_PROFILE_TIMEOUT_SECONDS=600 is past TRINO_QUERY_TIMEOUT_SECONDS=60: the "
        "aggregate query is a Trino query; set it to 60 or less, or raise "
        "TRINO_QUERY_TIMEOUT_SECONDS"
    ]
    monkeypatch.setenv("TRINO_QUERY_TIMEOUT_SECONDS", "0")
    assert Config.from_env().problems() == []


@pytest.mark.parametrize(
    "own, query, call, effective",
    [
        ("0", "60", "120", 60.0),
        ("0", "0", "120", 120.0),
        ("0", "200", "120", 120.0),
        ("30", "60", "120", 30.0),
        ("30", "0", "0", 30.0),
        ("0", "45", "0", 45.0),
        ("0", "0", "0", 0.0),
    ],
)
def test_the_budget_of_the_aggregate_is_the_smallest_set(monkeypatch, own, query, call, effective):
    monkeypatch.setenv("MCP_PROFILE_TIMEOUT_SECONDS", own)
    monkeypatch.setenv("TRINO_QUERY_TIMEOUT_SECONDS", query)
    monkeypatch.setenv("MCP_CALL_TIMEOUT_SECONDS", call)
    assert Config.from_env().profile_budget_seconds() == effective


def test_full_profiles_without_any_budget_are_warned_of(monkeypatch):
    monkeypatch.setenv("TRINO_QUERY_TIMEOUT_SECONDS", "0")
    monkeypatch.setenv("MCP_CALL_TIMEOUT_SECONDS", "0")
    warning = (
        "MCP_PROFILE_MODES allows full while MCP_PROFILE_TIMEOUT_SECONDS, "
        "TRINO_QUERY_TIMEOUT_SECONDS and MCP_CALL_TIMEOUT_SECONDS are 0: a full profile reads "
        "every row it may read with no time limit"
    )
    assert warning in Config.from_env().warnings()
    monkeypatch.setenv("MCP_PROFILE_MODES", "sample,bernoulli")
    assert warning not in Config.from_env().warnings()
    monkeypatch.setenv("MCP_PROFILE_MODES", "full")
    monkeypatch.setenv("MCP_PROFILE_TIMEOUT_SECONDS", "30")
    assert warning not in Config.from_env().warnings()


# ---- describe_table pages its columns; search_columns reads several catalogs at once


def test_the_discovery_settings_have_their_defaults_and_variables(monkeypatch):
    c = Config.from_env()
    assert (c.describe_max_columns, c.search_concurrency) == (5000, 8)
    monkeypatch.setenv("MCP_DESCRIBE_MAX_COLUMNS", "100000")
    monkeypatch.setenv("MCP_SEARCH_CONCURRENCY", "32")
    c = Config.from_env()
    assert (c.describe_max_columns, c.search_concurrency) == (100000, 32)
    assert c.problems() == []


@pytest.mark.parametrize("count", ["0", "100001"])
def test_describe_max_columns_outside_1_to_100000_refuses_to_start(monkeypatch, count):
    monkeypatch.setenv("MCP_DESCRIBE_MAX_COLUMNS", count)
    assert Config.from_env().problems() == [
        f"MCP_DESCRIBE_MAX_COLUMNS={count}: describe_table reads 1 to 100000 columns of a table"
    ]


@pytest.mark.parametrize("count", ["0", "33"])
def test_search_concurrency_outside_1_to_32_refuses_to_start(monkeypatch, count):
    monkeypatch.setenv("MCP_SEARCH_CONCURRENCY", count)
    assert Config.from_env().problems() == [
        f"MCP_SEARCH_CONCURRENCY={count}: search_columns reads 1 to 32 catalogs at once"
    ]


def test_the_bound_of_a_type_is_500_by_default_and_read_from_its_variable(monkeypatch):
    assert Config.from_env().profile_type_chars == 500
    monkeypatch.setenv("MCP_PROFILE_TYPE_CHARS", "100000")
    assert Config.from_env().profile_type_chars == 100000
    assert Config.from_env().problems() == []


@pytest.mark.parametrize("count", ["0", "100001"])
def test_a_bound_of_a_type_outside_1_to_100000_refuses_to_start(monkeypatch, count):
    monkeypatch.setenv("MCP_PROFILE_TYPE_CHARS", count)
    assert Config.from_env().problems() == [
        f"MCP_PROFILE_TYPE_CHARS={count}: profile_table serves 1 to 100000 characters of a type"
    ]


def test_search_threads_of_a_process_are_32_by_default_and_read_from_their_variable(monkeypatch):
    assert Config.from_env().search_max_threads == 32
    monkeypatch.setenv("MCP_SEARCH_MAX_THREADS", "256")
    assert Config.from_env().search_max_threads == 256
    assert Config.from_env().problems() == []


@pytest.mark.parametrize("count", ["0", "257"])
def test_search_threads_outside_1_to_256_refuse_to_start(monkeypatch, count):
    monkeypatch.setenv("MCP_SEARCH_MAX_THREADS", count)
    assert Config.from_env().problems() == [
        f"MCP_SEARCH_MAX_THREADS={count}: search_columns runs 1 to 256 reads at once in a process"
    ]


def test_describe_reading_more_columns_than_profile_is_warned_of(monkeypatch):
    warning = (
        "MCP_DESCRIBE_MAX_COLUMNS=6000 is above MCP_PROFILE_DESCRIBE_ROWS=5000: describe_table "
        "shows columns profile_table cannot name"
    )
    assert warning not in Config.from_env().warnings()
    monkeypatch.setenv("MCP_DESCRIBE_MAX_COLUMNS", "6000")
    assert warning in Config.from_env().warnings()


def test_the_claims_of_the_client_and_the_scopes_have_their_defaults_and_variables(monkeypatch):
    c = Config.from_env()
    assert (c.jwt_client_claims, c.jwt_scope_claims) == ("azp,client_id", "scope")
    assert (c.client_claims(), c.scope_claims()) == (("azp", "client_id"), ("scope",))
    monkeypatch.setenv("MCP_JWT_CLIENT_CLAIMS", " cid , azp,cid")
    monkeypatch.setenv("MCP_JWT_SCOPE_CLAIMS", "scp")
    c = Config.from_env()
    assert (c.client_claims(), c.scope_claims()) == (("cid", "azp"), ("scp",))
    assert c.problems() == []


@pytest.mark.parametrize("name", ["MCP_JWT_CLIENT_CLAIMS", "MCP_JWT_SCOPE_CLAIMS"])
@pytest.mark.parametrize("value", [" , ", "a b", "x;y"])
def test_a_claim_list_with_no_name_or_a_bad_name_refuses_to_start(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    (problem,) = Config.from_env().problems()
    assert problem.startswith(f"{name}={value!r}: ")


def test_the_names_an_audit_line_keeps_are_200_by_default_and_read_from_their_variable(
    monkeypatch,
):
    assert Config.from_env().audit_max_names == 200
    monkeypatch.setenv("MCP_AUDIT_MAX_NAMES", "100000")
    assert Config.from_env().audit_max_names == 100000
    assert Config.from_env().problems() == []


@pytest.mark.parametrize("count", ["0", "100001"])
def test_names_kept_outside_1_to_100000_refuse_to_start(monkeypatch, count):
    monkeypatch.setenv("MCP_AUDIT_MAX_NAMES", count)
    assert Config.from_env().problems() == [
        f"MCP_AUDIT_MAX_NAMES={count}: an audit line keeps 1 to 100000 names a list"
    ]


def test_bounds_settings_validated_and_in_check(monkeypatch, capsys):
    """MCP_MAX_SQL_CHARS (100000, 1000 at least) and MCP_MAX_SQL_NESTING (32, 8 at least):
    read from the environment, refused below their least, printed by --check."""
    from akko_mcp_trino.__main__ import check_config

    c = Config.from_env()
    assert (c.max_sql_chars, c.max_sql_nesting) == (100000, 32)
    assert not [p for p in c.problems() if "MCP_MAX_SQL" in p]
    monkeypatch.setenv("MCP_MAX_SQL_CHARS", "1000")
    monkeypatch.setenv("MCP_MAX_SQL_NESTING", "8")
    c = Config.from_env()
    assert (c.max_sql_chars, c.max_sql_nesting) == (1000, 8)
    assert not [p for p in c.problems() if "MCP_MAX_SQL" in p]
    monkeypatch.setenv("MCP_MAX_SQL_CHARS", "999")
    monkeypatch.setenv("MCP_MAX_SQL_NESTING", "7")
    assert [p for p in Config.from_env().problems() if "MCP_MAX_SQL" in p] == [
        "MCP_MAX_SQL_CHARS=999: a statement must be allowed 1000 characters or more",
        "MCP_MAX_SQL_NESTING=7: a statement must be allowed 8 nested brackets or more",
    ]
    monkeypatch.setenv("MCP_MAX_SQL_CHARS", "50000")
    monkeypatch.setenv("MCP_MAX_SQL_NESTING", "10")
    check_config(Config.from_env())
    assert (
        "agent SQL: up to 50000 characters, brackets nested 10 deep at most, refused past "
        "either before it is parsed; parsed by 2 worker processes within 2 s, one statement "
        "at a time per caller"
    ) in capsys.readouterr().out


def test_parse_worker_settings_validated_and_in_check(monkeypatch, capsys):
    """MCP_SQL_PARSE_SECONDS (2, above 0) and MCP_SQL_PARSE_WORKERS (2, 1 at least)."""
    from akko_mcp_trino.__main__ import check_config

    c = Config.from_env()
    assert (c.sql_parse_seconds, c.sql_parse_workers) == (2.0, 2)
    monkeypatch.setenv("MCP_SQL_PARSE_SECONDS", "0.5")
    monkeypatch.setenv("MCP_SQL_PARSE_WORKERS", "1")
    c = Config.from_env()
    assert (c.sql_parse_seconds, c.sql_parse_workers) == (0.5, 1)
    assert not [p for p in c.problems() if "MCP_SQL_PARSE" in p]
    check_config(c)
    assert "parsed by 1 worker processes within 0.5 s" in capsys.readouterr().out
    monkeypatch.setenv("MCP_SQL_PARSE_SECONDS", "0")
    monkeypatch.setenv("MCP_SQL_PARSE_WORKERS", "0")
    assert [p for p in Config.from_env().problems() if "MCP_SQL_PARSE" in p] == [
        "MCP_SQL_PARSE_SECONDS=0: the parse of a statement must be given more than 0 seconds",
        "MCP_SQL_PARSE_WORKERS=0: statements are parsed by 1 worker process or more",
    ]
    monkeypatch.setenv("MCP_SQL_PARSE_SECONDS", "-1")
    assert any("MCP_SQL_PARSE_SECONDS=-1" in p for p in Config.from_env().problems())
