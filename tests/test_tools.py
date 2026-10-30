"""The eight core tools, characterised through a fake MCPServer.

Same outputs, same read-only guard; the caller comes from the verified principal, never
from an argument.
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
    def __init__(self, result=None, raises=None, results=None):
        self._result = result or {"columns": ["c"], "rows": [["v"]], "row_count": 1}
        # answered in order, before ``result``; an exception among them is raised instead
        self._results = list(results or [])
        self._raises = raises
        self.calls = []
        self.keywords = []  # the keywords of each query: row_limit, max_rows, timeout

    def query(self, sql, user=None, params=None, **keywords):
        self.calls.append((sql, user))
        self.keywords.append(keywords)
        if self._raises:
            raise self._raises
        out = self._results.pop(0) if self._results else self._result
        if isinstance(out, BaseException):
            raise out
        return out

    def ping(self):
        if self._raises:
            raise self._raises


_BUDGETS = ("describe_schema_tables", "describe_schema_columns")


def _registered(read_only=True, max_rows=100, max_catalogs=10000, **kw):
    budgets = {k: kw.pop(k) for k in _BUDGETS if k in kw}
    mcp = FakeMCP()
    client = FakeClient(**kw)
    register_query_tools(
        mcp, client, read_only=read_only, max_rows=max_rows, max_catalogs=max_catalogs, **budgets
    )
    return mcp, client


def _names(n, prefix="t"):
    return [[f"{prefix}{i:03d}"] for i in range(n)]


def test_tool_annotations_are_classes_not_strings():
    """Guard: the SDK introspects annotations as CLASSES (issubclass). A
    `from __future__ import annotations` in a tools module would turn them into
    strings and crash the server at startup (seen in production). Forbidden."""
    mcp, _ = _registered()
    for name, fn in mcp.tools.items():
        for param, annotation in getattr(fn, "__annotations__", {}).items():
            assert not isinstance(annotation, str), (
                f"{name}.{param} a une annotation string ({annotation!r}) — retire "
                "`from __future__ import annotations` du module d'outils."
            )


def test_exactly_the_eight_tools_are_registered():
    mcp, _ = _registered()
    assert set(mcp.tools) == {
        "list_catalogs",
        "list_schemas",
        "list_tables",
        "describe_schema",
        "describe_table",
        "explain_query",
        "execute_query",
        "search_columns",
    }


def test_list_catalogs_returns_the_names_and_whether_the_list_is_complete():
    mcp, _ = _registered(
        result={"columns": ["Catalog"], "rows": [["iceberg"], ["tpch"]], "row_count": 2}
    )
    assert json.loads(mcp.tools["list_catalogs"]()) == {
        "catalogs": ["iceberg", "tpch"],
        "next_offset": None,
        "truncated": False,
    }


def test_list_catalogs_hides_the_system_catalog():
    """The guard refuses `system` everywhere, so no tool points an agent at it."""
    mcp, _ = _registered(
        result={"columns": ["Catalog"], "rows": [["iceberg"], ["system"]], "row_count": 2}
    )
    assert json.loads(mcp.tools["list_catalogs"]()) == {
        "catalogs": ["iceberg"],
        "next_offset": None,
        "truncated": False,
    }


@pytest.mark.parametrize(
    "tool, args",
    [
        ("list_schemas", ("system",)),
        ("list_tables", ("SYSTEM", "runtime")),
        ("describe_schema", ("system", "jdbc")),
        ("describe_table", ("System", "runtime", "queries")),
        ("describe_table", ("system", "runtime", "queries", 20)),
    ],
)
def test_the_metadata_tools_refuse_the_system_catalog(tool, args):
    """`system` exposes engine state, such as other users' SQL in `system.runtime.queries`:
    the metadata tools refuse it before Trino, as execute_query does, sample rows included."""
    mcp, client = _registered()
    out = json.loads(mcp.tools[tool](*args))
    assert "system catalog" in out["error"] and client.calls == []


def test_list_schemas_validates_identifier():
    mcp, client = _registered(result={"columns": ["Schema"], "rows": [["fraud"]], "row_count": 1})
    assert json.loads(mcp.tools["list_schemas"]("iceberg")) == {
        "schemas": ["fraud"],
        "next_offset": None,
        "truncated": False,
    }
    assert client.calls[-1][0] == (
        "SELECT schema_name FROM iceberg.information_schema.schemata "
        "ORDER BY schema_name OFFSET 0 LIMIT 101"
    )
    out = json.loads(mcp.tools["list_schemas"]("bad;name"))
    assert "Invalid catalog" in out["error"] and len(client.calls) == 1, "nothing must reach Trino"


def test_list_tables_validates_both_identifiers():
    mcp, client = _registered(
        result={"columns": ["Table"], "rows": [["scores"], ["alerts"]], "row_count": 2}
    )
    assert json.loads(mcp.tools["list_tables"]("iceberg", "fraud")) == {
        "tables": ["scores", "alerts"],
        "next_offset": None,
        "truncated": False,
    }
    assert client.calls[-1][0] == (
        "SELECT table_name FROM iceberg.information_schema.tables "
        "WHERE table_schema = 'fraud' ORDER BY table_name OFFSET 0 LIMIT 101"
    )
    out = json.loads(mcp.tools["list_tables"]("iceberg", "bad schema"))
    assert "Invalid schema" in out["error"] and len(client.calls) == 1


# ---- pagination: offset in, next_offset out; a page is the server's row cap


def test_list_tables_gives_the_next_page_and_says_where_it_starts():
    mcp, client = _registered(result={"rows": _names(100), "row_count": 100, "truncated": True})
    out = json.loads(mcp.tools["list_tables"]("demo", "crm"))
    assert out["tables"] == [r[0] for r in _names(100)]
    assert out["next_offset"] == 100 and out["truncated"] is True
    assert client.calls[-1][0].endswith("ORDER BY table_name OFFSET 0 LIMIT 101")
    assert client.keywords[-1] == {}, "a page is the server's own row cap"


def test_the_next_page_starts_where_the_last_one_ended():
    mcp, client = _registered(result={"rows": _names(100), "row_count": 100, "truncated": True})
    out = json.loads(mcp.tools["list_tables"]("demo", "crm", offset=100))
    assert out["next_offset"] == 200
    assert client.calls[-1][0].endswith("ORDER BY table_name OFFSET 100 LIMIT 101")


def test_the_last_page_has_no_next_offset():
    mcp, client = _registered(result={"rows": _names(7), "row_count": 7, "truncated": False})
    out = json.loads(mcp.tools["list_tables"]("demo", "crm", offset=200))
    assert out["next_offset"] is None and out["truncated"] is False
    assert len(out["tables"]) == 7
    assert client.calls[-1][0].endswith("ORDER BY table_name OFFSET 200 LIMIT 101")


def test_a_page_holds_the_page_size_even_from_a_client_without_a_cap():
    """LIMIT asks Trino for one name more than a page: a client that applies no cap of its
    own still gives a page of page_size names and a next page."""
    mcp, client = _registered(
        max_rows=3, result={"rows": _names(4), "row_count": 4, "truncated": False}
    )
    out = json.loads(mcp.tools["list_tables"]("demo", "crm"))
    assert out["tables"] == ["t000", "t001", "t002"] and out["next_offset"] == 3
    assert client.calls[-1][0].endswith("OFFSET 0 LIMIT 4")
    out = json.loads(mcp.tools["list_schemas"]("demo", offset=6))
    assert out["schemas"] == ["t000", "t001", "t002"] and out["next_offset"] == 9


def test_list_tables_on_a_missing_or_hidden_schema_says_so_never_an_empty_list():
    """information_schema shows no row for a schema that does not exist or that the policy
    hides: an empty page is checked against the schemata the caller sees."""
    empty = {"rows": [], "row_count": 0, "truncated": False}
    mcp, client = _registered(results=[empty, empty])
    out = json.loads(mcp.tools["list_tables"]("demo", "CRMM"))
    assert out == {"error": "Schema demo.CRMM not found or not visible", "kind": "schema"}
    assert client.calls[-1][0] == (
        "SELECT schema_name FROM demo.information_schema.schemata WHERE schema_name = 'crmm'"
    )


def test_list_tables_on_a_visible_empty_schema_is_an_empty_last_page():
    empty = {"rows": [], "row_count": 0, "truncated": False}
    mcp, client = _registered(results=[empty, {"rows": [["crm"]], "row_count": 1}])
    out = json.loads(mcp.tools["list_tables"]("demo", "crm"))
    assert out == {"tables": [], "next_offset": None, "truncated": False}
    assert len(client.calls) == 2


def test_list_tables_reads_the_schema_as_trino_reads_an_unquoted_name():
    """Trino folds an unquoted name to lower case, as SHOW TABLES FROM demo.CRM did: the
    schema compared in information_schema is the folded one."""
    mcp, client = _registered()
    mcp.tools["list_tables"]("demo", "CRM")
    assert "WHERE table_schema = 'crm' ORDER BY" in client.calls[-1][0]


def test_list_schemas_pages_the_same_way():
    mcp, client = _registered(
        result={"rows": _names(100, "s"), "row_count": 100, "truncated": True}
    )
    out = json.loads(mcp.tools["list_schemas"]("demo", offset=300))
    assert out["next_offset"] == 400 and out["truncated"] is True
    assert client.calls[-1][0].endswith("ORDER BY schema_name OFFSET 300 LIMIT 101")


@pytest.mark.parametrize("bad", [-1, "1; DROP", 1.5, True, None])
@pytest.mark.parametrize(
    "tool, args",
    [
        ("list_catalogs", ()),
        ("list_schemas", ("demo",)),
        ("list_tables", ("demo", "crm")),
        ("describe_schema", ("demo", "crm")),
    ],
)
def test_a_bad_offset_is_refused_before_trino(tool, args, bad):
    mcp, client = _registered()
    out = json.loads(mcp.tools[tool](*args, offset=bad))
    assert out["kind"] == "invalid_argument" and "offset" in out["error"]
    assert client.calls == [], "nothing must reach Trino"


def test_check_offset_accepts_a_whole_number_from_zero():
    from akko_mcp_trino.tools import check_offset

    assert check_offset(0) == 0 and check_offset(250) == 250


def test_list_catalogs_reads_every_catalog_past_the_row_cap_and_pages_them():
    """SHOW CATALOGS is read whole, up to MCP_MAX_CATALOGS, past the row cap of 2, then
    paged in the server, sorted by name, without system."""
    rows = [["tpch"], ["system"], ["hive"], ["iceberg"], ["delta"], ["memory"]]
    mcp, client = _registered(
        max_rows=2, max_catalogs=50, result={"rows": rows, "row_count": 6, "truncated": False}
    )
    pages = [json.loads(mcp.tools["list_catalogs"](offset=o)) for o in (0, 2, 4)]
    assert [p["catalogs"] for p in pages] == [
        ["delta", "hive"],
        ["iceberg", "memory"],
        ["tpch"],
    ]
    assert [p["next_offset"] for p in pages] == [2, 4, None]
    assert [p["truncated"] for p in pages] == [True, True, False]
    assert client.calls[-1][0] == "SHOW CATALOGS"
    assert client.keywords[-1] == {"row_limit": 51}, "the bound, system aside, replaces the cap"


def test_an_offset_past_the_last_catalog_is_an_empty_last_page():
    mcp, _ = _registered(result={"rows": [["hive"]], "row_count": 1, "truncated": False})
    out = json.loads(mcp.tools["list_catalogs"](offset=10))
    assert out == {"catalogs": [], "next_offset": None, "truncated": False}


@pytest.mark.parametrize(
    "rows, cut",
    [
        (_names(4, "c"), False),  # one catalog more than the bound, system not among them
        (_names(4, "c"), True),  # SHOW CATALOGS itself goes past the bound and system
    ],
)
def test_list_catalogs_above_its_bound_is_an_error_never_a_cut_list(rows, cut):
    mcp, _ = _registered(
        max_catalogs=3, result={"rows": rows, "row_count": len(rows), "truncated": cut}
    )
    out = json.loads(mcp.tools["list_catalogs"]())
    assert out == {
        "error": "more than 3 catalogs are visible; raise the server's limit",
        "kind": "invalid_argument",
    }


def test_the_system_catalog_does_not_count_against_the_bound():
    rows = [*_names(3, "c"), ["system"]]
    mcp, client = _registered(max_catalogs=3, result={"rows": rows, "row_count": 4})
    out = json.loads(mcp.tools["list_catalogs"]())
    assert out["catalogs"] == ["c000", "c001", "c002"] and out["next_offset"] is None
    assert client.keywords[-1] == {"row_limit": 4}, "one row more than the bound, for system"


def test_listing_reply_refuses_the_truncated_flag_it_took_before():
    """A plugin written for listing_reply(key, items, truncated) fails loudly: False read as
    an offset would say a complete list has a next page."""
    from akko_mcp_trino.tools import listing_reply

    for flag in (True, False):
        with pytest.raises(TypeError, match="not a bool"):
            listing_reply("tables", ["a"], flag)
    assert json.loads(listing_reply("tables", ["a"], 1)) == {
        "tables": ["a"],
        "next_offset": 1,
        "truncated": True,
    }


def test_the_list_descriptions_say_how_to_get_the_next_page():
    mcp, _ = _registered()
    for name in ("list_catalogs", "list_schemas", "list_tables"):
        assert "offset=next_offset" in mcp.tools[name].__doc__, name


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


def test_the_guard_resolves_names_in_the_session_catalog():
    """With the session catalog `system` (the default), `runtime.queries` is refused
    before Trino by every tool that runs a statement."""
    mcp = FakeMCP()
    client = FakeClient()
    register_query_tools(mcp, client, session_catalog="system")
    for tool in ("execute_query", "explain_query"):
        out = json.loads(mcp.tools[tool](sql="SELECT query FROM runtime.queries"))
        assert "read-only" in out["error"].lower() or "read_only" in json.dumps(out)
    assert client.calls == []


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
    assert client.calls[-1][1] == "dave_steward"  # the verified caller, handed to the identity mode


def test_execute_query_falls_back_to_service_account_when_no_identity():
    mcp, client = _registered(result={"columns": [], "rows": [], "row_count": 0})
    mcp.tools["execute_query"]("SELECT 1")
    assert client.calls[-1][1] is None  # None → TrinoClient utilise le compte de service


def test_execute_query_no_longer_exposes_spoofable_user_param():
    mcp, _ = _registered()
    import inspect

    assert "user" not in inspect.signature(mcp.tools["execute_query"]).parameters


# ---- audit: every tool call leaves one record, never the token ----

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
    t3 = set_current_request_id("req-1")
    try:
        return fn()
    finally:
        reset_current_request_id(t3)
        reset_current_principal(t1)


def test_execute_query_records_who_called_which_tool_with_which_outcome():
    """The core's line: request id, tool, subject, outcome, duration. Nothing else is
    written: the token id, the SQL digest and the tables are an extension's."""
    mcp, _, audit = _registered_with_audit()
    _in_request(lambda: mcp.tools["execute_query"]("SELECT 1"))
    assert len(audit.events) == 1
    e = audit.events[0]
    assert (e.request_id, e.tool, e.subject, e.ok) == (
        "req-1",
        "execute_query",
        "alice_admin",
        True,
    )
    assert set(e.as_dict()) == {"request_id", "tool", "subject", "ok", "error", "duration_ms"}


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
    assert audit.events[0].ok is False and audit.events[0].error == "read_only"


def test_trino_error_is_audited_as_failure():
    mcp, _, audit = _registered_with_audit(raises=RuntimeError("Access Denied: Cannot select"))
    _in_request(lambda: mcp.tools["execute_query"]("SELECT 1"))
    assert audit.events[0].ok is False and audit.events[0].error == "RuntimeError"


def test_invalid_identifier_is_audited_as_a_failure():
    """An agent gets {"error": …} it can act on, and the audit line says it failed."""
    mcp, _, audit = _registered_with_audit()
    out = json.loads(_in_request(lambda: mcp.tools["list_schemas"]("bad;name")))
    assert "Invalid catalog" in out["error"]
    assert audit.events[0].ok is False and audit.events[0].tool == "list_schemas"


def test_outside_a_request_the_record_has_no_identity_but_its_own_call_id():
    """stdio has no HTTP request: each call still gets an id, so Trino's log joins it."""
    mcp, _, audit = _registered_with_audit()
    mcp.tools["list_catalogs"]()
    mcp.tools["list_catalogs"]()
    e = audit.events[0]
    assert e.subject == "" and e.principal is None
    assert len(e.request_id) == 32 and e.request_id != audit.events[1].request_id


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


# ---- every tool hands the verified caller to the identity mode, not only execute_query


def test_every_tool_hands_the_verified_caller_to_the_client():
    """Listing tools that queried Trino as the service
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


# ---- exploration tools


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


def test_explain_query_explains_reads_only():
    mcp, client = _registered(
        result={"columns": ["Query Plan"], "rows": [["Fragment 0"], ["  Output"]], "row_count": 2}
    )
    out = json.loads(mcp.tools["explain_query"]("SELECT 1;"))
    assert out["plan"] == "Fragment 0\n  Output" and client.calls[-1][0] == "EXPLAIN SELECT 1"
    assert (
        "error" in json.loads(mcp.tools["explain_query"]("DROP TABLE t")) and len(client.calls) == 1
    )


def test_listing_errors_are_returned_not_raised():
    mcp, _ = _registered(raises=RuntimeError("Access Denied"))
    for name, args in (
        ("list_catalogs", {}),
        ("list_schemas", {"catalog": "c"}),
        ("list_tables", {"catalog": "c", "schema": "s"}),
        ("describe_schema", {"catalog": "c", "schema": "s"}),
        ("describe_table", {"catalog": "c", "schema": "s", "table": "t"}),
    ):
        assert "Access Denied" in json.loads(mcp.tools[name](**args))["error"]


def test_tool_descriptions_tell_the_model_about_governed_values():
    mcp, _ = _registered()
    for name in ("execute_query", "describe_table"):
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
    from akko_mcp_trino.tools import audited

    audit = InMemoryAudit()

    def boom() -> str:
        raise RuntimeError("kaboom")

    with pytest.raises(RuntimeError):
        audited(audit, boom)()
    assert audit.events[0].ok is False and audit.events[0].error == "RuntimeError"


def test_results_with_dates_decimals_and_bytes_are_serialisable():
    """Found by a live adversarial proof: a table with a DATE column
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


def test_every_tool_declares_mcp_annotations_read_only_and_non_destructive():
    """MCP tool annotations (2025-06-18) let a host show that a tool only reads.
    Every tool here reads; none is destructive; listing is idempotent."""
    mcp, _ = _registered()
    for name, ann in mcp.annotations.items():
        assert ann.read_only_hint is True, name
        assert ann.destructive_hint is False, name
        assert ann.open_world_hint is False, name
        assert ann.title, name
    assert mcp.annotations["list_catalogs"].idempotent_hint is True


# ---- the annotations say what the tools can really do


def test_annotations_follow_the_read_only_setting():
    """A host skips its confirmation on a tool annotated read-only: with
    TRINO_READ_ONLY=false, execute_query may write and must say so. The other tools
    only read whatever the setting."""
    for read_only in (True, False):
        mcp = FakeMCP()
        register_query_tools(mcp, FakeClient(), read_only=read_only)
        run = mcp.annotations["execute_query"]
        assert run.read_only_hint is read_only
        assert run.destructive_hint is (not read_only)
        assert run.idempotent_hint is read_only
        for name in (
            "list_catalogs",
            "list_schemas",
            "list_tables",
            "describe_schema",
            "describe_table",
        ):
            assert mcp.annotations[name].read_only_hint is True
            assert mcp.annotations[name].destructive_hint is False
        assert mcp.annotations["explain_query"].read_only_hint is True


def test_the_description_of_execute_query_follows_the_read_only_setting():
    ro, rw = FakeMCP(), FakeMCP()
    register_query_tools(ro, FakeClient(), read_only=True)
    register_query_tools(rw, FakeClient(), read_only=False)
    assert ro.tools["execute_query"].__doc__.startswith("Run one read-only SQL statement")
    assert "are refused before reaching Trino" in ro.tools["execute_query"].__doc__
    assert ro.annotations["execute_query"].title == "Run a read-only query"
    assert rw.tools["execute_query"].__doc__.startswith("Run one SQL statement")
    assert "The statement may write" in rw.tools["execute_query"].__doc__
    assert "refused" not in rw.tools["execute_query"].__doc__.split("max_rows")[0]
    assert rw.annotations["execute_query"].title == "Run a query (writes allowed)"


class _TrinoUserError(Exception):
    def __init__(self, name, message):
        super().__init__(message)
        self.error_name, self.error_type, self.message = name, "USER_ERROR", message


def test_every_error_reply_keeps_error_and_adds_kind():
    exc = _TrinoUserError("TABLE_NOT_FOUND", "line 1:15: Table 'c.s.t' does not exist")
    mcp, _ = _registered(raises=exc)
    out = json.loads(mcp.tools["execute_query"]("SELECT 1"))
    assert out == {"error": str(exc), "kind": "table", "identifier": "c.s.t"}


def test_a_refused_write_has_kind_read_only():
    mcp, _ = _registered(read_only=True)
    for tool, args in (("execute_query", ("DROP TABLE t",)), ("explain_query", ("DROP TABLE t",))):
        assert json.loads(mcp.tools[tool](*args))["kind"] == "read_only"


def test_an_invalid_identifier_has_kind_invalid_argument():
    mcp, _ = _registered()
    out = json.loads(mcp.tools["list_schemas"]("bad;name"))
    assert out["kind"] == "invalid_argument" and "Invalid catalog" in out["error"]


def test_a_call_past_its_budget_has_kind_time_budget(monkeypatch):
    import akko_mcp_trino.audit as audit_module
    from akko_mcp_trino.tools import audited

    now = [0.0]
    monkeypatch.setattr(audit_module.time, "monotonic", lambda: now[0])

    def slow():
        now[0] += 8
        return json.dumps({"ok": 1})

    out = json.loads(audited(None, slow, budget_seconds=7)())
    assert out["kind"] == "time_budget" and "time budget" in out["error"]


def test_explain_query_says_it_checks_a_query_before_running_it():
    mcp, _ = _registered()
    assert "check a query before execute_query" in mcp.tools["explain_query"].__doc__


# ---- describe_schema: whole tables, their comments and columns, one page at a time


def _rows(rows, cut=False):
    return {"rows": [list(r) for r in rows], "row_count": len(rows), "truncated": cut}


_CRM_TABLES = [("customers", "BASE TABLE"), ("orders_v", "VIEW")]
_CRM_COMMENTS = [("customers", "One row per customer"), ("orders_v", None)]
_CRM_COLUMNS = [
    ("customers", "id", "bigint", "Customer key"),
    ("customers", "email", "varchar", None),
    ("orders_v", "total", "decimal(12,2)", "Order total, euros"),
]


def _describing(tables, columns, comments=(), **kw):
    """A server whose schema is visible, with these tables, comments and columns."""
    results = [_rows([("crm",)]), _rows(tables), _rows(comments), _rows(columns)]
    return _registered(results=results, **kw)


def test_describe_schema_returns_whole_tables_with_comments():
    mcp, client = _describing(_CRM_TABLES, _CRM_COLUMNS, _CRM_COMMENTS)
    out = json.loads(_in_request(lambda: mcp.tools["describe_schema"]("demo", "crm")))
    assert out == {
        "catalog": "demo",
        "schema": "crm",
        "tables": [
            {
                "table": "customers",
                "type": "BASE TABLE",
                "comment": "One row per customer",
                "columns": [
                    {"column": "id", "type": "bigint", "comment": "Customer key"},
                    {"column": "email", "type": "varchar", "comment": None},
                ],
            },
            {
                "table": "orders_v",
                "type": "VIEW",
                "comment": None,
                "columns": [
                    {"column": "total", "type": "decimal(12,2)", "comment": "Order total, euros"}
                ],
            },
        ],
        "next_offset": None,
        "shortened": False,
    }
    assert [sql for sql, _ in client.calls] == [
        "SELECT schema_name FROM demo.information_schema.schemata WHERE schema_name = 'crm'",
        "SELECT table_name, table_type FROM demo.information_schema.tables "
        "WHERE table_schema = 'crm' ORDER BY table_name OFFSET 0 LIMIT 21",
        "SELECT table_name, comment FROM system.metadata.table_comments "
        "WHERE catalog_name = 'demo' AND schema_name = 'crm' "
        "AND table_name IN ('customers', 'orders_v') ORDER BY table_name",
        "SELECT table_name, column_name, type_name, remarks FROM system.jdbc.columns "
        "WHERE table_cat = 'demo' AND table_schem = 'crm' "
        "AND table_name IN ('customers', 'orders_v') ORDER BY table_name, ordinal_position",
    ]
    assert client.keywords[1] == {"row_limit": 21}, "one table more than a page, never the cap"
    assert {u for _, u in client.calls} == {"alice_admin"}, "every query runs as the caller"


def test_describe_schema_reads_names_as_trino_folds_an_unquoted_name():
    mcp, client = _describing(_CRM_TABLES, _CRM_COLUMNS)
    out = json.loads(mcp.tools["describe_schema"]("Demo", "CRM"))
    assert (out["catalog"], out["schema"]) == ("demo", "crm")
    assert client.calls[0][0].endswith("WHERE schema_name = 'crm'")
    assert "WHERE table_cat = 'demo' AND table_schem = 'crm'" in client.calls[3][0]


def test_a_forbidden_and_a_missing_schema_answer_the_same():
    """information_schema.schemata, filtered by Trino, shows neither a schema the policy
    hides nor one that does not exist: both answer the same, so nothing is revealed."""
    answers = []
    for schema in ("hidden", "missing"):
        mcp, client = _registered(results=[_rows([])])
        answers.append(json.loads(mcp.tools["describe_schema"]("demo", schema)))
        assert len(client.calls) == 1, "nothing is read past the check"
    assert answers == [
        {"error": f"Schema demo.{s} not found or not visible", "kind": "schema"}
        for s in ("hidden", "missing")
    ]


def test_an_empty_visible_schema_is_an_empty_list():
    mcp, client = _registered(results=[_rows([("crm",)]), _rows([])])
    out = json.loads(mcp.tools["describe_schema"]("demo", "crm"))
    assert out == {
        "catalog": "demo",
        "schema": "crm",
        "tables": [],
        "next_offset": None,
        "shortened": False,
    }
    assert len(client.calls) == 2, "no metadata view is read for no table"


def test_a_full_page_says_where_the_next_one_starts():
    tables = [(f"t{i}", "BASE TABLE") for i in range(3)]
    columns = [(f"t{i}", "c", "bigint", None) for i in range(2)]
    mcp, client = _describing(tables, columns, describe_schema_tables=2)
    out = json.loads(mcp.tools["describe_schema"]("demo", "crm", offset=40))
    assert [t["table"] for t in out["tables"]] == ["t0", "t1"]
    assert out["next_offset"] == 42 and out["shortened"] is False
    assert client.calls[1][0].endswith("ORDER BY table_name OFFSET 40 LIMIT 3")
    assert "table_name IN ('t0', 't1')" in client.calls[3][0], "the extra row is not described"


def test_a_page_never_cuts_a_table_and_says_when_it_was_shortened():
    """Whole tables only: the page stops before the table that would pass the column
    budget, says it was shortened, and the next page starts at that table."""
    tables = [("a", "BASE TABLE"), ("b", "BASE TABLE"), ("c", "BASE TABLE")]
    columns = [("a", f"a{i}", "int", None) for i in range(3)]
    columns += [("b", f"b{i}", "int", None) for i in range(3)]
    columns += [("c", "c0", "int", None)]
    mcp, _ = _describing(tables, columns, describe_schema_tables=3, describe_schema_columns=5)
    out = json.loads(mcp.tools["describe_schema"]("demo", "crm", offset=6))
    assert [t["table"] for t in out["tables"]] == ["a"]
    assert len(out["tables"][0]["columns"]) == 3
    assert out["shortened"] is True and out["next_offset"] == 7


def test_a_table_wider_than_the_column_budget_still_comes_whole():
    tables = [("wide", "BASE TABLE"), ("b", "BASE TABLE")]
    columns = [("wide", f"c{i}", "int", None) for i in range(5)] + [("b", "x", "int", None)]
    mcp, _ = _describing(tables, columns, describe_schema_columns=2)
    out = json.loads(mcp.tools["describe_schema"]("demo", "crm"))
    assert [t["table"] for t in out["tables"]] == ["wide"]
    assert len(out["tables"][0]["columns"]) == 5
    assert out["shortened"] is True and out["next_offset"] == 1
    mcp, _ = _describing(tables[:1], columns[:5], describe_schema_columns=2)
    out = json.loads(mcp.tools["describe_schema"]("demo", "crm"))
    assert out["shortened"] is False and out["next_offset"] is None, "alone, it ends the schema"


def test_describe_schema_reads_every_column_past_the_row_cap():
    """The metadata views are read with every_row: a page the client cuts is followed by
    the next, from where it stopped, so no column is lost to the cap."""
    tables = [("a", "BASE TABLE")]
    first = [("a", "c0", "int", None), ("a", "c1", "int", None)]
    results = [
        _rows([("crm",)]),
        _rows(tables),
        _rows([]),
        _rows(first, cut=True),
        _rows([("a", "c2", "int", None)]),
    ]
    mcp, client = _registered(results=results, describe_schema_columns=2)
    out = json.loads(mcp.tools["describe_schema"]("demo", "crm"))
    assert [c["column"] for c in out["tables"][0]["columns"]] == ["c0", "c1", "c2"]
    assert client.calls[4][0].endswith("ORDER BY table_name, ordinal_position OFFSET 2")


def test_a_table_name_is_put_in_sql_as_an_escaped_literal():
    """Names come back from Trino; one with a quote still makes a literal, never SQL."""
    mcp, client = _describing([("o'brien", "BASE TABLE")], [])
    out = json.loads(mcp.tools["describe_schema"]("demo", "crm"))
    assert out["tables"][0]["columns"] == []
    assert "table_name IN ('o''brien')" in client.calls[3][0]


def test_describe_schema_refuses_the_system_catalog():
    mcp, client = _registered()
    for catalog in ("system", "SYSTEM"):
        out = json.loads(mcp.tools["describe_schema"](catalog, "jdbc"))
        assert out["kind"] == "invalid_argument" and "system catalog" in out["error"]
    out = json.loads(mcp.tools["describe_schema"]("demo", "crm'--"))
    assert "Invalid schema" in out["error"] and client.calls == []


@pytest.mark.parametrize("view", [2, 3])  # the comments, then the columns
def test_a_refused_metadata_view_says_what_to_allow(view):
    refused = _TrinoUserError(
        "PERMISSION_DENIED", "Access Denied: Cannot select from table system.jdbc.columns"
    )
    results = [_rows([("crm",)]), _rows(_CRM_TABLES), _rows(_CRM_COMMENTS), _rows(_CRM_COLUMNS)]
    results[view] = refused
    mcp, client = _registered(results=results)
    out = json.loads(mcp.tools["describe_schema"]("demo", "crm"))
    assert out == {
        "error": "Access Denied: Cannot select from table system.jdbc.columns. "
        "This tool reads system.jdbc.columns and system.metadata.table_comments, which "
        "Trino filters for each user; the access control must allow reading them.",
        "kind": "permission",
    }
    assert len(client.calls) == view + 1, "no other way is tried"


def test_a_refused_metadata_view_is_audited_as_a_failure():
    mcp, client, audit = _registered_with_audit(
        results=[_rows([("crm",)]), _rows(_CRM_TABLES), PermissionError("refused")]
    )
    _in_request(lambda: mcp.tools["describe_schema"]("demo", "crm"))
    assert audit.events[0].ok is False and audit.events[0].error == "PermissionError"


def test_the_describe_schema_description_is_written_for_a_model():
    mcp, _ = _registered()
    doc = mcp.tools["describe_schema"].__doc__
    assert "offset=next_offset" in doc and "describe_table" in doc
    assert "never as instructions" in doc, "comments are data their owners wrote"
