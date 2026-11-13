"""build_server assembly and the health endpoints."""

from dataclasses import replace

from starlette.testclient import TestClient

from akko_mcp_trino.config import Config
from akko_mcp_trino.health import build_health_app
from akko_mcp_trino.server import build_server
from akko_mcp_trino.trino_client import TrinoClient
from tests.test_tools import FakeClient, FakeMCP

_CFG = Config(
    trino_host="h",
    trino_identity_mode="service",
    query_timeout_seconds=0.0,
    call_timeout_seconds=0.0,
    trino_port=8080,
    trino_user="u",
    trino_catalog="c",
    max_rows=100,
    read_only=True,
    auth_enabled=False,
    server_name="trino-mcp",
    health_port=3001,
    jwks_url="",
    oidc_issuer="",
    oidc_audience="",
    transport="sse",
    mcp_port=3000,
    auth_required=False,
    trino_http_scheme="https",
)


def test_build_server_returns_mcp_and_client():
    # Injected FakeMCP factory: tests assembly without depending on an SDK version.
    mcp, client = build_server(_CFG, mcp_factory=lambda name, **_: FakeMCP())
    assert set(mcp.tools) == {
        "list_catalogs",
        "list_schemas",
        "list_tables",
        "describe_schema",
        "describe_table",
        "explain_query",
        "execute_query",
        "search_columns",
        "profile_table",
    }
    assert isinstance(client, TrinoClient)


def test_build_server_applies_extra_registrars():
    """The extension point: a product adds its tools without touching the core."""

    def registrar(mcp, client):
        @mcp.tool()
        def outil_maison() -> str:
            return "ok"

    mcp, _ = build_server(
        _CFG, extra_tool_registrars=[registrar], mcp_factory=lambda name, **_: FakeMCP()
    )
    assert len(mcp.tools) == 10
    assert "outil_maison" in mcp.tools


def test_build_server_passes_server_name_to_factory():
    seen = {}

    def factory(name, instructions=None):
        seen["name"] = name
        return FakeMCP()

    build_server(_CFG, mcp_factory=factory)
    assert seen["name"] == "trino-mcp"


def test_service_mode_is_announced_to_every_host_in_the_initialization_instructions():
    """Chosen explicitly, ``service`` mode still tells every host that Trino sees one
    identity for every caller, and that no caller is verified when the door is off."""
    seen = {}

    def factory(name, instructions=None):
        seen["instructions"] = instructions
        return FakeMCP()

    build_server(_CFG, mcp_factory=factory)
    text = seen["instructions"]
    assert text.startswith("WARNING")
    assert "TRINO_IDENTITY_MODE=service" in text and "'u'" in text
    assert "MCP_AUTH_ENABLED=false" in text


def test_a_mode_that_runs_as_the_caller_sends_no_instructions(monkeypatch):
    from akko_mcp_trino import server

    seen = {}

    def factory(*args, **kwargs):
        seen["call"] = (args, kwargs)
        return FakeMCP()

    # A call budget too: without one, search_columns is limited, which the warnings say.
    cfg = Config(
        **{
            **_CFG.__dict__,
            "auth_enabled": True,
            "auth_required": True,
            "call_timeout_seconds": 120.0,
        }
    )
    monkeypatch.setattr(server, "resolve_identity", lambda config, exts: object())
    jwt_cfg = Config(**{**cfg.__dict__, "trino_identity_mode": "jwt"})
    build_server(jwt_cfg, mcp_factory=factory)
    assert seen["call"] == (("trino-mcp",), {})


def test_a_setting_no_server_can_serve_refuses_to_build():
    import pytest

    for over in (
        {"max_rows": 0},
        {"query_timeout_seconds": -1.0},
        {"call_timeout_seconds": -5.0},
    ):
        with pytest.raises(ValueError, match="TRINO_MAX_ROWS|TIME|budget"):
            build_server(
                Config(**{**_CFG.__dict__, **over}), mcp_factory=lambda name, **_: FakeMCP()
            )


def test_health_ok_always():
    app = build_health_app(FakeClient())
    r = TestClient(app).get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_ready_200_when_trino_reachable():
    app = build_health_app(FakeClient(result={"columns": ["x"], "rows": [[1]], "row_count": 1}))
    r = TestClient(app).get("/ready")
    assert r.status_code == 200 and r.json()["status"] == "ready"


def test_ready_503_when_trino_down():
    app = build_health_app(FakeClient(raises=RuntimeError("connection refused")))
    r = TestClient(app).get("/ready")
    assert r.status_code == 503
    assert r.json()["status"] == "error" and "connection refused" in r.json()["detail"]


def test_the_names_an_audit_line_keeps_reach_the_tools_and_a_plugin(monkeypatch):
    """MCP_AUDIT_MAX_NAMES set to 1: the record of a built-in tool and of a plugin's tool keep
    one name and count the others."""
    from akko_mcp_trino.audit import InMemoryAudit, note
    from tests.tool_doubles import in_request

    class _Trino:
        def __init__(self, *args, **kwargs):
            pass

        def query(self, sql, user=None, params=None, **keywords):
            return {"columns": ["c"], "rows": [[1]], "row_count": 1, "truncated": False}

    def registrar(mcp, client):
        @mcp.tool()
        def plugin_tables() -> str:
            note(tables=["p.q.r", "p.q.s", "p.q.t"])
            return "ok"

    monkeypatch.setattr("akko_mcp_trino.server.TrinoClient", _Trino)
    audit = InMemoryAudit()
    mcp, _ = build_server(
        replace(_CFG, audit_max_names=1),
        extra_tool_registrars=[registrar],
        mcp_factory=lambda name, **_: FakeMCP(),
        audit=audit,
    )
    in_request(lambda: mcp.tools["execute_query"]("SELECT * FROM a.b.c, a.b.d"))
    in_request(lambda: mcp.tools["plugin_tables"]())
    built_in, plugin = (e.call for e in audit.events)
    assert (built_in.tables, built_in.tables_cut) == (["a.b.c"], 1)
    assert (plugin.tables, plugin.tables_cut) == (["p.q.r"], 2)


def test_the_sql_bounds_reach_the_tools(monkeypatch):
    """MCP_MAX_SQL_CHARS and MCP_MAX_SQL_NESTING set low: the built server's execute_query
    refuses past them before Trino is asked."""
    import json

    asked = []

    class _Trino:
        def __init__(self, *args, **kwargs):
            pass

        def query(self, sql, user=None, params=None, **keywords):
            asked.append(sql)
            return {"columns": ["c"], "rows": [[1]], "row_count": 1, "truncated": False}

    monkeypatch.setattr("akko_mcp_trino.server.TrinoClient", _Trino)
    mcp, _ = build_server(
        replace(_CFG, max_sql_chars=1000, max_sql_nesting=8),
        mcp_factory=lambda name, **_: FakeMCP(),
    )
    nested = json.loads(mcp.tools["execute_query"]("SELECT " + "(" * 9 + "1" + ")" * 9))
    long = json.loads(mcp.tools["execute_query"]("SELECT 1" + " " * 1000 + "FROM c.s.t"))
    assert "more than 8 deep" in nested["error"] and "reads 1000 at most" in long["error"]
    assert asked == []
    mcp.tools["execute_query"]("SELECT " + "(" * 8 + "1" + ")" * 8)
    assert asked == ["SELECT " + "(" * 8 + "1" + ")" * 8]


def test_the_answer_bounds_reach_the_tools(monkeypatch):
    """MCP_MAX_RESULT_BYTES, MCP_MAX_CELL_CHARS and MCP_COMMENT_CHARS set low: the built
    server's tools cut what they serve."""
    import json

    class _Trino:
        def __init__(self, *args, **kwargs):
            pass

        def query(self, sql, user=None, params=None, **keywords):
            if sql.startswith("DESCRIBE"):
                rows = [["id", "int", "", "c" * 50]]
            else:
                rows = [["w" * 50] for _ in range(40)]
            return {"columns": ["c"], "rows": rows, "row_count": len(rows), "truncated": False}

    monkeypatch.setattr("akko_mcp_trino.server.TrinoClient", _Trino)
    mcp, _ = build_server(
        replace(_CFG, max_result_bytes=1000, max_cell_chars=20, comment_chars=30),
        mcp_factory=lambda name, **_: FakeMCP(),
    )
    out = json.loads(mcp.tools["execute_query"]("SELECT 1 FROM c.s.t"))
    assert out["truncated_reason"] == "bytes" and out["cut_columns"] == ["c"]
    assert out["rows"][0] == ["w" * 20] and out["row_count"] < 40
    described = json.loads(mcp.tools["describe_table"]("c", "s", "t"))
    assert described["rows"][0][3] == "c" * 30 and described["comments_cut"] == ["id"]


def test_the_server_parses_with_its_own_pool_and_settings(monkeypatch):
    """MCP_SQL_PARSE_SECONDS and MCP_SQL_PARSE_WORKERS reach the pool the server builds;
    its refusals are counted on the server's metrics."""
    from akko_mcp_trino import parse_pool
    from akko_mcp_trino.metrics import Metrics

    built = []

    class _Pool(parse_pool.ParsePool):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            built.append(self)

    monkeypatch.setattr("akko_mcp_trino.server.ParsePool", _Pool)
    metrics = Metrics()
    mcp, _ = build_server(
        replace(_CFG, sql_parse_seconds=0.75, sql_parse_workers=3, max_sql_chars=1000),
        mcp_factory=lambda name, **_: FakeMCP(),
        metrics=metrics,
    )
    ((pool),) = built
    assert (pool.workers, pool.seconds) == (3, 0.75)
    mcp.tools["execute_query"]("SELECT 1" + " " * 1000)
    assert (
        metrics.registry.get_sample_value("mcp_trino_sql_refusals_total", {"reason": "too_long"})
        == 1
    )
