"""build_server assembly and the health endpoints."""

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
    assert len(mcp.tools) == 8
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

    cfg = Config(**{**_CFG.__dict__, "auth_enabled": True, "auth_required": True})
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
