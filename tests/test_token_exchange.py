"""akko_mcp_trino.token_exchange: RFC 8693 against a stand-in identity provider, no network."""

import pytest

from akko_mcp_trino.config import Config
from akko_mcp_trino.token_exchange import (
    ACCESS_TOKEN_TYPE,
    GRANT_TYPE,
    TokenExchange,
    tls_verify,
)


def _cfg(**over):
    base = dict(
        trino_host="h",
        trino_port=8443,
        trino_user="svc",
        trino_catalog="c",
        max_rows=10,
        read_only=True,
        auth_enabled=True,
        server_name="t",
        health_port=3001,
        jwks_url="https://idp/certs",
        oidc_issuer="https://idp",
        oidc_audience="mcp-server",
        transport="streamable-http",
        mcp_port=3000,
        auth_required=True,
        trino_http_scheme="https",
        token_exchange_url="https://idp/token",
        token_exchange_client_id="mcp-server",
        token_exchange_client_secret="s3cret",
        token_exchange_audience="trino",
        token_exchange_verify="/ca.pem",
        trino_request_timeout=7.0,
    )
    base.update(over)
    return Config(**base)


class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class _IdP:
    """Answers each exchange with a fresh token, and records what it was sent."""

    def __init__(self, status=200, body=None):
        self.sent, self.status, self.body = [], status, body

    def __call__(self, url, **kw):
        self.sent.append((url, kw))
        body = (
            self.body
            if self.body is not None
            else {
                "access_token": f"for-trino-{len(self.sent)}",
                "issued_token_type": ACCESS_TOKEN_TYPE,
                "token_type": "Bearer",
                "expires_in": 300,
            }
        )
        return _Resp(self.status, body)


def test_the_exchange_follows_rfc_8693_with_the_servers_own_client():
    idp = _IdP()
    token = TokenExchange(_cfg(token_exchange_scope="openid"), post=idp).token_for("for-mcp")
    assert token == "for-trino-1"
    url, kw = idp.sent[0]
    assert url == "https://idp/token"
    assert kw["data"] == {
        "grant_type": GRANT_TYPE,
        "subject_token": "for-mcp",
        "subject_token_type": ACCESS_TOKEN_TYPE,
        "requested_token_type": ACCESS_TOKEN_TYPE,
        "audience": "trino",
        "scope": "openid",
    }
    assert kw["auth"] == ("mcp-server", "s3cret")
    assert kw["verify"] == "/ca.pem" and kw["timeout"] == 7.0


def test_no_scope_is_sent_when_none_is_set():
    idp = _IdP()
    TokenExchange(_cfg(), post=idp).token_for("for-mcp")
    assert "scope" not in idp.sent[0][1]["data"]


def test_an_exchanged_token_is_reused_until_shortly_before_it_expires():
    idp, now = _IdP(), [0.0]
    exchange = TokenExchange(_cfg(), post=idp, clock=lambda: now[0])
    assert exchange.token_for("alice") == "for-trino-1"
    now[0] = 200.0
    assert exchange.token_for("alice") == "for-trino-1", "reused within its lifetime"
    assert exchange.token_for("bob") == "for-trino-2", "one token per caller"
    now[0] = 271.0  # 300 s minus the 30 s margin
    assert exchange.token_for("alice") == "for-trino-3", "exchanged again before it expires"
    assert len(idp.sent) == 3


def test_a_token_without_a_usable_lifetime_is_not_kept():
    for body in (
        {"access_token": "t", "expires_in": 10},
        {"access_token": "t"},
        {"access_token": "t", "expires_in": "soon"},
    ):
        idp = _IdP(body=body)
        exchange = TokenExchange(_cfg(), post=idp)
        exchange.token_for("alice")
        exchange.token_for("alice")
        assert len(idp.sent) == 2


def test_the_cache_is_bounded_and_drops_expired_and_oldest_first(monkeypatch):
    import akko_mcp_trino.token_exchange as te

    monkeypatch.setattr(te, "_CACHE_SIZE", 2)
    idp, now = _IdP(), [0.0]
    exchange = TokenExchange(_cfg(), post=idp, clock=lambda: now[0])
    exchange.token_for("a")
    exchange.token_for("b")
    exchange.token_for("c")  # full: "a" goes
    assert len(exchange._cache) == 2
    exchange.token_for("a")
    assert len(idp.sent) == 4, "the oldest was dropped and exchanged again"
    now[0] = 1000.0  # every token expired: purged before the next one is kept
    exchange.token_for("d")
    assert len(exchange._cache) == 1


def test_the_cache_never_holds_the_callers_token():
    exchange = TokenExchange(_cfg(), post=_IdP())
    exchange.token_for("eyJ.secret-caller-token")
    assert all("secret-caller-token" not in k for k in exchange._cache)


@pytest.mark.parametrize(
    ("status", "body", "detail"),
    [
        (400, {"error": "invalid_request"}, "HTTP 400, invalid_request"),
        (403, {"error": "access_denied", "error_description": "eyJ.x"}, "HTTP 403, access_denied"),
        (500, ValueError("not json"), "HTTP 500)"),
        (200, {"token_type": "Bearer"}, "HTTP 200)"),
        (200, ["not", "a", "dict"], "HTTP 200)"),
    ],
)
def test_a_refused_exchange_is_a_permission_error_that_names_no_token(status, body, detail):
    with pytest.raises(PermissionError) as e:
        TokenExchange(_cfg(), post=_IdP(status, body)).token_for("eyJ.caller")
    assert detail in str(e.value)
    assert "eyJ" not in str(e.value)


def test_a_call_past_its_deadline_asks_the_provider_nothing(monkeypatch):
    import akko_mcp_trino.audit as audit
    from akko_mcp_trino.audit import CallTimeout, bind_call, reset_call

    now = [0.0]
    monkeypatch.setattr(audit.time, "monotonic", lambda: now[0])
    idp = _IdP()
    _, token = bind_call("execute_query", budget_seconds=5)
    try:
        now[0] = 6.0
        with pytest.raises(CallTimeout):
            TokenExchange(_cfg(), post=idp).token_for("alice")
    finally:
        reset_call(token)
    assert idp.sent == []


def test_no_request_timeout_means_no_timeout_not_zero():
    idp = _IdP()
    TokenExchange(_cfg(trino_request_timeout=0.0), post=idp).token_for("alice")
    assert idp.sent[0][1]["timeout"] is None


def test_the_settings_are_described_without_the_secret():
    lines = TokenExchange(_cfg(token_exchange_scope="trino-read")).describe()
    assert lines == [
        "token exchange: https://idp/token as client mcp-server (secret set), "
        "audience trino, scope trino-read"
    ]
    assert "s3cret" not in lines[0]


def test_missing_settings_and_plain_http_refuse_to_start():
    with pytest.raises(ValueError, match="needs TRINO_TOKEN_EXCHANGE_CLIENT_SECRET, "):
        TokenExchange(_cfg(token_exchange_client_secret="", token_exchange_audience="")).describe()
    with pytest.raises(ValueError, match="must be https"):
        TokenExchange(_cfg(token_exchange_url="http://idp/token")).describe()


def test_tls_verification_setting():
    assert tls_verify("true") is True and tls_verify("NO") is False
    assert tls_verify(" /ca.pem ") == "/ca.pem"
