"""build_server assembly and the health endpoints."""

from starlette.testclient import TestClient

from akko_mcp_trino.config import Config
from akko_mcp_trino.health import build_health_app
from akko_mcp_trino.server import build_server
from akko_mcp_trino.trino_client import TrinoClient
from tests.test_tools import FakeClient, FakeMCP

_CFG = Config(
    trino_host="h",
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
)


def test_build_server_returns_mcp_and_client():
    # Injected FakeMCP factory: tests assembly without depending on a FastMCP version.
    mcp, client = build_server(_CFG, mcp_factory=lambda name: FakeMCP())
    assert set(mcp.tools) == {
        "list_catalogs",
        "list_schemas",
        "list_tables",
        "describe_table",
        "execute_query",
    }
    assert isinstance(client, TrinoClient)


def test_build_server_applies_extra_registrars():
    """The extension point: a product adds its tools without touching the core."""

    def registrar(mcp, client):
        @mcp.tool()
        def outil_maison() -> str:
            return "ok"

    mcp, _ = build_server(
        _CFG, extra_tool_registrars=[registrar], mcp_factory=lambda name: FakeMCP()
    )
    assert len(mcp.tools) == 6
    assert "outil_maison" in mcp.tools


def test_build_server_passes_server_name_to_factory():
    seen = {}

    def factory(name):
        seen["name"] = name
        return FakeMCP()

    build_server(_CFG, mcp_factory=factory)
    assert seen["name"] == "trino-mcp"


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
