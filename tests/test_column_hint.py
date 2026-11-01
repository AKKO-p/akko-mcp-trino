"""An unknown column: the error names the table of the agent's own statement to describe.

An agent that writes ``c.customer_id`` where the table has ``id`` gets Trino's error, kind
``column``; the hint sends it to ``describe_table`` on that table. The table comes from the
agent's statement only, never from Trino, so the hint shows nothing the caller could not
already name.
"""

import json

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
    ],
)
def test_the_tables_a_column_may_belong_to_come_from_the_statement(sql, identifier, tables):
    assert tables_for_column(sql, identifier) == tables


def _tools(raises):
    mcp = FakeMCP()
    register_query_tools(mcp, FakeClient(raises=raises))
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


def test_no_hint_when_no_table_of_the_statement_is_identifiable():
    out = json.loads(_tools(_column("x.nope"))["execute_query"](sql=_JOIN))
    assert out["kind"] == "column" and "hint" not in out


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
        assert "describe_table" in doc and '"hint"' in doc, name
