"""describe_table reads a wide table page by page: every column within reach, none invented.

A page holds the server's row cap of columns, from ``column_offset``; ``next_column_offset``
says where the next page starts, and ``columns_cut`` that the table has more columns than the
server reads (``MCP_DESCRIBE_MAX_COLUMNS``). The context of a catalogue follows the page: only
the columns Trino listed on it, and never a grain or a join that needs one it did not.
"""

import json
from types import MappingProxyType

import pytest

from akko_mcp_trino.context import ColumnContext, TableContext, TableRef
from akko_mcp_trino.tools import register_query_tools
from tests.context.memory_provider import Memory
from tests.tool_doubles import FakeMCP, GateTrino, in_request, with_context

_HEADER = ["Column", "Type", "Extra", "Comment"]


def _names(n):
    return [f"c{i:03d}" for i in range(n)]


class Wide:
    """Trino for one table of ``n`` columns: DESCRIBE answers as the client does, its row
    limit cutting the answer and saying so (``truncated``)."""

    def __init__(self, n, cap=100):
        self.rows = [[c, "bigint", "", ""] for c in _names(n)]
        self.cap = cap
        self.calls = []

    def query(self, sql, user=None, params=None, **keywords):
        self.calls.append((sql, keywords))
        limit = keywords.get("row_limit") or self.cap
        if sql.startswith("DESCRIBE "):
            rows = self.rows
        else:
            rows = [[1]]
        return {
            "columns": _HEADER,
            "rows": rows[:limit],
            "row_count": min(limit, len(rows)),
            "truncated": len(rows) > limit,
        }


def _describe(n, *, max_rows=100, max_columns=5000):
    mcp, trino = FakeMCP(), Wide(n, cap=max_rows)
    register_query_tools(mcp, trino, max_rows=max_rows, describe_max_columns=max_columns)
    tool = mcp.tools["describe_table"]
    return (lambda **kw: json.loads(tool("demo", "sales", "wide", **kw))), trino


def test_a_wide_table_is_read_page_by_page_to_its_last_column():
    describe, trino = _describe(250)
    first = describe()
    assert [r[0] for r in first["rows"]] == _names(100)
    assert (first["truncated"], first["next_column_offset"], first["columns_cut"]) == (
        True,
        100,
        False,
    )
    second = describe(column_offset=100)
    assert [r[0] for r in second["rows"]] == _names(200)[100:]
    assert second["next_column_offset"] == 200
    last = describe(column_offset=200)
    assert [r[0] for r in last["rows"]] == _names(250)[200:]
    assert (last["truncated"], last["next_column_offset"], last["columns_cut"]) == (
        True,
        None,
        False,
    )
    assert [kw["row_limit"] for _, kw in trino.calls] == [100, 200, 300]


def test_a_table_that_fits_in_one_page_has_no_next_page():
    describe, _ = _describe(7)
    out = describe()
    assert (out["truncated"], out["next_column_offset"], out["columns_cut"]) == (False, None, False)


def test_past_the_most_columns_the_server_reads_the_answer_says_so():
    describe, trino = _describe(250, max_columns=150)
    out = describe(column_offset=100)
    assert [r[0] for r in out["rows"]] == _names(150)[100:], "never a column past the bound"
    assert (out["next_column_offset"], out["columns_cut"]) == (None, True)
    assert trino.calls[-1][1] == {"row_limit": 150}
    refused = describe(column_offset=150)
    assert refused["kind"] == "invalid_argument" and "MCP_DESCRIBE_MAX_COLUMNS" in refused["error"]
    assert len(trino.calls) == 1, "refused before Trino"


@pytest.mark.parametrize("offset", [-1, True, 1.5, "3"])
def test_a_column_offset_that_is_not_a_whole_number_is_refused_before_trino(offset):
    describe, trino = _describe(10)
    out = describe(column_offset=offset)
    assert out["kind"] == "invalid_argument" and trino.calls == []


def test_sample_rows_come_with_the_first_page_only():
    describe, trino = _describe(250)
    assert describe(sample_rows=1)["sample"]["rows"] == [[1]]
    calls = len(trino.calls)
    out = describe(column_offset=100, sample_rows=1)
    assert out["kind"] == "invalid_argument" and "column_offset" in out["error"]
    assert len(trino.calls) == calls, "refused before Trino"


def test_a_describer_receives_the_columns_of_its_page():
    mcp, trino = FakeMCP(), Wide(250)
    tools = register_query_tools(mcp, trino)
    seen = []
    tools.add_describer(lambda cat, sch, tbl, rows: seen.append([r[0] for r in rows]) or {})
    mcp.tools["describe_table"]("demo", "sales", "wide", column_offset=100)
    assert seen == [_names(200)[100:]]


def test_the_description_says_how_to_read_the_next_page():
    describe_doc = _describe_doc()
    assert "column_offset=next_column_offset" in describe_doc
    assert "columns_cut" in describe_doc


def _describe_doc():
    mcp = FakeMCP()
    register_query_tools(mcp, Wide(1))
    return mcp.tools["describe_table"].__doc__


# ---- the context of a catalogue, on each page


class WideGate(GateTrino):
    """The table demo.sales.orders of ``n`` columns, its DESCRIBE cut by the row limit."""

    def __init__(self, n):
        super().__init__(columns=_names(n))

    def query(self, sql, user=None, params=None, **keywords):
        out = super().query(sql, user, params, **keywords)
        limit = keywords.get("row_limit")
        if sql.startswith("DESCRIBE ") and limit:
            out = {**out, "rows": out["rows"][:limit], "truncated": len(out["rows"]) > limit}
        return out


@pytest.fixture
def setups():
    made = []
    yield made
    for setup in made:
        setup.close()


def _orders_context():
    return Memory(
        tables={
            TableRef("demo", "sales", "orders"): TableContext(
                description="Orders",
                grain=("c000", "c150"),
                columns=MappingProxyType(
                    {
                        "c000": ColumnContext(description="First"),
                        "c150": ColumnContext(description="Far"),
                    }
                ),
            )
        }
    )


def test_each_page_serves_the_context_of_its_own_columns_only(setups):
    mcp, _, _ = with_context(setups, WideGate(250), _orders_context())

    def page(offset):
        return json.loads(
            in_request(
                lambda: mcp.tools["describe_table"]("demo", "sales", "orders", column_offset=offset)
            )
        )

    first, second = page(0), page(100)
    assert list(first["column_context"]) == ["c000"] and "Far" not in json.dumps(first)
    assert list(second["column_context"]) == ["c150"] and "First" not in json.dumps(second)
    for out in (first, second):
        assert "grain" not in out["table"], "a grain needs every column it names proven"
        assert out["context_sources"] == {"catalog": "partial"}


def test_a_table_read_in_one_page_keeps_its_grain(setups):
    provider = Memory(
        tables={TableRef("demo", "sales", "orders"): TableContext(grain=("c000", "c001"))}
    )
    mcp, _, _ = with_context(setups, WideGate(2), provider)
    out = json.loads(in_request(lambda: mcp.tools["describe_table"]("demo", "sales", "orders")))
    assert out["table"]["grain"] == ["c000", "c001"]
    assert out["context_sources"] == {"catalog": "ok"}
