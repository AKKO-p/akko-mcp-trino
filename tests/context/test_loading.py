"""Provider loading, the check lines and the start of the context machinery."""

import dataclasses
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest

from akko_mcp_trino import server as server_module
from akko_mcp_trino.__main__ import check_config, parse_args
from akko_mcp_trino.config import Config
from akko_mcp_trino.context import CONTEXT_API, ContextChain, ContextLoop, ContextSetup
from akko_mcp_trino.context import loading as loading_module
from akko_mcp_trino.context.loading import ENTRY_POINT_GROUP, load_providers
from akko_mcp_trino.context.secrets import secret_setting
from akko_mcp_trino.server import build_server
from tests.test_app import _JWT_ENV, _clean_identity_env
from tests.test_core_alone import _config
from tests.test_tools import FakeMCP


class _EP:
    def __init__(self, name, factory):
        self.name, self._factory = name, factory

    def load(self):
        return self._factory


class _Provider:
    context_api = CONTEXT_API

    def __init__(self, name="catalog", lines=(), api=CONTEXT_API):
        self.name = name
        self.context_api = api
        self.lines = list(lines)
        self.closed = False

    def maps(self, catalog):
        return True

    def served_sets(self):
        return {}

    async def describe(self, refs, *, deadline, on_behalf_of=None):
        return {}

    async def terms(self, keys, *, deadline):
        return {}

    def check_lines(self):
        return self.lines

    async def aclose(self):
        self.closed = True

    def credentials_stamp(self):
        return ""


def _install(monkeypatch, **factories):
    monkeypatch.setattr(
        loading_module, "_entry_points", lambda: [_EP(n, f) for n, f in factories.items()]
    )


def test_without_providers_nothing_is_started(monkeypatch):
    monkeypatch.setattr(loading_module, "_entry_points", lambda: pytest.fail("looked up"))
    assert load_providers(_config(), {}) == []
    before = {t.ident for t in threading.enumerate()}
    seen = []
    mcp, _ = build_server(
        _config(),
        mcp_factory=lambda name, **_: FakeMCP(),
        context_started=seen.append,
    )
    assert {t.ident for t in threading.enumerate()} == before
    assert seen == [] and "mcp-context" not in {t.name for t in threading.enumerate()}


def test_providers_are_built_in_order_from_the_environment(monkeypatch):
    built = []

    def factory(name):
        def make(env):
            built.append((name, env["X"]))
            return _Provider(name)

        return make

    _install(monkeypatch, a=factory("a"), b=factory("b"))
    got = load_providers(_config(context_providers="b, a"), {"X": "1"})
    assert [p.name for p in got] == ["b", "a"] and built == [("b", "1"), ("a", "1")]


def test_an_unknown_provider_name_refuses_to_start(monkeypatch):
    _install(monkeypatch, real=lambda env: _Provider("real"))
    with pytest.raises(ValueError, match=r"unknown context provider 'typo'.*\['real'\]"):
        load_providers(_config(context_providers="typo"), {})
    _install(monkeypatch)
    with pytest.raises(ValueError, match="installed providers: none"):
        load_providers(_config(context_providers="typo"), {})


def test_a_provider_of_another_context_api_refuses_to_start(monkeypatch):
    _install(monkeypatch, old=lambda env: _Provider("old", api=CONTEXT_API + 1))
    with pytest.raises(ValueError, match=r"'old'.*context_api 2.*expects 1"):
        load_providers(_config(context_providers="old"), {})


def test_a_provider_factory_value_error_refuses_to_start(monkeypatch):
    def broken(env):
        raise ValueError("CATALOG_URL is not set")

    _install(monkeypatch, catalog=broken)
    with pytest.raises(ValueError, match="CATALOG_URL is not set"):
        load_providers(_config(context_providers="catalog"), {})


def test_a_provider_named_twice_refuses_to_start(monkeypatch):
    _install(monkeypatch, catalog=lambda env: _Provider())
    with pytest.raises(ValueError, match="'catalog' is listed twice"):
        load_providers(_config(context_providers="catalog,catalog"), {})


def test_two_providers_reporting_the_same_name_refuse_to_start(monkeypatch):
    _install(monkeypatch, a=lambda env: _Provider("same"), b=lambda env: _Provider("same"))
    with pytest.raises(ValueError, match="more than one context provider reports the name 'same'"):
        load_providers(_config(context_providers="a,b"), {})


def test_a_list_of_nothing_loads_nothing(monkeypatch):
    monkeypatch.setattr(loading_module, "_entry_points", lambda: pytest.fail("looked up"))
    assert load_providers(_config(context_providers=" , "), {}) == []


def test_an_unreachable_catalogue_does_not_stop_the_load(monkeypatch):
    _install(monkeypatch, catalog=lambda env: _Provider(lines=["UNREACHABLE: down"]))
    assert len(load_providers(_config(context_providers="catalog"), {})) == 1


def test_the_real_entry_point_group_is_read(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        loading_module, "entry_points", lambda group: seen.setdefault("g", group) and []
    )
    with pytest.raises(ValueError):
        load_providers(_config(context_providers="x"), {})
    assert seen["g"] == ENTRY_POINT_GROUP == "akko_mcp_trino.context"


@pytest.fixture
def loops(monkeypatch):
    """The loops the server starts, closed after the test so that no thread outlives it."""
    made: list[ContextLoop] = []

    class Recording(ContextLoop):
        def __init__(self):
            super().__init__()
            made.append(self)

    monkeypatch.setattr(server_module, "ContextLoop", Recording)
    yield made
    for loop in made:
        loop.close()


def test_providers_given_to_the_server_start_the_loop_and_the_chain(loops):
    seen: list[ContextSetup] = []
    config = _config()
    mcp, _ = build_server(
        config,
        mcp_factory=lambda name, **_: FakeMCP(),
        context_providers=[_Provider()],
        context_started=seen.append,
    )
    (setup,) = seen
    assert isinstance(setup.chain, ContextChain) and setup.config is config
    assert setup.chain.names == ("catalog",)
    assert len(loops) == 1 and "mcp-context" in {t.name for t in threading.enumerate()}


def test_a_registrar_that_fails_leaves_no_loop_and_no_callback(loops):
    def broken(mcp, client):
        raise RuntimeError("registrar failed")

    seen = []
    with pytest.raises(RuntimeError, match="registrar failed"):
        build_server(
            _config(),
            mcp_factory=lambda name, **_: FakeMCP(),
            extra_tool_registrars=[broken],
            context_providers=[_Provider()],
            context_started=seen.append,
        )
    assert seen == [] and "mcp-context" not in {t.name for t in threading.enumerate()}


def test_providers_without_a_callback_still_start(loops):
    mcp, _ = build_server(
        _config(), mcp_factory=lambda name, **_: FakeMCP(), context_providers=[_Provider()]
    )
    assert set(mcp.tools)


def test_the_settings_of_the_chain_come_from_the_configuration():
    from akko_mcp_trino.context import ContextSettings

    config = replace(
        _config(),
        context_timeout_seconds=7.0,
        context_reserve_seconds=1.5,
        context_max_concurrency=3,
        context_retry_after_seconds=4.0,
        context_refused_retry_seconds=5.0,
        context_batch=6,
        context_ttl_seconds=8.0,
        context_cache_bytes=9,
        context_terms_cache_bytes=10,
    )
    assert ContextSettings.from_config(config) == ContextSettings(
        timeout_seconds=7.0,
        reserve_seconds=1.5,
        max_concurrency=3,
        retry_after_seconds=4.0,
        refused_retry_seconds=5.0,
        batch=6,
        ttl_seconds=8.0,
        cache_bytes=9,
        terms_cache_bytes=10,
    )


def test_a_server_refuses_a_visibility_switch_turned_off():
    with pytest.raises(ValueError, match="MCP_VISIBILITY_ENABLED was removed in 0.8"):
        build_server(_config(visibility_switch="false"), mcp_factory=lambda name, **_: FakeMCP())


# ---- --check


def _check_env(monkeypatch, **extra):
    _clean_identity_env(monkeypatch)
    for name, value in _JWT_ENV.items():
        monkeypatch.setenv(name, value)
    for name in ("MCP_CONTEXT_PROVIDERS", "MCP_CONTEXT_CANARY", "MCP_CONTEXT_REQUIRE_SELECT"):
        monkeypatch.delenv(name, raising=False)
    for name, value in extra.items():
        monkeypatch.setenv(name, value)


def test_check_prints_the_context_lines_and_never_a_secret(monkeypatch, capsys, tmp_path):
    marker = "marker-secret-value"
    secret_file = tmp_path / "token"
    secret_file.write_text(marker, encoding="utf-8")
    monkeypatch.setenv("CATALOG_TOKEN_FILE", str(secret_file))

    def make(env):
        token = secret_setting(env, "CATALOG_TOKEN")
        return _Provider(lines=[f"catalog: token {'set' if token else '-'}"])

    _install(monkeypatch, catalog=make)
    _check_env(
        monkeypatch,
        MCP_CONTEXT_PROVIDERS="catalog",
        MCP_CONTEXT_CANARY="sales.crm.accounts",
        MCP_CONTEXT_REQUIRE_SELECT="true",
    )
    assert check_config(Config.from_env()) == 0
    out = capsys.readouterr().out
    assert (
        "context: catalog (timeout 5 s, reserve 0.5 s, 32 reads in flight, retry after 30 s, "
        "after a refusal 3600 s, batch 50, ttl 900 s, caches 134217728 + 16777216 bytes, "
        "text 500 characters, response 60000 characters, 500 columns)"
    ) in out
    assert (
        "visibility: ttl 60 s, cache 33554432 bytes, 10000 names, canary set, require select true"
    ) in out
    assert "catalog: token set" in out
    assert marker not in out
    assert out.index("context: catalog") < out.index("visibility:") < out.index("catalog: token")


def test_check_of_the_defaults_says_no_provider(monkeypatch, capsys):
    monkeypatch.setattr(loading_module, "_entry_points", lambda: pytest.fail("looked up"))
    _check_env(monkeypatch)
    assert check_config(Config.from_env()) == 0
    out = capsys.readouterr().out
    assert "context: none (" in out
    assert "visibility: ttl 60 s, cache 33554432 bytes, 10000 names, canary -, " in out
    assert "require select false" in out


def test_check_refuses_what_loading_refuses(monkeypatch, capsys):
    _install(monkeypatch)
    _check_env(monkeypatch, MCP_CONTEXT_PROVIDERS="typo")
    assert check_config(Config.from_env()) == 2
    assert "PROBLEM: unknown context provider 'typo'" in capsys.readouterr().out


def test_check_closes_the_providers_it_built(monkeypatch, capsys):
    provider = _Provider()
    _install(monkeypatch, catalog=lambda env: provider)
    _check_env(monkeypatch, MCP_CONTEXT_PROVIDERS="catalog")
    check_config(Config.from_env())
    assert provider.closed


def test_check_survives_a_provider_that_cannot_close(monkeypatch, capsys):
    provider = _Provider()

    async def boom():
        raise RuntimeError("no")

    provider.aclose = boom
    _install(monkeypatch, catalog=lambda env: provider)
    _check_env(monkeypatch, MCP_CONTEXT_PROVIDERS="catalog")
    assert check_config(Config.from_env()) == 0


def test_check_strict_fails_when_a_provider_is_unreachable(monkeypatch, capsys):
    _install(monkeypatch, catalog=lambda env: _Provider(lines=["UNREACHABLE: connection refused"]))
    _check_env(monkeypatch, MCP_CONTEXT_PROVIDERS="catalog")
    config = Config.from_env()
    assert check_config(config) == 0
    assert "UNREACHABLE: connection refused" in capsys.readouterr().out
    assert check_config(config, strict=True) == 2
    assert "PROBLEM: UNREACHABLE: connection refused" in capsys.readouterr().out


def test_check_strict_passes_when_every_provider_is_reachable(monkeypatch, capsys):
    _install(monkeypatch, catalog=lambda env: _Provider(lines=["catalog: reachable"]))
    _check_env(monkeypatch, MCP_CONTEXT_PROVIDERS="catalog")
    assert check_config(Config.from_env(), strict=True) == 0


def test_the_strict_argument_is_parsed():
    assert parse_args(["--check", "--strict"]).strict is True
    assert parse_args(["--check"]).strict is False


def test_the_visibility_switch_is_a_problem_in_check(monkeypatch, capsys):
    _check_env(monkeypatch, MCP_VISIBILITY_ENABLED="false")
    assert check_config(Config.from_env()) == 2
    assert "MCP_VISIBILITY_ENABLED was removed in 0.8" in capsys.readouterr().out


def test_the_setup_is_frozen():
    setup = ContextSetup(chain=SimpleNamespace(), config=_config(), loop=ContextLoop())
    assert [f.name for f in dataclasses.fields(setup)] == ["chain", "config", "loop"]
    with pytest.raises(dataclasses.FrozenInstanceError):
        setup.chain = None  # type: ignore[misc]


# ---- a factory that fails otherwise than with a ValueError


def test_a_provider_that_cannot_be_imported_refuses_to_start_naming_only_the_type(monkeypatch):
    first = _Provider("first")

    class _Broken:
        name = "broken"

        def load(self):
            raise ImportError("secret path /etc/token")

    monkeypatch.setattr(
        loading_module,
        "_entry_points",
        lambda: [_EP("first", lambda env: first), _Broken()],
    )
    with pytest.raises(ValueError) as caught:
        load_providers(_config(context_providers="first,broken"), {})
    message = str(caught.value)
    assert "'broken'" in message and "ImportError" in message and "secret" not in message
    assert first.closed


def test_a_factory_that_raises_a_type_error_refuses_to_start_naming_only_the_type(monkeypatch):
    def broken(env):
        raise TypeError("password=hunter2")

    _install(monkeypatch, catalog=broken)
    with pytest.raises(ValueError) as caught:
        load_providers(_config(context_providers="catalog"), {})
    assert "TypeError" in str(caught.value) and "hunter2" not in str(caught.value)


def test_a_failing_load_closes_the_providers_built_before_it(monkeypatch):
    first = _Provider("first")

    def broken(env):
        raise ValueError("no")

    _install(monkeypatch, first=lambda env: first, broken=broken)
    with pytest.raises(ValueError, match="no"):
        load_providers(_config(context_providers="first,broken"), {})
    assert first.closed


def test_a_failing_load_survives_a_provider_that_cannot_close(monkeypatch):
    first = _Provider("first")

    async def boom():
        raise RuntimeError("no")

    first.aclose = boom

    def broken(env):
        raise ValueError("stop")

    _install(monkeypatch, first=lambda env: first, broken=broken)
    with pytest.raises(ValueError, match="stop"):
        load_providers(_config(context_providers="first,broken"), {})


def test_check_names_a_provider_that_fails_to_load_and_closes_the_others(monkeypatch, capsys):
    first = _Provider("first")

    def broken(env):
        raise TypeError("password=hunter2")

    _install(monkeypatch, first=lambda env: first, broken=broken)
    _check_env(monkeypatch, MCP_CONTEXT_PROVIDERS="first,broken")
    assert check_config(Config.from_env()) == 2
    out = capsys.readouterr().out
    assert "PROBLEM: context provider 'broken' failed to load (TypeError)" in out
    assert "hunter2" not in out and first.closed


def test_closing_the_setup_closes_every_provider_then_the_loop(loops):
    order: list[str] = []

    class Closing(_Provider):
        async def aclose(self):
            order.append(f"{self.name} on {threading.current_thread().name}")
            raise RuntimeError("already closed")

    seen: list[ContextSetup] = []
    build_server(
        _config(),
        mcp_factory=lambda name, **_: FakeMCP(),
        context_providers=[Closing("first"), Closing("second")],
        context_started=seen.append,
    )
    (setup,) = seen
    setup.close()
    assert sorted(order) == ["first on mcp-context", "second on mcp-context"]
    assert "mcp-context" not in {t.name for t in threading.enumerate()}
    setup.close()
    assert len(order) == 2, "a second close reaches nothing"


def test_check_prints_every_context_setting_and_never_a_secret(monkeypatch, capsys, tmp_path):
    marker = "s3cr3t-T0KEN-value"
    secret_file = tmp_path / "token"
    secret_file.write_text(marker, encoding="utf-8")
    monkeypatch.setenv("CATALOG_TOKEN_FILE", str(secret_file))

    def make(env):
        token = secret_setting(env, "CATALOG_TOKEN")
        return _Provider(lines=[f"catalog: token {'set' if token else '-'}"])

    _install(monkeypatch, catalog=make)
    _check_env(
        monkeypatch,
        MCP_CONTEXT_PROVIDERS="catalog",
        MCP_CONTEXT_TIMEOUT_SECONDS="7",
        MCP_CONTEXT_RESERVE_SECONDS="0.75",
        MCP_CONTEXT_MAX_CONCURRENCY="11",
        MCP_CONTEXT_RETRY_AFTER_SECONDS="13",
        MCP_CONTEXT_REFUSED_RETRY_SECONDS="1700",
        MCP_CONTEXT_BATCH="17",
        MCP_CONTEXT_TTL_SECONDS="190",
        MCP_CONTEXT_CACHE_BYTES="2300",
        MCP_CONTEXT_TERMS_CACHE_BYTES="2900",
        MCP_CONTEXT_TEXT_CHARS="310",
        MCP_CONTEXT_RESPONSE_CHARS="3700",
        MCP_CONTEXT_MAX_COLUMNS="410",
        MCP_VISIBILITY_TTL_SECONDS="43",
        MCP_VISIBILITY_CACHE_BYTES="4700",
        MCP_VISIBILITY_MAX_NAMES="530",
        MCP_CONTEXT_CANARY="sales.crm.accounts",
        MCP_CONTEXT_REQUIRE_SELECT="true",
    )
    assert check_config(Config.from_env()) == 0
    out = capsys.readouterr().out
    assert (
        "context: catalog (timeout 7 s, reserve 0.75 s, 11 reads in flight, retry after "
        "13 s, after a refusal 1700 s, batch 17, ttl 190 s, caches 2300 + 2900 bytes, "
        "text 310 characters, response 3700 characters, 410 columns)"
    ) in out
    assert (
        "visibility: ttl 43 s, cache 4700 bytes, 530 names, canary set, require select true"
    ) in out
    assert "catalog: token set" in out
    assert marker not in out and "accounts" not in out
