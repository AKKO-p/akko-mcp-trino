"""The five core tools, characterised through a fake FastMCP.

Same outputs, same read-only guard, identity forwarded and never forgeable.
"""

import json

import pytest

from akko_mcp_trino.tools import register_query_tools


class FakeMCP:
    """Captures the functions decorated with @mcp.tool() so tests can call them directly."""

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
    """Guard: FastMCP 1.8.1 introspects annotations as CLASSES (issubclass). A
    `from __future__ import annotations` in a tools module would turn them into
    strings and crash the server at startup (seen in production). Forbidden."""
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
        "list_catalogs",
        "list_schemas",
        "list_tables",
        "describe_table",
        "execute_query",
    }


def test_list_catalogs_returns_json_array():
    mcp, _ = _registered(
        result={"columns": ["Catalog"], "rows": [["iceberg"], ["tpch"]], "row_count": 2}
    )
    assert json.loads(mcp.tools["list_catalogs"]()) == ["iceberg", "tpch"]


def test_list_schemas_validates_identifier():
    mcp, client = _registered(result={"columns": ["Schema"], "rows": [["fraud"]], "row_count": 1})
    assert json.loads(mcp.tools["list_schemas"]("iceberg")) == ["fraud"]
    assert "SHOW SCHEMAS FROM iceberg" in client.calls[-1][0]
    with pytest.raises(ValueError):
        mcp.tools["list_schemas"]("bad;name")


def test_list_tables_validates_both_identifiers():
    mcp, client = _registered(
        result={"columns": ["Table"], "rows": [["scores"], ["alerts"]], "row_count": 2}
    )
    assert json.loads(mcp.tools["list_tables"]("iceberg", "fraud")) == ["scores", "alerts"]
    assert "SHOW TABLES FROM iceberg.fraud" in client.calls[-1][0]
    with pytest.raises(ValueError):
        mcp.tools["list_tables"]("iceberg", "bad schema")


def test_describe_table_builds_qualified_name():
    mcp, client = _registered(
        result={"columns": ["Column", "Type"], "rows": [["id", "bigint"]], "row_count": 1}
    )
    out = json.loads(mcp.tools["describe_table"]("iceberg", "fraud", "scores"))
    assert out["columns"] == ["Column", "Type"]
    assert "DESCRIBE iceberg.fraud.scores" in client.calls[-1][0]


def test_execute_query_read_only_blocks_writes():
    mcp, client = _registered(read_only=True)
    out = json.loads(mcp.tools["execute_query"]("INSERT INTO t VALUES (1)"))
    assert "Read-only mode" in out["error"]
    assert client.calls == []  # never executed


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
    from akko_mcp_trino import identity
    from akko_mcp_trino.auth import Principal

    mcp, client = _registered(result={"columns": [], "rows": [], "row_count": 0})
    # identity = the verified Principal in the context, not a tool parameter
    tok = identity.set_current_principal(Principal(subject="dave_steward"))
    try:
        mcp.tools["execute_query"]("SELECT 1")
    finally:
        identity.reset_current_principal(tok)
    assert client.calls[-1][1] == "dave_steward"  # X-Trino-User = the authenticated user


def test_execute_query_falls_back_to_service_account_when_no_identity():
    mcp, client = _registered(result={"columns": [], "rows": [], "row_count": 0})
    mcp.tools["execute_query"]("SELECT 1")
    assert client.calls[-1][1] is None  # None → TrinoClient utilise le compte de service


def test_execute_query_no_longer_exposes_spoofable_user_param():
    mcp, _ = _registered()
    import inspect

    assert "user" not in inspect.signature(mcp.tools["execute_query"]).parameters


# ---- audit: every tool call leaves one record, never the token ----

from akko_mcp_trino.agents import reset_current_agent, set_current_agent  # noqa: E402
from akko_mcp_trino.audit import (  # noqa: E402
    InMemoryAudit,
    reset_current_request_id,
    set_current_request_id,
)
from akko_mcp_trino.auth import Principal  # noqa: E402
from akko_mcp_trino.identity import reset_current_principal, set_current_principal  # noqa: E402


def _registered_with_audit(read_only=True, **client_kw):
    mcp = FakeMCP()
    client = FakeClient(**client_kw)
    audit = InMemoryAudit()
    register_query_tools(mcp, client, read_only=read_only, audit=audit)
    return mcp, client, audit


def _in_request(fn):
    t1 = set_current_principal(Principal(subject="alice_admin", token_id="jti-9"))
    t2 = set_current_agent("cursor")
    t3 = set_current_request_id("req-1")
    try:
        return fn()
    finally:
        reset_current_request_id(t3)
        reset_current_agent(t2)
        reset_current_principal(t1)


def test_execute_query_records_who_what_and_from_which_product():
    mcp, _, audit = _registered_with_audit()
    _in_request(lambda: mcp.tools["execute_query"]("SELECT 1"))
    assert len(audit.events) == 1
    e = audit.events[0]
    assert (e.request_id, e.tool, e.subject, e.agent, e.token_id, e.ok) == (
        "req-1",
        "execute_query",
        "alice_admin",
        "cursor",
        "jti-9",
        True,
    )


def test_every_tool_is_audited():
    mcp, _, audit = _registered_with_audit()
    _in_request(
        lambda: (
            mcp.tools["list_catalogs"](),
            mcp.tools["list_schemas"]("c"),
            mcp.tools["list_tables"]("c", "s"),
            mcp.tools["describe_table"]("c", "s", "t"),
        )
    )
    assert [e.tool for e in audit.events] == [
        "list_catalogs",
        "list_schemas",
        "list_tables",
        "describe_table",
    ]


def test_refused_write_is_audited_as_failure():
    mcp, _, audit = _registered_with_audit()
    _in_request(lambda: mcp.tools["execute_query"]("DROP TABLE t"))
    assert audit.events[0].ok is False and "Read-only" in audit.events[0].error


def test_trino_error_is_audited_as_failure():
    mcp, _, audit = _registered_with_audit(raises=RuntimeError("Access Denied: Cannot select"))
    _in_request(lambda: mcp.tools["execute_query"]("SELECT 1"))
    assert audit.events[0].ok is False and "Access Denied" in audit.events[0].error


def test_invalid_identifier_is_audited_then_raised():
    mcp, _, audit = _registered_with_audit()
    with pytest.raises(ValueError):
        _in_request(lambda: mcp.tools["list_schemas"]("bad;name"))
    assert audit.events[0].ok is False and audit.events[0].tool == "list_schemas"


def test_outside_a_request_the_record_has_no_identity():
    mcp, _, audit = _registered_with_audit()
    mcp.tools["list_catalogs"]()
    e = audit.events[0]
    assert e.subject == "" and e.agent == "" and e.request_id == "" and e.token_id == ""


def test_no_audit_sink_means_no_record_and_no_error():
    mcp, _ = _registered()
    assert mcp.tools["list_catalogs"]()


def test_trailing_semicolon_is_stripped_before_trino():
    """Models end SQL with `;` by habit; Trino refuses it with a syntax error.
    Seen live with a Mistral agent that retried the same statement twelve times.
    One trailing semicolon is dropped; stacked statements are still refused."""
    mcp, client = _registered()
    mcp.tools["execute_query"]("SELECT 1 ;  ")
    assert client.calls[-1][0] == "SELECT 1"
    out = json.loads(mcp.tools["execute_query"]("SELECT 1; SELECT 2"))
    assert "error" in out and len(client.calls) == 1
