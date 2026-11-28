"""Settings read strictly: a boolean is ``true`` or ``false`` and nothing else, a number of
seconds is finite, a TLS setting is never a boolean look-alike, and the JWKS bounds hold.

A value outside those is a startup error and a ``PROBLEM`` of ``--check`` that names the
variable, the value received and the values allowed. Reading ``== "true"`` would turn
``TRINO_READ_ONLY=flase`` into writes allowed and ``MCP_AUTH_ENABLED=1`` into no
authentication, silently: these tests fail if that reading comes back."""

import pytest

from akko_mcp_trino.config import Config, read_flag

# Every boolean the server reads, with its default.
BOOLEANS = {
    "TRINO_READ_ONLY": True,
    "MCP_AUTH_ENABLED": True,
    "MCP_AUTH_REQUIRED": True,
    "MCP_CLEAN_CELLS": False,
    "MCP_CONTEXT_REQUIRE_SELECT": False,
}
FIELDS = {
    "TRINO_READ_ONLY": "read_only",
    "MCP_AUTH_ENABLED": "auth_enabled",
    "MCP_AUTH_REQUIRED": "auth_required",
    "MCP_CLEAN_CELLS": "clean_cells",
    "MCP_CONTEXT_REQUIRE_SELECT": "context_require_select",
}
JWKS = ("MCP_JWKS_TIMEOUT_SECONDS", "MCP_JWKS_CACHE_SECONDS", "MCP_JWKS_MIN_REFRESH_SECONDS")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in (*BOOLEANS, *JWKS, "TRINO_VERIFY", "TRINO_TOKEN_EXCHANGE_VERIFY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("MCP_CONTEXT_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("MCP_CALL_TIMEOUT_SECONDS", raising=False)


def _message(name: str, raw: str) -> str:
    return f"{name}={raw!r} is not a boolean: set true or false (case does not matter)"


@pytest.mark.parametrize("name", BOOLEANS)
@pytest.mark.parametrize("raw", ["true", "TRUE", " True ", "false", "FALSE", " False\t"])
def test_true_and_false_are_read_whatever_the_case_and_spaces(monkeypatch, name, raw):
    monkeypatch.setenv(name, raw)
    config = Config.from_env()
    assert getattr(config, FIELDS[name]) is (raw.strip().lower() == "true")
    assert not [p for p in config.problems() if p.startswith(f"{name}=")]


@pytest.mark.parametrize("name", BOOLEANS)
def test_an_unset_boolean_takes_its_default(name):
    config = Config.from_env()
    assert getattr(config, FIELDS[name]) is BOOLEANS[name]
    assert config.env_problems == ()


@pytest.mark.parametrize("name", BOOLEANS)
@pytest.mark.parametrize("raw", ["flase", "ture", "1", "0", "yes", "no", "on", "", "truee"])
def test_any_other_value_refuses_to_start_and_names_the_variable(monkeypatch, name, raw):
    """``TRINO_READ_ONLY=flase``, ``MCP_AUTH_ENABLED=1``, ``MCP_AUTH_REQUIRED=ture``: before,
    each read as false without a word; now each is a problem, and the setting keeps its
    default rather than a guess."""
    monkeypatch.setenv(name, raw)
    config = Config.from_env()
    assert _message(name, raw) in config.problems()
    assert getattr(config, FIELDS[name]) is BOOLEANS[name]


def test_check_says_the_problem_and_exits_2(monkeypatch, capsys):
    from akko_mcp_trino.__main__ import check_config

    monkeypatch.setenv("TRINO_READ_ONLY", "flase")
    assert check_config(Config.from_env()) == 2
    out = capsys.readouterr().out
    assert (
        "PROBLEM: TRINO_READ_ONLY='flase' is not a boolean: set true or false "
        "(case does not matter)"
    ) in out


def test_the_server_refuses_to_start(monkeypatch):
    from akko_mcp_trino.server import build_server

    monkeypatch.setenv("MCP_AUTH_ENABLED", "1")
    with pytest.raises(ValueError, match="MCP_AUTH_ENABLED='1' is not a boolean"):
        build_server(Config.from_env())


def test_read_flag_collects_every_bad_value():
    invalid: list[str] = []
    assert read_flag("A", "yes", True, invalid) is True
    assert read_flag("B", "no", False, invalid) is False
    assert read_flag("C", None, True, invalid) is True
    assert invalid == [_message("A", "yes"), _message("B", "no")]


@pytest.mark.parametrize("name", ["TRINO_VERIFY", "TRINO_TOKEN_EXCHANGE_VERIFY"])
@pytest.mark.parametrize("raw", ["1", "0", "yes", "NO", " off "])
def test_a_tls_setting_refuses_a_boolean_look_alike(monkeypatch, name, raw):
    monkeypatch.setenv(name, raw)
    assert (
        f"{name}={raw!r}: set true, false or the path of a CA bundle (case does not matter)"
        in Config.from_env().problems()
    )


@pytest.mark.parametrize("name", ["TRINO_VERIFY", "TRINO_TOKEN_EXCHANGE_VERIFY"])
@pytest.mark.parametrize("raw", ["true", "FALSE", "/etc/ssl/ca.pem"])
def test_a_tls_setting_takes_true_false_or_a_path(monkeypatch, name, raw):
    monkeypatch.setenv(name, raw)
    assert not [p for p in Config.from_env().problems() if p.startswith(f"{name}=")]


@pytest.mark.parametrize("raw", ["nan", "inf", "-inf"])
@pytest.mark.parametrize("name", ["MCP_CONTEXT_TIMEOUT_SECONDS", "MCP_CALL_TIMEOUT_SECONDS", *JWKS])
def test_a_number_of_seconds_is_finite(monkeypatch, name, raw):
    """nan compares false to everything, so ``value < 0`` would let it through every bound."""
    monkeypatch.setenv(name, raw)
    assert any(
        p.startswith(f"{name}=") and "a number of seconds is a finite number" in p
        for p in Config.from_env().problems()
    )


def test_the_jwks_defaults_are_short_and_bounded():
    config = Config.from_env()
    assert (
        config.jwks_timeout_seconds,
        config.jwks_cache_seconds,
        config.jwks_min_refresh_seconds,
    ) == (5.0, 300.0, 30.0)
    assert not [p for p in config.problems() if "JWKS" in p]


@pytest.mark.parametrize(
    ("settings", "problem"),
    [
        (
            {"MCP_JWKS_TIMEOUT_SECONDS": "0"},
            "MCP_JWKS_TIMEOUT_SECONDS=0: a read of the JWKS needs more than 0 seconds",
        ),
        (
            {"MCP_JWKS_MIN_REFRESH_SECONDS": "0"},
            "MCP_JWKS_MIN_REFRESH_SECONDS=0: the JWKS is read again at most once a second, or "
            "every token naming an unknown key would cause a read; set 1 or more",
        ),
        (
            {"MCP_JWKS_CACHE_SECONDS": "10", "MCP_JWKS_MIN_REFRESH_SECONDS": "60"},
            "MCP_JWKS_CACHE_SECONDS=10 is below MCP_JWKS_MIN_REFRESH_SECONDS=60: the keys "
            "cannot be read again sooner than that; set it to that or more",
        ),
    ],
)
def test_the_jwks_bounds_refuse_to_start(monkeypatch, settings, problem):
    for name, value in settings.items():
        monkeypatch.setenv(name, value)
    assert problem in Config.from_env().problems()


def test_the_jwks_settings_are_read(monkeypatch):
    monkeypatch.setenv("MCP_JWKS_TIMEOUT_SECONDS", "2.5")
    monkeypatch.setenv("MCP_JWKS_CACHE_SECONDS", "600")
    monkeypatch.setenv("MCP_JWKS_MIN_REFRESH_SECONDS", "10")
    config = Config.from_env()
    assert (
        config.jwks_timeout_seconds,
        config.jwks_cache_seconds,
        config.jwks_min_refresh_seconds,
    ) == (2.5, 600.0, 10.0)
    assert not [p for p in config.problems() if "JWKS" in p]
