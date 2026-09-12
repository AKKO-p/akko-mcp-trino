"""akko_mcp_trino.trino_client with a mocked connection: no real Trino involved."""

from akko_mcp_trino.config import Config
from akko_mcp_trino.trino_client import TrinoClient

_CFG = Config(
    trino_host="h",
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


def _patch_connect(monkeypatch, rows, capture):
    import akko_mcp_trino.trino_client as tc

    def fake_connect(**kwargs):
        capture.update(kwargs)
        return _Conn(rows)

    monkeypatch.setattr(tc.trino.dbapi, "connect", fake_connect)


def test_query_returns_columns_rows_count_bounded(monkeypatch):
    cap = {}
    _patch_connect(monkeypatch, [(1, 2), (3, 4), (5, 6)], cap)
    out = TrinoClient(_CFG).query("SELECT * FROM t")
    assert out == {
        "columns": ["a", "b"],
        "rows": [[1, 2], [3, 4]],
        "row_count": 2,
    }  # capped at max_rows=2


def test_query_uses_config_identity_by_default(monkeypatch):
    cap = {}
    _patch_connect(monkeypatch, [], cap)
    TrinoClient(_CFG).query("SELECT 1")
    assert {k: cap[k] for k in ("host", "port", "user", "catalog")} == {
        "host": "h",
        "port": 8080,
        "user": "svc",
        "catalog": "cat",
    }


def test_query_propagates_explicit_user(monkeypatch):
    cap = {}
    _patch_connect(monkeypatch, [], cap)
    TrinoClient(_CFG).query("SELECT 1", user="dave_steward")
    assert cap["user"] == "dave_steward"  # X-Trino-User = identity end to end


def test_query_no_description_returns_empty_columns(monkeypatch):
    import akko_mcp_trino.trino_client as tc

    class _NoDescCur(_Cur):
        description = None

    monkeypatch.setattr(
        tc.trino.dbapi,
        "connect",
        lambda **k: type("C", (), {"cursor": lambda self: _NoDescCur([])})(),
    )
    out = TrinoClient(_CFG).query("SET x=1")
    assert out["columns"] == [] and out["rows"] == []


# ---- 0.2: HTTPS, service credentials, TLS verification, timeout, identity modes


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


def test_defaults_are_plain_http_without_auth(monkeypatch):
    seen = _capture(monkeypatch)
    TrinoClient(_CFG).query("SELECT 1", user="alice")
    assert seen["http_scheme"] == "http" and seen["auth"] is None
    assert seen["user"] == "alice", "X-Trino-User carries the caller"
    assert seen["verify"] is True and seen["request_timeout"] == 30.0


def test_https_with_service_password_impersonates_the_caller(monkeypatch):
    """The usual production shape: the service account authenticates (Basic over
    HTTPS) and Trino's impersonation rules let it act as the verified user."""
    import trino.auth

    seen = _capture(monkeypatch)
    cfg = _cfg(trino_http_scheme="https", trino_password="s3cret")
    TrinoClient(cfg).query("SELECT 1", user="alice")
    assert seen["http_scheme"] == "https"
    assert isinstance(seen["auth"], trino.auth.BasicAuthentication)
    assert seen["auth"]._username == "svc" and seen["auth"]._password == "s3cret"
    assert seen["user"] == "alice"


def test_password_over_plain_http_refuses_to_send_it(monkeypatch):
    """A password on http would travel in clear; the client refuses rather than leaks."""
    import pytest

    _capture(monkeypatch)
    with pytest.raises(ValueError):
        TrinoClient(_cfg(trino_password="s3cret")).query("SELECT 1")


def test_tls_verification_can_point_at_a_ca_bundle_or_be_disabled(monkeypatch):
    seen = _capture(monkeypatch)
    TrinoClient(_cfg(trino_http_scheme="https", trino_verify="/etc/ssl/ca.pem")).query("SELECT 1")
    assert seen["verify"] == "/etc/ssl/ca.pem"
    TrinoClient(_cfg(trino_http_scheme="https", trino_verify="false")).query("SELECT 1")
    assert seen["verify"] is False


def test_request_timeout_is_configurable(monkeypatch):
    seen = _capture(monkeypatch)
    TrinoClient(_cfg(trino_request_timeout=5.5)).query("SELECT 1")
    assert seen["request_timeout"] == 5.5


def test_jwt_passthrough_sends_the_callers_token_to_trino(monkeypatch):
    """Identity mode `jwt`: Trino verifies the same token the guard verified, so
    no impersonation right is needed. The service account is not used at all."""
    import trino.auth

    seen = _capture(monkeypatch)
    cfg = _cfg(trino_http_scheme="https", trino_identity_mode="jwt")
    TrinoClient(cfg).query("SELECT 1", user="alice", bearer="eyJ.token")
    assert isinstance(seen["auth"], trino.auth.JWTAuthentication)
    assert seen["auth"].token == "eyJ.token"
    assert seen["user"] == "alice"


def test_jwt_passthrough_refuses_a_query_without_a_bearer(monkeypatch):
    """No token, no query: falling back to the service account here would be the
    exact silent escalation this server exists to prevent."""
    import pytest

    _capture(monkeypatch)
    with pytest.raises(PermissionError):
        TrinoClient(_cfg(trino_http_scheme="https", trino_identity_mode="jwt")).query("SELECT 1")


def test_jwt_passthrough_requires_https(monkeypatch):
    import pytest

    _capture(monkeypatch)
    with pytest.raises(ValueError):
        TrinoClient(_cfg(trino_identity_mode="jwt")).query("SELECT 1", bearer="t")


def test_unknown_identity_mode_is_refused(monkeypatch):
    import pytest

    _capture(monkeypatch)
    with pytest.raises(ValueError):
        TrinoClient(_cfg(trino_identity_mode="magic")).query("SELECT 1")
