"""core.trino_client with a mocked connection: no real Trino involved."""
from core.config import Config
from core.trino_client import TrinoClient

_CFG = Config(
    trino_host="h", trino_port=8080, trino_user="svc", trino_catalog="cat",
    max_rows=2, read_only=True, auth_enabled=False, server_name="x", health_port=3001, jwks_url="", oidc_issuer="", oidc_audience="", transport="sse", mcp_port=3000, auth_required=False,
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
    import core.trino_client as tc

    def fake_connect(**kwargs):
        capture.update(kwargs)
        return _Conn(rows)

    monkeypatch.setattr(tc.trino.dbapi, "connect", fake_connect)


def test_query_returns_columns_rows_count_bounded(monkeypatch):
    cap = {}
    _patch_connect(monkeypatch, [(1, 2), (3, 4), (5, 6)], cap)
    out = TrinoClient(_CFG).query("SELECT * FROM t")
    assert out == {"columns": ["a", "b"], "rows": [[1, 2], [3, 4]], "row_count": 2}  # capped at max_rows=2


def test_query_uses_config_identity_by_default(monkeypatch):
    cap = {}
    _patch_connect(monkeypatch, [], cap)
    TrinoClient(_CFG).query("SELECT 1")
    assert cap == {"host": "h", "port": 8080, "user": "svc", "catalog": "cat"}


def test_query_propagates_explicit_user(monkeypatch):
    cap = {}
    _patch_connect(monkeypatch, [], cap)
    TrinoClient(_CFG).query("SELECT 1", user="dave_steward")
    assert cap["user"] == "dave_steward"  # X-Trino-User = identity end to end


def test_query_no_description_returns_empty_columns(monkeypatch):
    cap = {}
    import core.trino_client as tc

    class _NoDescCur(_Cur):
        description = None

    monkeypatch.setattr(tc.trino.dbapi, "connect",
                        lambda **k: type("C", (), {"cursor": lambda self: _NoDescCur([])})())
    out = TrinoClient(_CFG).query("SET x=1")
    assert out["columns"] == [] and out["rows"] == []
