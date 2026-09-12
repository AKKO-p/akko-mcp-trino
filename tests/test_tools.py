"""The eight core tools, characterised through a fake FastMCP.

Same outputs, same read-only guard, identity forwarded and never forgeable.
"""

import json

import pytest

from akko_mcp_trino.tools import register_query_tools


class FakeMCP:
    """Captures the functions decorated with @mcp.tool() so tests can call them directly."""

    def __init__(self):
        self.tools = {}
        self.annotations = {}

    def tool(self, annotations=None, **kw):
        def deco(fn):
            self.tools[fn.__name__] = fn
            self.annotations[fn.__name__] = annotations
            return fn

        return deco


class FakeClient:
    def __init__(self, result=None, raises=None):
        self._result = result or {"columns": ["c"], "rows": [["v"]], "row_count": 1}
        self._raises = raises
        self.calls = []

    def query(self, sql, user=None, params=None, bearer=None):
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


def test_exactly_the_nine_tools_are_registered():
    mcp, _ = _registered()
    assert set(mcp.tools) == {
        "list_catalogs",
        "list_schemas",
        "list_tables",
        "describe_table",
        "search_columns",
        "profile_table",
        "explain_query",
        "explain_table",
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
    out = json.loads(mcp.tools["list_schemas"]("bad;name"))
    assert "Invalid catalog" in out["error"] and len(client.calls) == 1, "nothing must reach Trino"


def test_list_tables_validates_both_identifiers():
    mcp, client = _registered(
        result={"columns": ["Table"], "rows": [["scores"], ["alerts"]], "row_count": 2}
    )
    assert json.loads(mcp.tools["list_tables"]("iceberg", "fraud")) == ["scores", "alerts"]
    assert "SHOW TABLES FROM iceberg.fraud" in client.calls[-1][0]
    out = json.loads(mcp.tools["list_tables"]("iceberg", "bad schema"))
    assert "Invalid schema" in out["error"] and len(client.calls) == 1


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


def test_invalid_identifier_is_audited_as_a_failure():
    """An agent gets {"error": …} it can act on, and the audit line says it failed."""
    mcp, _, audit = _registered_with_audit()
    out = json.loads(_in_request(lambda: mcp.tools["list_schemas"]("bad;name")))
    assert "Invalid catalog" in out["error"]
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


# ---- every tool runs under the caller's identity, not only execute_query


def test_every_tool_forwards_the_callers_identity_to_trino():
    """Found while reviewing 0.2: discovery tools queried Trino as the service
    account, so a restricted user could list catalogs, schemas, tables and
    columns the engine would hide from them. Metadata is data."""
    mcp, client = _registered()
    calls = [
        ("list_catalogs", {}),
        ("list_schemas", {"catalog": "c"}),
        ("list_tables", {"catalog": "c", "schema": "s"}),
        ("describe_table", {"catalog": "c", "schema": "s", "table": "t"}),
        ("execute_query", {"sql": "SELECT 1"}),
    ]
    _in_request(lambda: [mcp.tools[n](**a) for n, a in calls])
    assert [u for _, u in client.calls] == ["alice_admin"] * len(calls)


# ---- 0.2 exploration tools


def test_describe_table_can_add_a_governed_sample():
    mcp, client = _registered()
    out = json.loads(_in_request(lambda: mcp.tools["describe_table"]("c", "s", "t", sample_rows=5)))
    assert out["sample"]["rows"] == [["v"]]
    assert client.calls[-1] == ("SELECT * FROM c.s.t LIMIT 5", "alice_admin")


def test_describe_table_sample_is_capped_and_never_negative():
    mcp, client = _registered()
    mcp.tools["describe_table"]("c", "s", "t", sample_rows=999)
    assert client.calls[-1][0].endswith("LIMIT 20")
    calls_before = len(client.calls)
    mcp.tools["describe_table"]("c", "s", "t", sample_rows=-3)
    assert len(client.calls) == calls_before + 1, "a negative sample must not query data"


def test_search_columns_builds_a_safe_like_over_information_schema():
    mcp, client = _registered(
        result={
            "columns": ["s", "t", "c", "ty"],
            "rows": [["clients", "customers", "email", "varchar"]],
            "row_count": 1,
        }
    )
    out = json.loads(mcp.tools["search_columns"]("%E'Mail%", catalog="core_postgres"))
    sql = client.calls[-1][0]
    assert "core_postgres.information_schema.columns" in sql and "LIKE '%e''mail%'" in sql
    assert out == [
        {
            "catalog": "core_postgres",
            "schema": "clients",
            "table": "customers",
            "column": "email",
            "type": "varchar",
        }
    ]


def test_search_columns_without_catalog_walks_every_visible_catalog():
    mcp, client = _registered(result={"columns": ["a"], "rows": [["x"]], "row_count": 1})
    mcp.tools["search_columns"]("%id%")
    assert client.calls[0][0] == "SHOW CATALOGS"
    assert len(client.calls) == 2  # one catalog "x" came back, one search on it


def test_search_columns_requires_a_pattern():
    mcp, _ = _registered()
    assert "error" in json.loads(mcp.tools["search_columns"]("   "))


def test_search_columns_rejects_an_injected_catalog():
    mcp, _ = _registered()
    assert (
        "Invalid catalog" in json.loads(mcp.tools["search_columns"]("%x%", catalog="a;b"))["error"]
    )


def test_profile_table_uses_show_stats():
    mcp, client = _registered()
    mcp.tools["profile_table"]("c", "s", "t")
    assert client.calls[-1][0] == "SHOW STATS FOR c.s.t"


def test_explain_query_explains_reads_only():
    mcp, client = _registered(
        result={"columns": ["Query Plan"], "rows": [["Fragment 0"], ["  Output"]], "row_count": 2}
    )
    out = json.loads(mcp.tools["explain_query"]("SELECT 1;"))
    assert out["plan"] == "Fragment 0\n  Output" and client.calls[-1][0] == "EXPLAIN SELECT 1"
    assert (
        "error" in json.loads(mcp.tools["explain_query"]("DROP TABLE t")) and len(client.calls) == 1
    )


def test_discovery_errors_are_returned_not_raised():
    mcp, _ = _registered(raises=RuntimeError("Access Denied"))
    for name, args in (
        ("list_catalogs", {}),
        ("list_schemas", {"catalog": "c"}),
        ("profile_table", {"catalog": "c", "schema": "s", "table": "t"}),
    ):
        assert "Access Denied" in json.loads(mcp.tools[name](**args))["error"]


def test_tool_descriptions_tell_the_model_about_governed_values():
    mcp, _ = _registered()
    for name in ("execute_query", "describe_table", "search_columns", "profile_table"):
        doc = mcp.tools[name].__doc__ or ""
        assert "masked" in doc and "not an error" in doc, (
            f"{name} does not explain masking to the model"
        )


def test_describe_and_explain_return_trino_errors_as_json():
    mcp, _ = _registered(raises=RuntimeError("Access Denied: Cannot select"))
    assert "Access Denied" in json.loads(mcp.tools["describe_table"]("c", "s", "t"))["error"]
    assert "Access Denied" in json.loads(mcp.tools["explain_query"]("SELECT 1"))["error"]


def test_audit_records_an_exception_raised_by_a_tool_then_reraises():
    """The audited wrapper must not swallow a raise from a custom registrar's tool."""
    from akko_mcp_trino.tools import _audited

    audit = InMemoryAudit()

    def boom() -> str:
        raise RuntimeError("kaboom")

    with pytest.raises(RuntimeError):
        _audited(audit, boom)()
    assert audit.events[0].ok is False and "kaboom" in audit.events[0].error


def test_results_with_dates_decimals_and_bytes_are_serialisable():
    """Found by the live adversarial proof on 0.2: a table with a DATE column
    made every tool fail with 'Object of type date is not JSON serializable'.
    Trino returns dates, timestamps, decimals and varbinary as Python objects."""
    import datetime as dt
    import decimal

    row = [
        dt.date(2024, 12, 31),
        dt.datetime(2024, 12, 31, 23, 59, 5),
        decimal.Decimal("12.50"),
        b"\x01\xff",
        None,
    ]
    mcp, _ = _registered(
        result={"columns": ["d", "ts", "amount", "blob", "n"], "rows": [row], "row_count": 1}
    )
    out = json.loads(mcp.tools["execute_query"]("SELECT 1"))
    assert out["rows"][0] == ["2024-12-31", "2024-12-31T23:59:05", "12.50", "01ff", None]
    sample = json.loads(mcp.tools["describe_table"]("c", "s", "t", sample_rows=1))
    assert sample["sample"]["rows"][0][0] == "2024-12-31"


def test_uuid_is_serialised_and_unknown_types_still_fail_loudly():
    import uuid

    from akko_mcp_trino.tools import dumps

    assert json.loads(dumps({"u": uuid.UUID(int=1)}))["u"] == "00000000-0000-0000-0000-000000000001"
    with pytest.raises(TypeError):
        dumps({"x": object()})


def test_tools_pass_the_callers_bearer_to_the_client_for_jwt_passthrough():
    """The bearer travels in its own ContextVar, never inside the Principal or the audit."""
    from akko_mcp_trino.identity import reset_current_bearer, set_current_bearer

    class _Client(FakeClient):
        def query(self, sql, user=None, params=None, bearer=None):
            self.calls.append((sql, user, bearer))
            return self._result

    mcp = FakeMCP()
    client = _Client()
    register_query_tools(mcp, client)
    t = set_current_bearer("eyJ.raw")
    try:
        mcp.tools["execute_query"]("SELECT 1")
        mcp.tools["list_catalogs"]()
    finally:
        reset_current_bearer(t)
    assert all(c[2] == "eyJ.raw" for c in client.calls)
    mcp.tools["list_catalogs"]()
    assert client.calls[-1][2] is None, "the bearer must be cleared after the request"


def test_every_tool_declares_mcp_annotations_read_only_and_non_destructive():
    """MCP tool annotations (2025-06-18) let a host show that a tool only reads.
    Every tool here reads; none is destructive; discovery is idempotent."""
    mcp, _ = _registered()
    for name, ann in mcp.annotations.items():
        assert ann.readOnlyHint is True, name
        assert ann.destructiveHint is False, name
        assert ann.openWorldHint is False, name
        assert ann.title, name
    assert mcp.annotations["list_catalogs"].idempotentHint is True


# ---- 0.3: context providers enrich describe_table and power explain_table


class _Ctx:
    def __init__(self):
        from akko_mcp_trino.context import ColumnContext, Join, TableContext

        self.t = TableContext(
            description="One row per customer",
            owner="Customer data team",
            tier="gold",
            grain=["customer_id"],
            joins=[Join(["customer_id"], "core_postgres.clients.accounts", ["customer_id"])],
            tags=["pii"],
        )
        self.c = {"email": ColumnContext(description="Contact address", classification=["PII"])}

    def table(self, cat, sch, tbl):
        return self.t if tbl == "customers" else None

    def column(self, cat, sch, tbl, col):
        return self.c.get(col)


def _registered_with_context(**client_kw):
    mcp = FakeMCP()
    client = FakeClient(**client_kw)
    register_query_tools(mcp, client, context=_Ctx())
    return mcp, client


def test_describe_table_carries_the_table_and_column_context():
    rows = [["customer_id", "bigint", "", ""], ["email", "varchar", "", ""]]
    mcp, _ = _registered_with_context(
        result={"columns": ["Column", "Type", "Extra", "Comment"], "rows": rows, "row_count": 2}
    )
    out = json.loads(mcp.tools["describe_table"]("core_postgres", "clients", "customers"))
    assert out["table"]["owner"] == "Customer data team" and out["table"]["grain"] == [
        "customer_id"
    ]
    assert out["table"]["joins"][0]["target"] == "core_postgres.clients.accounts"
    assert out["column_context"] == {
        "email": {"description": "Contact address", "classification": ["PII"]}
    }


def test_describe_table_without_context_keeps_the_0_2_shape():
    mcp, _ = _registered(
        result={
            "columns": ["Column", "Type", "Extra", "Comment"],
            "rows": [["a", "int", "", ""]],
            "row_count": 1,
        }
    )
    out = json.loads(mcp.tools["describe_table"]("c", "s", "t"))
    assert "table" not in out and "column_context" not in out


def test_explain_table_answers_from_context_and_says_when_nothing_is_known():
    mcp, _ = _registered_with_context()
    out = json.loads(mcp.tools["explain_table"]("core_postgres", "clients", "customers"))
    assert out["grain"] == ["customer_id"] and out["tier"] == "gold" and out["tags"] == ["pii"]
    nothing = json.loads(mcp.tools["explain_table"]("core_postgres", "clients", "orders"))
    assert nothing == {"known": False}


def test_explain_table_validates_identifiers():
    mcp, _ = _registered_with_context()
    assert "Invalid table" in json.loads(mcp.tools["explain_table"]("c", "s", "x;y"))["error"]


def test_context_provider_failures_never_break_describe_table():
    class _Broken:
        def table(self, *a):
            raise RuntimeError("catalogue down")

        def column(self, *a):
            raise RuntimeError("catalogue down")

    mcp = FakeMCP()
    register_query_tools(
        mcp,
        FakeClient(
            result={
                "columns": ["Column", "Type", "Extra", "Comment"],
                "rows": [["a", "int", "", ""]],
                "row_count": 1,
            }
        ),
        context=_Broken(),
    )
    out = json.loads(mcp.tools["describe_table"]("c", "s", "t"))
    assert out["rows"] == [["a", "int", "", ""]] and "error" not in out
