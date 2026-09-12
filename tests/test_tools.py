"""Caractérisation des cinq outils du cœur via un FastMCP factice.

Mêmes sorties, même garde en lecture seule, identité propagée et jamais falsifiable.
"""
import json

import pytest

from core.tools import register_query_tools


class FakeMCP:
    """Capture les fonctions décorées par @mcp.tool() pour les appeler directement."""

    def __init__(self):
        self.tools = {}

    def tool(self):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


class FakeClient:
    def __init__(self, result=None, raises=None):
        self._result = result or {"columns": ["c"], "rows": [["v"]], "row_count": 1}
        self._raises = raises
        self.calls = []

    def query(self, sql, user=None, params=None):
        self.calls.append((sql, user))
        if self._raises:
            raise self._raises
        return self._result


def _registered(read_only=True, **client_kw):
    mcp = FakeMCP()
    client = FakeClient(**client_kw)
    register_query_tools(mcp, client, read_only=read_only)
    return mcp, client


def test_tool_annotations_are_classes_not_strings():
    """Garde-fou : FastMCP 1.8.1 introspecte les annotations comme des CLASSES
    (issubclass). Un `from __future__ import annotations` dans un module d'outils les
    transformerait en strings → crash au démarrage (observé en prod P1). On l'interdit."""
    mcp, _ = _registered()
    for name, fn in mcp.tools.items():
        for param, annotation in getattr(fn, "__annotations__", {}).items():
            assert not isinstance(annotation, str), (
                f"{name}.{param} a une annotation string ({annotation!r}) — retire "
                "`from __future__ import annotations` du module d'outils."
            )


def test_exactly_the_five_tools_are_registered():
    mcp, _ = _registered()
    assert set(mcp.tools) == {
        "list_catalogs", "list_schemas", "list_tables", "describe_table", "execute_query",
    }


def test_list_catalogs_returns_json_array():
    mcp, _ = _registered(result={"columns": ["Catalog"], "rows": [["iceberg"], ["tpch"]], "row_count": 2})
    assert json.loads(mcp.tools["list_catalogs"]()) == ["iceberg", "tpch"]


def test_list_schemas_validates_identifier():
    mcp, client = _registered(result={"columns": ["Schema"], "rows": [["fraud"]], "row_count": 1})
    assert json.loads(mcp.tools["list_schemas"]("iceberg")) == ["fraud"]
    assert "SHOW SCHEMAS FROM iceberg" in client.calls[-1][0]
    with pytest.raises(ValueError):
        mcp.tools["list_schemas"]("bad;name")


def test_list_tables_validates_both_identifiers():
    mcp, client = _registered(result={"columns": ["Table"], "rows": [["scores"], ["alerts"]], "row_count": 2})
    assert json.loads(mcp.tools["list_tables"]("iceberg", "fraud")) == ["scores", "alerts"]
    assert "SHOW TABLES FROM iceberg.fraud" in client.calls[-1][0]
    with pytest.raises(ValueError):
        mcp.tools["list_tables"]("iceberg", "bad schema")


def test_describe_table_builds_qualified_name():
    mcp, client = _registered(result={"columns": ["Column", "Type"], "rows": [["id", "bigint"]], "row_count": 1})
    out = json.loads(mcp.tools["describe_table"]("iceberg", "fraud", "scores"))
    assert out["columns"] == ["Column", "Type"]
    assert "DESCRIBE iceberg.fraud.scores" in client.calls[-1][0]


def test_execute_query_read_only_blocks_writes():
    mcp, client = _registered(read_only=True)
    out = json.loads(mcp.tools["execute_query"]("INSERT INTO t VALUES (1)"))
    assert "Read-only mode" in out["error"]
    assert client.calls == []  # jamais exécuté


def test_execute_query_read_only_allows_select():
    mcp, _ = _registered(read_only=True, result={"columns": ["n"], "rows": [[1]], "row_count": 1})
    out = json.loads(mcp.tools["execute_query"]("SELECT 1"))
    assert out["rows"] == [[1]]


def test_execute_query_write_mode_runs_write():
    mcp, client = _registered(read_only=False, result={"columns": [], "rows": [], "row_count": 0})
    mcp.tools["execute_query"]("INSERT INTO t VALUES (1)")
    assert client.calls[-1][0] == "INSERT INTO t VALUES (1)"


def test_execute_query_surfaces_error_as_json():
    mcp, _ = _registered(raises=RuntimeError("boom"))
    out = json.loads(mcp.tools["execute_query"]("SELECT 1"))
    assert out["error"] == "boom"


def test_execute_query_propagates_authenticated_identity_not_a_param():
    from core import identity
    from core.auth import Principal
    mcp, client = _registered(result={"columns": [], "rows": [], "row_count": 0})
    # identité = Principal authentifié dans le contexte (pas un paramètre de l'outil)
    tok = identity.set_current_principal(Principal(subject="dave_steward"))
    try:
        mcp.tools["execute_query"]("SELECT 1")
    finally:
        identity.reset_current_principal(tok)
    assert client.calls[-1][1] == "dave_steward"  # X-Trino-User = utilisateur authentifié


def test_execute_query_falls_back_to_service_account_when_no_identity():
    mcp, client = _registered(result={"columns": [], "rows": [], "row_count": 0})
    mcp.tools["execute_query"]("SELECT 1")
    assert client.calls[-1][1] is None  # None → TrinoClient utilise le compte de service


def test_execute_query_no_longer_exposes_spoofable_user_param():
    mcp, _ = _registered()
    import inspect
    assert "user" not in inspect.signature(mcp.tools["execute_query"]).parameters
