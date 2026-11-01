"""An unknown column: the error names the table of the agent's own statement to describe.

An agent that writes ``c.customer_id`` where the table has ``id`` gets Trino's error, kind
``column``; the hint sends it to ``describe_table`` on that table. The table comes from the
agent's statement only, never from Trino, so the hint shows nothing the caller could not
already name.
"""

import json
import re

import pytest

from akko_mcp_trino.sql_guard import tables_for_column
from akko_mcp_trino.tools import register_query_tools
from tests.test_tools import FakeClient, FakeMCP


class _TrinoError(Exception):
    def __init__(self, name, message):
        super().__init__(message)
        self.error_name, self.error_type, self.message = name, "USER_ERROR", message


def _column(name):
    return _TrinoError("COLUMN_NOT_FOUND", f"line 1:8: Column '{name}' cannot be resolved")


_JOIN = (
    "SELECT c.segment, a.balance FROM demo.crm.customers c "
    "JOIN demo.crm.accounts AS a ON a.customer_id = c.customer_id"
)


@pytest.mark.parametrize(
    ("sql", "identifier", "tables"),
    [
        (_JOIN, "c.customer_id", ["demo.crm.customers"]),
        (_JOIN, "a.nope", ["demo.crm.accounts"]),
        (_JOIN, "accounts.nope", ["demo.crm.accounts"]),
        (_JOIN, "demo.crm.accounts.nope", ["demo.crm.accounts"]),
        (_JOIN, "nope", ["demo.crm.accounts", "demo.crm.customers"]),
        (_JOIN, "x.nope", []),
        ("WITH t AS (SELECT 1 AS a) SELECT nope FROM t", "nope", []),
        ("SELECT nope", "nope", []),
        ("SELECT FROM WHERE (((", "nope", []),
        ("EXPLAIN SELECT nope FROM demo.crm.accounts", "nope", ["demo.crm.accounts"]),
        ("SELECT n.nope FROM tpch.tiny.nation n", "N.NOPE", ["tpch.tiny.nation"]),
        (
            "SELECT t.nope FROM TABLE(pg.system.query(query => 'select 1')) t",
            "t.nope",
            [],
        ),
        ("WITH x AS (SELECT id FROM demo.crm.t) SELECT foo FROM x", "foo", []),
        ("SELECT foo FROM (SELECT id FROM demo.crm.t) s", "foo", []),
        ("SELECT foo FROM demo.crm.t WHERE EXISTS (SELECT 1 FROM demo.crm.u)", "foo", []),
        ("SELECT foo FROM demo.crm.t WHERE id IN (SELECT id FROM demo.crm.u)", "foo", []),
        (
            "SELECT foo FROM demo.crm.t UNION ALL SELECT foo FROM demo.crm.u",
            "foo",
            ["demo.crm.t", "demo.crm.u"],
        ),
        (
            "SELECT t.foo FROM demo.crm.t t WHERE EXISTS (SELECT 1 FROM demo.crm.u)",
            "t.foo",
            ["demo.crm.t"],
        ),
        (
            "WITH x AS (SELECT 1 AS a) SELECT y.foo FROM demo.crm.t y JOIN x ON true",
            "y.foo",
            ["demo.crm.t"],
        ),
    ],
)
def test_the_tables_a_column_may_belong_to_come_from_the_statement(sql, identifier, tables):
    assert tables_for_column(sql, identifier) == tables


_SEEN = ("demo.crm.accounts", "demo.crm.customers", "tpch.tiny.nation")


class _Client(FakeClient):
    """The statement fails with ``raises``; a lookup of a table answers it when the caller
    sees it (``seen``), or fails with ``lookup`` when given."""

    def __init__(self, raises, seen=_SEEN, lookup=None):
        super().__init__()
        self.raises, self.seen, self.lookup = raises, seen, lookup

    def query(self, sql, user=None, params=None, **keywords):
        self.calls.append((sql, user))
        found = re.fullmatch(
            r"SELECT table_name FROM (\w+)\.information_schema\.tables "
            r"WHERE table_schema = '([^']*)' AND table_name = '([^']*)'",
            sql,
        )
        if not found:
            raise self.raises
        if self.lookup is not None:
            raise self.lookup
        name = ".".join(found.groups())
        rows = [[found.group(3)]] if name in self.seen else []
        return {"columns": ["table_name"], "rows": rows, "row_count": len(rows)}


_LAST: list = []  # the client of the last _tools, for the tests that read its calls


def _tools(raises, **kw):
    mcp = FakeMCP()
    client = _Client(raises, **kw)
    register_query_tools(mcp, client, session_catalog="demo")
    _LAST[:] = [client]
    return mcp.tools


@pytest.mark.parametrize("tool", ["execute_query", "explain_query"])
def test_an_unknown_column_advises_describe_table_on_its_table(tool):
    out = json.loads(_tools(_column("c.customer_id"))[tool](sql=_JOIN))
    assert out["kind"] == "column" and out["identifier"] == "c.customer_id"
    assert out["hint"] == (
        "Read the columns of demo.crm.customers with describe_table, then write the query "
        "again with a column it has."
    )
    assert out["error"] == "line 1:8: Column 'c.customer_id' cannot be resolved"


def test_the_advice_names_only_the_tables_the_caller_sees():
    """dave reads accounts but not customers: an unknown column without a qualifier names
    accounts alone, never the table he may not see."""
    out = json.loads(
        _tools(_column("nope"), seen=("demo.crm.accounts",))["execute_query"](sql=_JOIN)
    )
    assert out["hint"] == (
        "Read the columns of demo.crm.accounts with describe_table, then write the query "
        "again with a column it has."
    )
    hidden = json.loads(
        _tools(_column("c.customer_id"), seen=("demo.crm.accounts",))["execute_query"](sql=_JOIN)
    )
    assert "hint" not in hidden, "the only candidate is hidden: no advice"


def test_each_candidate_is_looked_up_by_equality_in_its_catalog():
    tools = _tools(_column("nope"))
    tools["execute_query"](sql=_JOIN)
    client = _client_of(tools)
    lookups = [sql for sql, _ in client.calls if "information_schema.tables" in sql]
    assert lookups == [
        "SELECT table_name FROM demo.information_schema.tables "
        "WHERE table_schema = 'crm' AND table_name = 'accounts'",
        "SELECT table_name FROM demo.information_schema.tables "
        "WHERE table_schema = 'crm' AND table_name = 'customers'",
    ]


@pytest.mark.parametrize(
    "lookup",
    [
        _TrinoError("PERMISSION_DENIED", "Access Denied: Cannot access catalog demo"),
        TimeoutError("the query exceeded its time budget"),
        ConnectionError("lost"),
    ],
)
def test_a_lookup_that_fails_gives_no_advice_and_keeps_the_error(lookup):
    out = json.loads(_tools(_column("c.customer_id"), lookup=lookup)["execute_query"](sql=_JOIN))
    assert out["kind"] == "column" and out["identifier"] == "c.customer_id"
    assert "hint" not in out


@pytest.mark.parametrize(
    ("sql", "looked_up"),
    [
        ("SELECT nope FROM crm.accounts", ["demo.information_schema.tables"]),
        ("SELECT nope FROM accounts", []),
        ('SELECT nope FROM "we\'ird".crm.accounts', []),
        ("SELECT nope FROM system.runtime.queries", []),
    ],
)
def test_a_name_is_looked_up_only_when_its_catalog_and_schema_are_known(sql, looked_up):
    tools = _tools(_column("nope"))
    out = json.loads(tools["execute_query"](sql=sql))
    calls = [c for c, _ in _client_of(tools).calls if "information_schema.tables" in c]
    assert [c.split(" FROM ")[1].split(" ")[0] for c in calls] == looked_up
    if not looked_up:
        assert "hint" not in out


def test_the_lookups_are_bounded():
    from akko_mcp_trino.tools import MAX_ADVICE_TABLES

    names = [f"demo.crm.t{i}" for i in range(MAX_ADVICE_TABLES + 3)]
    sql = "SELECT nope FROM " + ", ".join(names)
    tools = _tools(_column("nope"), seen=tuple(names))
    out = json.loads(tools["execute_query"](sql=sql))
    lookups = [c for c, _ in _client_of(tools).calls if "information_schema.tables" in c]
    assert len(lookups) == MAX_ADVICE_TABLES
    assert out["hint"].count("demo.crm.t") == MAX_ADVICE_TABLES


def _client_of(tools):
    return _LAST[0]


def test_no_hint_when_no_table_of_the_statement_is_identifiable():
    out = json.loads(_tools(_column("x.nope"))["execute_query"](sql=_JOIN))
    assert out["kind"] == "column" and "hint" not in out


@pytest.mark.parametrize(
    "exc",
    [
        _TrinoError("AMBIGUOUS_NAME", "line 1:8: Column 'id' is ambiguous"),
        _TrinoError("MISSING_COLUMN_NAME", "line 1:8: Column name not specified at position 1"),
        _TrinoError("COLUMN_NOT_FOUND", "line 1:8: a column cannot be resolved"),
    ],
)
def test_no_hint_for_a_column_that_exists_or_is_not_named(exc):
    """An ambiguous column exists (qualify it), an unnamed one needs an alias, and one Trino
    does not name cannot be placed: sending the agent to describe_table would be wrong."""
    out = json.loads(_tools(exc)["execute_query"](sql=_JOIN))
    assert out["kind"] == "column" and "hint" not in out


def test_a_column_of_several_tables_names_each_to_describe():
    out = json.loads(_tools(_column("nope"))["execute_query"](sql=_JOIN))
    assert out["hint"] == (
        "Read the columns of each of these tables with describe_table: demo.crm.accounts, "
        "demo.crm.customers. Then write the query again with a column one of them has."
    )


def test_a_parser_failure_never_costs_the_agent_its_error(monkeypatch):
    import akko_mcp_trino.tools as tools_module

    def broken(sql, identifier):
        raise RecursionError("the parser gave up")

    monkeypatch.setattr(tools_module, "tables_for_column", broken)
    out = json.loads(_tools(_column("c.customer_id"))["execute_query"](sql=_JOIN))
    assert out["kind"] == "column" and out["identifier"] == "c.customer_id"
    assert "hint" not in out


def test_no_hint_for_an_error_of_another_kind():
    denied = _TrinoError("PERMISSION_DENIED", "Access Denied: Cannot select from columns [x]")
    out = json.loads(_tools(denied)["execute_query"](sql=_JOIN))
    assert out["kind"] == "permission" and "hint" not in out


def test_the_hint_names_only_what_the_statement_wrote():
    """Trino's message may name more than the statement; the hint never repeats it."""
    exc = _column("c.customer_id")
    exc.message = "line 1:8: Column 'c.customer_id' cannot be resolved; secret_table has it"
    out = json.loads(_tools(exc)["execute_query"](sql=_JOIN))
    assert "secret_table" not in out["hint"]


def test_the_query_tools_say_what_a_column_error_adds():
    mcp = FakeMCP()
    register_query_tools(mcp, FakeClient())
    for name in ("execute_query", "explain_query"):
        doc = mcp.tools[name].__doc__
        assert "describe_table" in doc and 'may add "hint"' in " ".join(doc.split()), name
