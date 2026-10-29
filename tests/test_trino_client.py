"""akko_mcp_trino.trino_client with a mocked connection: no real Trino involved."""

import logging
from unittest.mock import Mock

import pytest

from akko_mcp_trino import trino_client as tc
from akko_mcp_trino.__main__ import check_config
from akko_mcp_trino.config import Config
from akko_mcp_trino.identity import reset_current_bearer, set_current_bearer
from akko_mcp_trino.trino_client import (
    ImpersonationIdentity,
    JwtIdentity,
    ServiceIdentity,
    TrinoClient,
    resolve_identity,
)

_CFG = Config(
    trino_host="h",
    trino_identity_mode="service",
    query_timeout_seconds=0.0,
    call_timeout_seconds=0.0,
    trino_port=8080,
    trino_user="svc",
    trino_catalog="cat",
    max_rows=2,
    read_only=True,
    auth_enabled=False,
    server_name="x",
    health_port=3001,
    jwks_url="",
    oidc_issuer="",
    oidc_audience="",
    transport="sse",
    mcp_port=3000,
    auth_required=False,
)


class _Cur:
    description = [("a",), ("b",)]

    def __init__(self, rows):
        self._rows = rows
        self.executed = None

    def execute(self, sql, params=None):
        self.executed = (sql, params)

    def fetchmany(self, n):
        return self._rows[:n]


class _Conn:
    def __init__(self, rows):
        self._cur = _Cur(rows)

    def cursor(self):
        return self._cur


class _AsCaller:
    """A stand-in identity mode for the mechanics of a query (rows, bounds, budgets): the
    caller, no authentication. The core's own mode, ``service``, is tested on its own."""

    def __init__(self, cfg):
        self._cfg = cfg

    def describe(self):
        return []

    def connection(self, user):
        return user or self._cfg.trino_user, None

    def ping(self, client):
        client.query("SELECT 1")


def _client(cfg, **kw):
    return TrinoClient(cfg, identity=_AsCaller(cfg), **kw)


def _patch_connect(monkeypatch, rows, capture):
    import akko_mcp_trino.trino_client as tc

    def fake_connect(**kwargs):
        capture.update(kwargs)
        return _Conn(rows)

    monkeypatch.setattr(tc.trino.dbapi, "connect", fake_connect)


def test_query_returns_columns_rows_count_bounded(monkeypatch):
    cap = {}
    _patch_connect(monkeypatch, [(1, 2), (3, 4), (5, 6)], cap)
    out = _client(_CFG).query("SELECT * FROM t")
    assert out == {
        "columns": ["a", "b"],
        "rows": [[1, 2], [3, 4]],
        "row_count": 2,
        "truncated": True,
    }  # capped at max_rows=2, and it says so


def test_query_uses_config_identity_by_default(monkeypatch):
    cap = {}
    _patch_connect(monkeypatch, [], cap)
    _client(_CFG).query("SELECT 1")
    assert {k: cap[k] for k in ("host", "port", "user", "catalog")} == {
        "host": "h",
        "port": 8080,
        "user": "svc",
        "catalog": "cat",
    }


def test_query_propagates_explicit_user(monkeypatch):
    cap = {}
    _patch_connect(monkeypatch, [], cap)
    _client(_CFG).query("SELECT 1", user="dave_steward")
    assert cap["user"] == "dave_steward"  # the identity mode decides the Trino user


def test_query_no_description_returns_empty_columns(monkeypatch):
    import akko_mcp_trino.trino_client as tc

    class _NoDescCur(_Cur):
        description = None

    monkeypatch.setattr(
        tc.trino.dbapi,
        "connect",
        lambda **k: type("C", (), {"cursor": lambda self: _NoDescCur([])})(),
    )
    out = _client(_CFG).query("SET x=1")
    assert out["columns"] == [] and out["rows"] == []


# ---- HTTPS, TLS verification, timeout, identity modes


def _capture(monkeypatch):
    import akko_mcp_trino.trino_client as tc

    seen = {}

    def connect(**kw):
        seen.update(kw)
        return type("C", (), {"cursor": lambda self: _Cur([])})()

    monkeypatch.setattr(tc.trino.dbapi, "connect", connect)
    return seen


def _cfg(**over):
    return Config(**{**_CFG.__dict__, **over})


def test_the_connection_carries_what_the_identity_mode_says(monkeypatch):
    seen = _capture(monkeypatch)
    _client(_CFG).query("SELECT 1", user="alice")
    assert seen["http_scheme"] == "http" and seen["auth"] is None
    assert seen["user"] == "alice", "the stand-in mode names the caller"
    assert seen["verify"] is True and seen["request_timeout"] == 30.0


def test_the_core_has_four_modes_and_an_extension_may_add_or_replace_one():
    import pytest

    assert isinstance(resolve_identity(_cfg(trino_identity_mode="service")), ServiceIdentity)
    assert isinstance(resolve_identity(_jwt_cfg()), JwtIdentity)
    assert isinstance(resolve_identity(_imp_cfg()), ImpersonationIdentity)
    with pytest.raises(
        ValueError, match="expected one of \\('impersonation', 'jwt', 'oauth2', 'service'\\)"
    ):
        resolve_identity(_cfg(trino_identity_mode="per-user"))

    class _Ext:
        def identity_modes(self):
            return {"mine": _AsCaller(_CFG)}

    assert isinstance(resolve_identity(_cfg(trino_identity_mode="mine"), [_Ext()]), _AsCaller)


def test_tls_verification_can_point_at_a_ca_bundle_or_be_disabled(monkeypatch):
    seen = _capture(monkeypatch)
    _client(_cfg(trino_http_scheme="https", trino_verify="/etc/ssl/ca.pem")).query("SELECT 1")
    assert seen["verify"] == "/etc/ssl/ca.pem"
    _client(_cfg(trino_http_scheme="https", trino_verify="false")).query("SELECT 1")
    assert seen["verify"] is False


def test_request_timeout_is_configurable(monkeypatch):
    seen = _capture(monkeypatch)
    _client(_cfg(trino_request_timeout=5.5)).query("SELECT 1")
    assert seen["request_timeout"] == 5.5


def test_service_mode_runs_every_query_as_the_configured_identity_whoever_calls(monkeypatch):
    """``service``: Trino sees the identity the administrator configured, the same for
    every caller."""
    seen = _capture(monkeypatch)
    TrinoClient(_CFG).query("SELECT 1", user="alice")
    assert seen["user"] == "svc" and seen["auth"] is None


def test_service_mode_sends_the_password_by_basic_auth_over_https(monkeypatch):
    import trino.auth

    seen = _capture(monkeypatch)
    cfg = _cfg(trino_http_scheme="https", trino_password="s3cret")
    TrinoClient(cfg).query("SELECT 1", user="alice")
    assert isinstance(seen["auth"], trino.auth.BasicAuthentication)
    assert seen["user"] == "svc"


def test_service_mode_refuses_a_password_over_plain_http(monkeypatch):
    import pytest

    _capture(monkeypatch)
    cfg = _cfg(trino_password="s3cret")
    with pytest.raises(ValueError, match="TRINO_HTTP_SCHEME"):
        resolve_identity(cfg)
    with pytest.raises(ValueError, match="TRINO_HTTP_SCHEME"):
        TrinoClient(_CFG, identity=ServiceIdentity(cfg)).query("SELECT 1")


def test_service_mode_says_whether_a_password_is_set_never_which():
    with_password = ServiceIdentity(_cfg(trino_http_scheme="https", trino_password="s3cret"))
    assert with_password.describe() == [
        "trino identity: svc (password set), the same for every caller"
    ]
    assert "s3cret" not in " ".join(with_password.describe())
    assert ServiceIdentity(_CFG).describe() == [
        "trino identity: svc (no password), the same for every caller"
    ]


def test_unknown_identity_mode_is_refused(monkeypatch):
    import pytest

    _capture(monkeypatch)
    with pytest.raises(ValueError):
        TrinoClient(_cfg(trino_identity_mode="magic")).query("SELECT 1")


def test_ping_runs_a_bounded_select_1_as_the_configured_identity(monkeypatch):
    import akko_mcp_trino.trino_client as tc

    conn, cap = _Conn([(1,)]), {}
    monkeypatch.setattr(tc.trino.dbapi, "connect", lambda **kw: cap.update(kw) or conn)
    TrinoClient(_CFG).ping()
    assert conn._cur.executed == ("SELECT 1", None) and cap["user"] == "svc"


@pytest.mark.parametrize(
    "transport, scheme, message",
    [
        ("sse", "https", "stdio"),
        ("streamable-http", "https", "stdio"),
        ("stdio", "http", "https"),
    ],
)
def test_oauth2_refuses_http_transports_and_plain_http(transport, scheme, message):
    cfg = _cfg(trino_identity_mode="oauth2", transport=transport, trino_http_scheme=scheme)
    with pytest.raises(ValueError, match=message):
        resolve_identity(cfg)


def test_oauth2_reuses_auth_and_keeps_redirects_off_stdout(monkeypatch, capsys):
    cfg = _cfg(trino_identity_mode="oauth2", transport="stdio", trino_http_scheme="https")
    auth, browser, seen = Mock(), Mock(), {}
    monkeypatch.setattr(tc.trino.auth, "OAuth2Authentication", auth)
    monkeypatch.setattr(tc.webbrowser, "open_new", browser)
    monkeypatch.setattr(logging.getLogger(tc.__name__), "handlers", [logging.StreamHandler()])
    _patch_connect(monkeypatch, [], seen)
    assert check_config(cfg) == 0
    auth.assert_not_called()
    client = TrinoClient(cfg)
    client.query("SELECT 1", user="ignored")
    client.ping()
    auth.assert_called_once()
    assert seen["auth"] is auth.return_value and seen["user"] is None
    capsys.readouterr()  # Discard --check's intentional stdout output.
    redirect = auth.call_args.kwargs["redirect_auth_url_handler"]
    assert isinstance(redirect, tc.trino.auth.CompositeRedirectHandler)
    redirect("https://trino.example/login")
    browser.assert_called_once_with("https://trino.example/login")
    captured = capsys.readouterr()
    assert captured.out == "" and "https://trino.example/login" in captured.err


# ---- jwt: the caller's own token, verified again by Trino


def _as_bearer(token):
    """Run the body with ``token`` bound as the verified caller's bearer, as the door does."""
    import contextlib

    @contextlib.contextmanager
    def bound():
        t = set_current_bearer(token)
        try:
            yield
        finally:
            reset_current_bearer(t)

    return bound()


class _Exchange:
    """A stand-in for the token exchange: what it was asked, what it answers."""

    def __init__(self, answer="for-trino"):
        self.asked, self.answer = [], answer

    def describe(self):
        return ["token exchange: stand-in"]

    def token_for(self, subject_token):
        self.asked.append(subject_token)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def _jwt_cfg(**over):
    base = dict(
        trino_identity_mode="jwt",
        trino_http_scheme="https",
        auth_enabled=True,
        auth_required=True,
        token_exchange_url="https://idp/token",
        token_exchange_client_id="mcp-server",
        token_exchange_client_secret="s3cret",
        token_exchange_audience="trino",
    )
    return _cfg(**{**base, **over})


def _imp_cfg(**over):
    base = dict(
        trino_identity_mode="impersonation",
        trino_http_scheme="https",
        auth_enabled=True,
        auth_required=True,
        trino_password="s3cret",
    )
    return _cfg(**{**base, **over})


def test_jwt_mode_sends_trino_an_exchanged_token_never_the_callers_own(monkeypatch):
    """The MCP authorization specification forbids token passthrough: Trino receives the
    token the identity provider issued for it in exchange for the caller's, and the query
    runs as the caller."""
    import trino.auth

    seen = _capture(monkeypatch)
    exchange = _Exchange("eyJ.for-trino")
    cfg = _jwt_cfg()
    with _as_bearer("eyJ.for-mcp"):
        TrinoClient(cfg, identity=JwtIdentity(cfg, exchange)).query("SELECT 1", user="alice")
    assert isinstance(seen["auth"], trino.auth.JWTAuthentication)
    assert seen["auth"].token == "eyJ.for-trino" and seen["user"] == "alice"
    assert exchange.asked == ["eyJ.for-mcp"], "the caller's token went to the exchange only"


def test_jwt_mode_refuses_a_query_without_a_verified_bearer(monkeypatch):
    """No token, no query: falling back to TRINO_USER would be the silent escalation this
    mode exists to prevent."""
    import pytest

    seen = _capture(monkeypatch)
    exchange = _Exchange()
    cfg = _jwt_cfg()
    with pytest.raises(PermissionError, match="TRINO_USER"):
        TrinoClient(cfg, identity=JwtIdentity(cfg, exchange)).query("SELECT 1", user="alice")
    assert seen == {} and exchange.asked == [], "nothing reached Trino nor the provider"


def test_jwt_mode_refuses_a_bearer_without_a_verified_subject(monkeypatch):
    import pytest

    seen = _capture(monkeypatch)
    cfg = _jwt_cfg()
    with _as_bearer("eyJ.for-mcp"), pytest.raises(PermissionError, match="no verified caller"):
        TrinoClient(cfg, identity=JwtIdentity(cfg, _Exchange())).query("SELECT 1", user=None)
    assert seen == {}


def test_jwt_mode_surfaces_a_refused_exchange_and_runs_nothing(monkeypatch):
    import pytest

    seen = _capture(monkeypatch)
    cfg = _jwt_cfg()
    mode = JwtIdentity(cfg, _Exchange(PermissionError("the identity provider refused")))
    with _as_bearer("eyJ.for-mcp"), pytest.raises(PermissionError, match="refused"):
        TrinoClient(cfg, identity=mode).query("SELECT 1", user="alice")
    assert seen == {}


def test_jwt_mode_refuses_plain_http_at_start_and_per_query():
    import pytest

    cfg = _jwt_cfg(trino_http_scheme="http")
    with pytest.raises(ValueError, match="TRINO_HTTP_SCHEME=https"):
        resolve_identity(cfg)
    with _as_bearer("eyJ.token"), pytest.raises(ValueError, match="TRINO_HTTP_SCHEME=https"):
        JwtIdentity(cfg, _Exchange()).connection("alice")


def test_modes_that_run_as_the_caller_refuse_to_start_behind_an_open_door():
    """A request without a verified token must be refused at the door (401), not reach a
    tool: both modes that run as the caller need MCP_AUTH_ENABLED and MCP_AUTH_REQUIRED."""
    import pytest

    for make in (_jwt_cfg, _imp_cfg):
        for door in ({"auth_enabled": False}, {"auth_required": False}):
            with pytest.raises(ValueError, match="MCP_AUTH_REQUIRED=true"):
                resolve_identity(make(**door))


def test_jwt_mode_refuses_to_start_without_the_token_exchange():
    import pytest

    with pytest.raises(ValueError, match="TRINO_TOKEN_EXCHANGE_URL.*impersonation"):
        resolve_identity(_jwt_cfg(token_exchange_url=""))


def test_jwt_mode_says_what_it_sends_never_a_token_nor_the_secret():
    lines = JwtIdentity(_jwt_cfg()).describe()
    assert lines == [
        "trino identity: the verified caller, with a token for Trino obtained by token "
        "exchange; the caller's own token never leaves the server",
        "token exchange: https://idp/token as client mcp-server (secret set), audience trino",
    ]
    assert "s3cret" not in " ".join(lines)


def test_jwt_mode_sends_the_token_a_subclass_chooses_for_the_caller(monkeypatch):
    """An extension may obtain the token for Trino another way by overriding
    ``trino_token``."""
    import trino.auth

    class _ForTrino(JwtIdentity):
        def trino_token(self, bearer):
            return "for-trino:" + bearer

    seen = _capture(monkeypatch)
    cfg = _jwt_cfg()
    mode = _ForTrino(cfg)
    with _as_bearer("for-mcp"):
        TrinoClient(cfg, identity=mode).query("SELECT 1", user="alice")
    assert isinstance(seen["auth"], trino.auth.JWTAuthentication)
    assert seen["auth"].token == "for-trino:for-mcp"


def test_impersonation_runs_the_query_as_the_verified_caller_with_the_servers_account(
    monkeypatch,
):
    """Trino authenticates the server's account and runs the query as the caller it names,
    under an impersonation rule only that account has; the caller's token stays here."""
    import trino.auth

    seen = _capture(monkeypatch)
    with _as_bearer("eyJ.for-mcp"):
        TrinoClient(_imp_cfg()).query("SELECT 1", user="alice")
    assert seen["user"] == "alice"
    assert isinstance(seen["auth"], trino.auth.BasicAuthentication)
    assert seen["auth"]._username == "svc"


def test_impersonation_readiness_runs_as_the_servers_account_impersonating_no_one(monkeypatch):
    """Found on the live integration run: readiness has no caller, and must not be refused
    for it; it runs as the server's own account."""
    seen = _capture(monkeypatch)
    TrinoClient(_imp_cfg()).ping()
    assert seen["user"] == "svc" and seen["auth"]._username == "svc"


def test_impersonation_refuses_a_query_without_a_verified_caller(monkeypatch):
    import pytest

    seen = _capture(monkeypatch)
    with pytest.raises(PermissionError, match="no verified caller"):
        TrinoClient(_imp_cfg()).query("SELECT 1", user=None)
    assert seen == {}


def test_impersonation_needs_https_and_a_credential_for_the_servers_account():
    import pytest

    with pytest.raises(ValueError, match="TRINO_HTTP_SCHEME=https"):
        resolve_identity(_imp_cfg(trino_http_scheme="http", trino_password=""))
    with pytest.raises(ValueError, match="TRINO_PASSWORD, or TRINO_CLIENT_CERT"):
        resolve_identity(_imp_cfg(trino_password=""))
    assert resolve_identity(_imp_cfg()).describe() == [
        "trino identity: the verified caller, impersonated by svc (password set); "
        "Trino must allow only svc to impersonate"
    ]


def test_the_servers_account_may_authenticate_by_client_certificate(monkeypatch):
    import pytest
    import trino.auth

    seen = _capture(monkeypatch)
    cfg = _imp_cfg(trino_password="", trino_client_cert="/c.pem", trino_client_key="/k.pem")
    TrinoClient(cfg).query("SELECT 1", user="alice")
    assert isinstance(seen["auth"], trino.auth.CertificateAuthentication)
    assert "client certificate" in resolve_identity(cfg).describe()[0]
    svc = _cfg(trino_http_scheme="https", trino_client_cert="/c.pem", trino_client_key="/k.pem")
    TrinoClient(svc).query("SELECT 1", user="alice")
    assert seen["user"] == "svc" and isinstance(seen["auth"], trino.auth.CertificateAuthentication)
    with pytest.raises(ValueError, match="go together"):
        resolve_identity(_cfg(trino_http_scheme="https", trino_client_cert="/c.pem"))
    with pytest.raises(ValueError, match="TRINO_HTTP_SCHEME"):
        resolve_identity(_cfg(trino_client_cert="/c.pem", trino_client_key="/k.pem"))


class _InfoResp:
    def __init__(self, status, body):
        self.status_code, self._b = status, body

    def json(self):
        return self._b


def test_jwt_mode_readiness_asks_trinos_public_v1_info(monkeypatch):
    """No identity of its own: readiness asks /v1/info, which needs none."""
    import akko_mcp_trino.trino_client as tc

    seen = {}

    def fake_get(url, **kw):
        seen["url"], seen["verify"] = url, kw.get("verify")
        return _InfoResp(200, {"starting": False})

    monkeypatch.setattr(tc.httpx, "get", fake_get)
    cfg = _jwt_cfg(trino_port=8443, trino_verify="/ca")
    TrinoClient(cfg).ping()
    assert seen == {"url": "https://h:8443/v1/info", "verify": "/ca"}


def test_jwt_mode_is_not_ready_while_trino_starts_or_errors(monkeypatch):
    import pytest

    import akko_mcp_trino.trino_client as tc

    cfg = _jwt_cfg()
    monkeypatch.setattr(tc.httpx, "get", lambda url, **kw: _InfoResp(200, {"starting": True}))
    with pytest.raises(RuntimeError, match="starting"):
        TrinoClient(cfg).ping()
    monkeypatch.setattr(tc.httpx, "get", lambda url, **kw: _InfoResp(503, {}))
    with pytest.raises(RuntimeError, match="503"):
        TrinoClient(cfg).ping()


# ---- the source, and the client tags an extension adds


def test_every_query_names_the_server_as_source_and_carries_the_tags_it_is_given(monkeypatch):
    from akko_mcp_trino.audit import (
        bind_call,
        reset_call,
        reset_current_request_id,
        set_current_request_id,
    )

    seen = _capture(monkeypatch)
    rid = set_current_request_id("edge-42")
    _, token = bind_call("execute_query")
    try:
        _client(_CFG, tags=lambda: ["tool=execute_query"]).query("SELECT 1", user="carol")
        assert seen["client_tags"] == ["tool=execute_query"]
        _client(_CFG).query("SELECT 1", user="carol")
    finally:
        reset_call(token)
        reset_current_request_id(rid)
    assert seen["source"] == "x", "the source defaults to the server name"
    assert seen["client_tags"] is None, "the core alone sends no client tag"


def test_the_source_can_be_set_and_no_tag_is_sent_outside_a_call(monkeypatch):
    seen = _capture(monkeypatch)
    _client(_cfg(trino_source="agents-gateway")).query("SELECT 1")
    assert seen["source"] == "agents-gateway" and seen["client_tags"] is None


# ---- bounded results: max_rows + 1 says whether the answer was cut


class _Cancellable(_Cur):
    query_id = "q1"  # as the Trino client names a query once Trino has answered its POST

    def __init__(self, rows):
        super().__init__(rows)
        self.cancelled = 0

    def cancel(self):
        self.cancelled += 1


def _patch_cursor(monkeypatch, cursor, capture=None):
    import akko_mcp_trino.trino_client as tc

    def connect(**kw):
        if capture is not None:
            capture.update(kw)
        return type("C", (), {"cursor": lambda self: cursor})()

    monkeypatch.setattr(tc.trino.dbapi, "connect", connect)


def test_one_row_more_than_the_cap_is_read_to_know_the_answer_was_cut(monkeypatch):
    cur = _Cancellable([(1, 2), (3, 4), (5, 6)])
    _patch_cursor(monkeypatch, cur)
    out = _client(_CFG).query("SELECT * FROM t")
    assert out == {
        "columns": ["a", "b"],
        "rows": [[1, 2], [3, 4]],
        "row_count": 2,
        "truncated": True,
    }
    assert cur.cancelled == 1, "the rest of a cut answer is cancelled, not left running in Trino"


def test_an_answer_that_fits_is_not_truncated_and_nothing_is_cancelled(monkeypatch):
    cur = _Cancellable([(1, 2), (3, 4)])
    _patch_cursor(monkeypatch, cur)
    out = _client(_CFG).query("SELECT * FROM t")
    assert out["truncated"] is False and out["row_count"] == 2 and cur.cancelled == 0


def test_a_caller_may_ask_for_fewer_rows_never_more(monkeypatch):
    cur = _Cancellable([(i, i) for i in range(10)])
    _patch_cursor(monkeypatch, cur)
    cfg = _cfg(max_rows=5)
    assert _client(cfg).query("SELECT 1", max_rows=1)["rows"] == [[0, 0]]
    assert _client(cfg).query("SELECT 1", max_rows=50)["row_count"] == 5
    assert _client(cfg).query("SELECT 1", max_rows=0)["row_count"] == 5


def test_a_failed_cancel_of_a_cut_answer_does_not_hide_the_rows(monkeypatch):
    class _BadCancel(_Cancellable):
        def cancel(self):
            raise RuntimeError("already finished")

    _patch_cursor(monkeypatch, _BadCancel([(1, 2), (3, 4), (5, 6)]))
    assert _client(_CFG).query("SELECT 1")["truncated"] is True


# ---- a time budget per query, enforced by cancelling it in Trino


class _Slow(_Cancellable):
    """A cursor whose rows only come once cancelled, as a long Trino query would."""

    def __init__(self):
        import threading

        super().__init__([])
        self.stop = threading.Event()

    def fetchmany(self, n):
        assert self.stop.wait(5), "the budget never cancelled the query"
        raise RuntimeError("Query was canceled")

    def cancel(self):
        self.cancelled += 1
        self.stop.set()


def test_a_query_over_its_time_budget_is_cancelled_and_says_so(monkeypatch):
    import pytest

    cur = _Slow()
    _patch_cursor(monkeypatch, cur)
    with pytest.raises(TimeoutError, match=r"time budget of 0\.05 s"):
        _client(_cfg(query_timeout_seconds=0.05)).query("SELECT 1")
    assert cur.cancelled == 1


def test_an_error_before_the_budget_is_the_error_itself(monkeypatch):
    import pytest

    class _Fails(_Cancellable):
        def fetchmany(self, n):
            raise RuntimeError("Access Denied: Cannot select")

    cur = _Fails([])
    _patch_cursor(monkeypatch, cur)
    with pytest.raises(RuntimeError, match="Access Denied"):
        _client(_cfg(query_timeout_seconds=30)).query("SELECT 1")
    assert cur.cancelled == 0


def test_a_query_within_its_budget_is_untouched(monkeypatch):
    cur = _Cancellable([(1, 2)])
    _patch_cursor(monkeypatch, cur)
    assert _client(_cfg(query_timeout_seconds=30)).query("SELECT 1")["rows"] == [[1, 2]]
    assert cur.cancelled == 0


# ---- the deadline of a tool call bounds every query it runs


def test_past_the_calls_deadline_no_query_starts_and_the_call_knows_it(monkeypatch):
    import pytest

    import akko_mcp_trino.audit as audit
    from akko_mcp_trino.audit import CallTimeout, bind_call, reset_call

    now = [0.0]
    monkeypatch.setattr(audit.time, "monotonic", lambda: now[0])
    seen = _capture(monkeypatch)
    call, token = bind_call("a_tool", budget_seconds=10)
    try:
        now[0] = 10.5
        with pytest.raises(CallTimeout, match="time budget of 10 s"):
            _client(_CFG).query("SELECT 1", user="carol")
    finally:
        reset_call(token)
    assert call.expired is True and seen == {}, "nothing was sent to Trino"


def test_a_query_gets_what_is_left_of_the_calls_budget_when_that_is_less(monkeypatch):
    import pytest

    import akko_mcp_trino.audit as audit
    from akko_mcp_trino.audit import CallTimeout, bind_call, reset_call

    now = [0.0]
    monkeypatch.setattr(audit.time, "monotonic", lambda: now[0])
    cur = _Slow()
    _patch_cursor(monkeypatch, cur)
    call, token = bind_call("a_tool", budget_seconds=30)
    try:
        now[0] = 29.95  # 0.05 s left, the query's own budget is 30 s
        with pytest.raises(CallTimeout, match="tool call exceeded its time budget of 30 s"):
            _client(_cfg(query_timeout_seconds=30)).query("SELECT 1")
    finally:
        reset_call(token)
    assert cur.cancelled == 1 and call.expired is True


def test_the_query_budget_holds_when_it_is_the_shorter_one(monkeypatch):
    import pytest

    from akko_mcp_trino.audit import CallTimeout, bind_call, reset_call

    cur = _Slow()
    _patch_cursor(monkeypatch, cur)
    call, token = bind_call("execute_query", budget_seconds=600)
    try:
        with pytest.raises(TimeoutError, match=r"query exceeded its time budget of 0\.05 s") as e:
            _client(_cfg(query_timeout_seconds=0.05)).query("SELECT 1")
    finally:
        reset_call(token)
    assert not isinstance(e.value, CallTimeout) and call.expired is False


def test_without_a_query_budget_the_calls_deadline_still_cancels(monkeypatch):
    import pytest

    import akko_mcp_trino.audit as audit
    from akko_mcp_trino.audit import CallTimeout, bind_call, reset_call

    now = [0.0]
    monkeypatch.setattr(audit.time, "monotonic", lambda: now[0])
    cur = _Slow()
    _patch_cursor(monkeypatch, cur)
    _, token = bind_call("execute_query", budget_seconds=5)
    try:
        now[0] = 4.95
        with pytest.raises(CallTimeout):
            _client(_CFG).query("SELECT 1")
    finally:
        reset_call(token)
    assert cur.cancelled == 1


class _BlockingStart(_Cancellable):
    """The Trino client's execute: the POST outlives the budget, then execute blocks until the
    first rows, which a query with nothing to show before its end brings only when it ends.
    A cancel before the POST is answered stops nothing (no name yet)."""

    query_id = None

    def __init__(self, post_seconds):
        import threading

        super().__init__([])
        self.post_seconds, self.stop = post_seconds, threading.Event()

    def execute(self, sql, params=None):
        import time

        time.sleep(self.post_seconds)
        self.query_id = "q1"  # Trino has answered the POST: the query has a name
        if self.stop.wait(5):
            raise RuntimeError("Query was canceled")

    def cancel(self):
        self.cancelled += 1
        if self.query_id is not None:
            self.stop.set()


def test_a_query_named_after_its_budget_ran_out_is_cancelled_as_soon_as_it_is_named(monkeypatch):
    """Found in review on 2026-09-27 with the real client: the cancel came before Trino named
    the query, and the query ran on for as long as it took to bring its first rows."""
    import time

    import pytest

    cur = _BlockingStart(post_seconds=0.2)
    _patch_cursor(monkeypatch, cur)
    start = time.monotonic()
    with pytest.raises(TimeoutError, match=r"time budget of 0\.05 s"):
        _client(_cfg(query_timeout_seconds=0.05)).query("SELECT 1")
    assert time.monotonic() - start < 1, "cancelled once named, not after the first rows"
    assert cur.stop.is_set() and cur.cancelled >= 2, "tried before the name, then after"


class _LateStart(_Cancellable):
    """A POST /v1/statement that outlives the budget. Until Trino answers it, the query has no
    name, and a cancel has nothing to stop (the Trino client then sends no DELETE); the rows
    come afterwards as if nothing had happened."""

    def __init__(self, rows, delay):
        super().__init__(rows)
        self.delay, self.named, self.stopped = delay, False, 0

    query_id = None

    def execute(self, sql, params=None):
        import time

        time.sleep(self.delay)
        self.named, self.query_id = True, "q1"
        super().execute(sql, params)

    def fetchmany(self, n):
        self.fetched = True
        return super().fetchmany(n)

    def cancel(self):
        self.cancelled += 1
        self.stopped += self.named


def test_a_budget_spent_before_trino_names_the_query_still_refuses_the_answer(monkeypatch):
    import pytest

    cur = _LateStart([(1, 2)], delay=0.2)
    _patch_cursor(monkeypatch, cur)
    with pytest.raises(TimeoutError, match=r"time budget of 0\.05 s"):
        _client(_cfg(query_timeout_seconds=0.05)).query("SELECT 1")
    assert cur.stopped == 1, "once Trino has named the query, it is cancelled"
    assert not hasattr(cur, "fetched"), "no row is read past the budget"


def test_a_call_whose_budget_ran_out_during_the_post_knows_it_expired(monkeypatch):
    import pytest

    from akko_mcp_trino.audit import CallTimeout, bind_call, reset_call

    cur = _LateStart([(1, 2)], delay=0.2)
    _patch_cursor(monkeypatch, cur)
    call, token = bind_call("a_tool", budget_seconds=0.05)
    try:
        with pytest.raises(CallTimeout):
            _client(_CFG).query("SELECT 1")
    finally:
        reset_call(token)
    assert call.expired is True and cur.stopped == 1


def test_rows_that_arrive_once_the_budget_is_spent_are_refused(monkeypatch):
    """The budget runs out while the last page is read, and the cancel comes too late to fail
    the fetch: the rows are there, the budget is not, and the answer is refused all the same."""
    import pytest

    class _LastPageLate(_Cancellable):
        def fetchmany(self, n):
            import time

            time.sleep(0.2)
            return super().fetchmany(n)

    cur = _LastPageLate([(1, 2)])
    _patch_cursor(monkeypatch, cur)
    with pytest.raises(TimeoutError, match="and was cancelled"):
        _client(_cfg(query_timeout_seconds=0.05)).query("SELECT 1")
    assert cur.cancelled >= 1


# ---- what the server itself must know is read past the row cap, never past a limit


def test_every_row_reads_on_from_the_next_row_while_the_cap_cuts_a_page():
    from akko_mcp_trino.trino_client import every_row

    table = [[i] for i in range(7)]
    sent = []

    def query(sql):
        sent.append(sql)
        start = int(sql.split(" OFFSET ")[1].split()[0]) if " OFFSET " in sql else 0
        stop = int(sql.rsplit(" LIMIT ", 1)[1]) if " LIMIT " in sql else len(table)
        rows = table[start:][:stop]
        return {"rows": rows[:3], "truncated": len(rows) > 3}

    assert every_row(query, "SELECT n FROM t ORDER BY n") == table
    assert sent == [
        "SELECT n FROM t ORDER BY n",
        "SELECT n FROM t ORDER BY n OFFSET 3",
        "SELECT n FROM t ORDER BY n OFFSET 6",
    ]
    sent.clear()
    assert every_row(query, "SELECT n FROM t ORDER BY n", 4) == table[:4]
    assert sent == [
        "SELECT n FROM t ORDER BY n LIMIT 4",
        "SELECT n FROM t ORDER BY n OFFSET 3 LIMIT 1",
    ]


def test_every_row_stops_on_a_cut_page_that_brings_nothing():
    from akko_mcp_trino.trino_client import every_row

    assert every_row(lambda sql: {"rows": [], "truncated": True}, "SELECT 1") == []
