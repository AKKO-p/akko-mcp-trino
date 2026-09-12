"""akko_mcp_trino.metrics: isolated registry, TrinoClient instrumentation, /metrics route."""

from prometheus_client import CollectorRegistry
from starlette.testclient import TestClient

from akko_mcp_trino.config import Config
from akko_mcp_trino.health import build_health_app
from akko_mcp_trino.metrics import Metrics
from akko_mcp_trino.trino_client import TrinoClient
from tests.test_trino_client import _patch_connect

_CFG = Config(
    trino_host="h",
    trino_port=8080,
    trino_user="u",
    trino_catalog="c",
    max_rows=100,
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


def _val(metrics, name):
    return metrics.registry.get_sample_value(name) or 0.0


def test_render_exposes_metric_names():
    body, ctype = Metrics().render()
    assert b"mcp_trino_queries_total" in body
    assert "text/plain" in ctype


def test_query_increments_counters_and_latency(monkeypatch):
    m = Metrics(CollectorRegistry())
    _patch_connect(monkeypatch, [(1,)], {})
    TrinoClient(_CFG, metrics=m).query("SELECT 1")
    assert _val(m, "mcp_trino_queries_total") == 1.0
    assert _val(m, "mcp_trino_query_errors_total") == 0.0
    assert _val(m, "mcp_trino_query_duration_seconds_count") == 1.0


def test_query_error_increments_error_counter(monkeypatch):
    m = Metrics(CollectorRegistry())
    import akko_mcp_trino.trino_client as tc

    monkeypatch.setattr(
        tc.trino.dbapi, "connect", lambda **k: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    import pytest

    with pytest.raises(RuntimeError):
        TrinoClient(_CFG, metrics=m).query("SELECT 1")
    assert _val(m, "mcp_trino_queries_total") == 1.0
    assert _val(m, "mcp_trino_query_errors_total") == 1.0


def test_query_without_metrics_does_not_instrument(monkeypatch):
    _patch_connect(monkeypatch, [(1,)], {})
    # pas de metrics → chemin direct, aucune exception
    out = TrinoClient(_CFG).query("SELECT 1")
    assert out["row_count"] == 1


def test_metrics_route_present_when_metrics_provided():
    m = Metrics(CollectorRegistry())
    from tests.test_tools import FakeClient

    app = build_health_app(FakeClient(), metrics=m)
    r = TestClient(app).get("/metrics")
    assert r.status_code == 200
    assert "mcp_trino_queries_total" in r.text


def test_metrics_route_absent_without_metrics():
    from tests.test_tools import FakeClient

    app = build_health_app(FakeClient())
    assert TestClient(app).get("/metrics").status_code == 404
