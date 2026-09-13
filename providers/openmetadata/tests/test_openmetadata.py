"""The OpenMetadata context provider, against a mocked API: no network, no OpenMetadata.

The provider reads what the catalogue knows about a table (description,
owners, tier, tags, primary key, foreign keys, column descriptions and
classifications) and says nothing when the table is unknown, the token is
refused, or the catalogue is down. It never touches data.
"""

from __future__ import annotations

import httpx
import pytest
from akko_mcp_trino.context import ColumnContext, TableContext

from akko_mcp_trino_openmetadata import OpenMetadataContext, from_env

TABLE = {
    "fullyQualifiedName": "trino-akko.core_postgres.clients.customers",
    "description": "Référentiel client (le QUI).",
    "owners": [{"type": "team", "name": "data-clients", "displayName": "Équipe données clients"}],
    "tags": [
        {"tagFQN": "Tier.Tier1"},
        {"tagFQN": "Certification.Gold"},
        {"tagFQN": "PersonalData.Personal"},
    ],
    "tableConstraints": [
        {"constraintType": "PRIMARY_KEY", "columns": ["customer_id"]},
        {
            "constraintType": "FOREIGN_KEY",
            "columns": ["branch_id"],
            "referredColumns": ["trino-akko.core_postgres.clients.branches.branch_id"],
        },
    ],
    "columns": [
        {"name": "customer_id", "description": "Identifiant client", "tags": []},
        {
            "name": "email",
            "description": "Adresse email (PII)",
            "tags": [{"tagFQN": "PII.Sensitive"}],
        },
        {"name": "segment", "description": "", "tags": [{"tagFQN": "Akko.AIGenerated_Unreviewed"}]},
    ],
}


def _provider(handler, **kw):
    transport = httpx.MockTransport(handler)
    return OpenMetadataContext(
        "http://om:8585", "bot-token", "trino-akko", http=httpx.Client(transport=transport), **kw
    )


def _ok(request: httpx.Request) -> httpx.Response:
    assert request.headers["authorization"] == "Bearer bot-token"
    assert request.url.path == "/api/v1/tables/name/trino-akko.core_postgres.clients.customers"
    assert (
        "columns" in request.url.params["fields"]
        and "tableConstraints" in request.url.params["fields"]
    )
    return httpx.Response(200, json=TABLE)


def test_table_maps_description_owner_tier_tags_grain_and_joins():
    t = _provider(_ok).table("core_postgres", "clients", "customers")
    assert t == TableContext(
        description="Référentiel client (le QUI).",
        owner="Équipe données clients",
        tier="Tier1",
        grain=["customer_id"],
        joins=[t.joins[0]],
        tags=["Certification.Gold", "PersonalData.Personal"],
    )
    assert t.joins[0].columns == ["branch_id"]
    assert t.joins[0].target == "core_postgres.clients.branches"
    assert t.joins[0].target_columns == ["branch_id"]


def test_column_maps_description_and_classification():
    p = _provider(_ok)
    assert p.column("core_postgres", "clients", "customers", "email") == ColumnContext(
        description="Adresse email (PII)", classification=["PII.Sensitive"]
    )
    assert p.column("core_postgres", "clients", "customers", "segment") == ColumnContext(
        classification=["Akko.AIGenerated_Unreviewed"]
    )
    assert p.column("core_postgres", "clients", "customers", "nope") is None


def test_the_table_is_fetched_once_for_its_columns():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json=TABLE)

    p = _provider(handler)
    p.table("core_postgres", "clients", "customers")
    for col in ("customer_id", "email", "segment"):
        p.column("core_postgres", "clients", "customers", col)
    assert len(calls) == 1


def test_unknown_table_refused_token_and_outage_all_mean_nothing_known():
    assert _provider(lambda r: httpx.Response(404, json={})).table("c", "s", "t") is None
    assert _provider(lambda r: httpx.Response(401, json={})).table("c", "s", "t") is None
    assert _provider(lambda r: httpx.Response(500, text="boom")).column("c", "s", "t", "x") is None

    def down(request):
        raise httpx.ConnectError("down")

    assert _provider(down).table("c", "s", "t") is None


def test_fqn_segments_are_url_encoded():
    seen = {}

    def handler(request):
        seen["path"] = request.url.raw_path.decode()
        return httpx.Response(404, json={})

    _provider(handler).table("core_postgres", "clients", "it's")
    assert "it%27s" in seen["path"]


def test_a_table_without_extras_gives_a_minimal_context():
    doc = {
        "fullyQualifiedName": "x",
        "description": "",
        "owners": [],
        "tags": [],
        "columns": [{"name": "a"}],
    }
    p = _provider(lambda r: httpx.Response(200, json=doc))
    assert p.table("c", "s", "t") is None
    assert p.column("c", "s", "t", "a") is None


def test_from_env_builds_the_provider_and_refuses_missing_settings():
    p = from_env(
        {
            "OPENMETADATA_URL": "http://om:8585/",
            "OPENMETADATA_TOKEN": "t",
            "OPENMETADATA_SERVICE": "trino-akko",
        },
        lambda sql: {"rows": []},
    )
    assert isinstance(p, OpenMetadataContext)
    with pytest.raises(ValueError):
        from_env({"OPENMETADATA_URL": "http://om"}, lambda sql: {"rows": []})


def test_json_that_is_not_a_table_is_nothing_known():
    p = _provider(lambda r: httpx.Response(200, content=b"not json"))
    assert p.table("c", "s", "t") is None


def test_other_constraint_types_are_ignored():
    doc = {**TABLE, "tableConstraints": [{"constraintType": "UNIQUE", "columns": ["email"]}]}
    t = _provider(lambda r: httpx.Response(200, json=doc)).table(
        "core_postgres", "clients", "customers"
    )
    assert t.grain == [] and t.joins == []
