"""profile_table: one aggregate query, run as the caller, over the columns DESCRIBE listed.

The caller learns nothing they could not compute with execute_query: the aggregates run as
them, so masks and row filters apply, and nothing but the aggregates comes back.
"""

import datetime as dt
import decimal
import json
import threading

import pytest

from akko_mcp_trino import tools as tools_module
from akko_mcp_trino import trino_client
from akko_mcp_trino.audit import InMemoryAudit
from akko_mcp_trino.audit import time as audit_time
from akko_mcp_trino.server import build_server
from akko_mcp_trino.tools import register_query_tools
from tests.tool_doubles import FakeMCP, YearToMonth, core_config, in_request

ORDERS = "tpch.tiny.orders"
DESCRIBED = {
    "columns": ["Column", "Type", "Extra", "Comment"],
    "rows": [["id", "bigint", "", ""], ["name", "varchar(25)", "", ""]],
    "row_count": 2,
    "truncated": False,
}


def _described(*columns):
    """DESCRIBE's answer for ``columns``, (name, type) pairs."""
    rows = [[name, kind, "", ""] for name, kind in columns]
    return {"columns": DESCRIBED["columns"], "rows": rows, "row_count": len(rows)}


def _aggregates(*row):
    return {"columns": [f"_col{i}" for i in range(len(row))], "rows": [list(row)], "row_count": 1}


class Trino:
    """Answers in order; records each statement and its keywords."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []
        self.keywords = []

    def query(self, sql, user=None, params=None, **keywords):
        self.calls.append(sql)
        self.keywords.append(keywords)
        out = self.answers.pop(0)
        if isinstance(out, BaseException):
            raise out
        if callable(out):
            return out()
        return out


def _profile(*answers, max_columns=50, sample_rows=100000, audit=None, budget=0.0, **settings):
    mcp, trino = FakeMCP(), Trino(*answers)
    register_query_tools(
        mcp,
        trino,
        audit=audit,
        call_timeout_seconds=budget,
        profile_max_columns=max_columns,
        profile_sample_rows=sample_rows,
        **settings,
    )
    return mcp.tools["profile_table"], trino


def _call(tool, *args, **kwargs):
    return json.loads(in_request(lambda: tool(*args, **kwargs)))


def test_profile_table_sends_one_aggregate_query_and_never_show_stats():
    tool, trino = _profile(DESCRIBED, _aggregates(3, 3, 3, 1, 9, 2, 2, "ann", "zoe"))
    out = _call(tool, "tpch", "tiny", "orders")
    assert trino.calls == [
        f"DESCRIBE {ORDERS}",
        'SELECT count(*), count("id"), approx_distinct("id"), min("id"), max("id"), '
        'count("name"), approx_distinct("name"), min("name"), max("name") '
        f'FROM (SELECT "id", "name" FROM {ORDERS} LIMIT 100000)',
    ]
    assert not [s for s in trino.calls if s.lstrip().upper().startswith(("SHOW", "EXPLAIN"))]
    assert out == {
        "table": ORDERS,
        "scope": "first_rows",
        "rows": 3,
        "columns_cut": False,
        "columns": [
            {
                "column": "id",
                "type": "bigint",
                "non_null": 3,
                "null_fraction": 0.0,
                "approx_distinct": 3,
                "min": 1,
                "max": 9,
            },
            {
                "column": "name",
                "type": "varchar(25)",
                "non_null": 2,
                "null_fraction": 1 - 2 / 3,
                "approx_distinct": 2,
                "min": "ann",
                "max": "zoe",
            },
        ],
    }


@pytest.mark.parametrize(
    "columns",
    [["id", "secret"], ["ID"], ["id", "id"], [], ["id", 7]],
    ids=["unlisted", "other-case", "twice", "none", "not-a-name"],
)
def test_a_column_describe_did_not_list_is_refused_before_any_aggregate(columns):
    tool, trino = _profile(DESCRIBED)
    out = _call(tool, "tpch", "tiny", "orders", columns=columns)
    assert out["kind"] == "invalid_argument"
    assert trino.calls == [f"DESCRIBE {ORDERS}"], "no aggregate is sent"


def test_the_named_columns_are_profiled_in_the_order_given():
    tool, trino = _profile(DESCRIBED, _aggregates(1, 1, 1, "a", "a"))
    out = _call(tool, "tpch", "tiny", "orders", columns=["name"])
    assert trino.calls[1] == (
        'SELECT count(*), count("name"), approx_distinct("name"), min("name"), max("name") '
        f'FROM (SELECT "name" FROM {ORDERS} LIMIT 100000)'
    )
    assert [c["column"] for c in out["columns"]] == ["name"]


def test_by_default_the_first_columns_describe_lists_are_profiled_up_to_the_limit():
    three = _described(("a", "bigint"), ("b", "bigint"), ("c", "bigint"))
    tool, trino = _profile(three, _aggregates(0, 0, 0, None, None, 0, 0, None, None), max_columns=2)
    out = _call(tool, "tpch", "tiny", "orders")
    assert [c["column"] for c in out["columns"]] == ["a", "b"]
    assert f'FROM (SELECT "a", "b" FROM {ORDERS} LIMIT' in trino.calls[1]


def test_more_columns_than_the_limit_are_refused_before_any_aggregate():
    three = _described(("a", "bigint"), ("b", "bigint"), ("c", "bigint"))
    tool, trino = _profile(three, max_columns=2)
    out = _call(tool, "tpch", "tiny", "orders", columns=["a", "b", "c"])
    assert out["kind"] == "invalid_argument" and "2" in out["error"]
    assert trino.calls == [f"DESCRIBE {ORDERS}"]


def test_sample_mode_reads_the_first_rows_and_says_so():
    tool, trino = _profile(DESCRIBED, _aggregates(5, 5, 5, 1, 5, 5, 5, "a", "e"), sample_rows=5)
    out = _call(tool, "tpch", "tiny", "orders", mode="sample")
    assert trino.calls[1].endswith(f'FROM (SELECT "id", "name" FROM {ORDERS} LIMIT 5)')
    assert out["scope"] == "first_rows"


def test_bernoulli_mode_reads_a_random_sample_and_says_so():
    tool, trino = _profile(DESCRIBED, _aggregates(2, 2, 2, 1, 2, 2, 2, "a", "b"))
    out = _call(tool, "tpch", "tiny", "orders", mode="bernoulli", percent=12.5)
    assert trino.calls[1].endswith(f"FROM {ORDERS} TABLESAMPLE BERNOULLI (12.5)")
    assert out["scope"] == "random_sample"
    tool, trino = _profile(DESCRIBED, _aggregates(2, 2, 2, 1, 2, 2, 2, "a", "b"))
    _call(tool, "tpch", "tiny", "orders", mode="bernoulli", percent=100)
    assert trino.calls[1].endswith("TABLESAMPLE BERNOULLI (100.0)"), "the number, never the text"


@pytest.mark.parametrize(
    "percent",
    [0, 0.0, -1, 100.5, float("nan"), float("inf"), True, "10", "10) UNION SELECT 1 --"],
)
def test_bernoulli_percent_outside_0_100_is_refused(percent):
    tool, trino = _profile(DESCRIBED)
    out = _call(tool, "tpch", "tiny", "orders", mode="bernoulli", percent=percent)
    assert out["kind"] == "invalid_argument"
    assert trino.calls == [], "refused before Trino"


@pytest.mark.parametrize("mode", ["sample", "full"])
def test_a_percent_outside_bernoulli_mode_is_refused(mode):
    tool, trino = _profile(DESCRIBED)
    out = _call(tool, "tpch", "tiny", "orders", mode=mode, percent=10)
    assert out["kind"] == "invalid_argument" and trino.calls == []


def test_an_unknown_mode_is_refused():
    tool, trino = _profile(DESCRIBED)
    out = _call(tool, "tpch", "tiny", "orders", mode="stats")
    assert out["kind"] == "invalid_argument" and trino.calls == []


@pytest.mark.parametrize(
    "catalog, schema, table",
    [("system", "runtime", "queries"), ("tpch", "bad;name", "orders"), ("tpch", "tiny", "")],
)
def test_a_name_the_tools_refuse_reaches_no_query(catalog, schema, table):
    tool, trino = _profile(DESCRIBED)
    out = _call(tool, catalog, schema, table)
    assert out["kind"] == "invalid_argument" and trino.calls == []


def test_full_mode_reads_every_visible_row_and_says_so():
    tool, trino = _profile(DESCRIBED, _aggregates(2, 2, 2, 1, 2, 2, 2, "a", "b"))
    out = _call(tool, "tpch", "tiny", "orders", mode="full")
    assert trino.calls[1].endswith(f'max("name") FROM {ORDERS}')
    assert out["scope"] == "visible_rows"


@pytest.mark.parametrize(
    "kind",
    [
        "array(integer)",
        "map(varchar, integer)",
        "row(a integer)",
        "json",
        "HyperLogLog",
        "qdigest(bigint)",
        "geometry",
        "someday_type",
        "varchar(25) array",
    ],
)
def test_non_comparable_types_get_count_only_and_say_so(kind):
    tool, trino = _profile(_described(("x", kind)), _aggregates(4, 3))
    out = _call(tool, "tpch", "tiny", "orders")
    assert trino.calls[1] == (
        f'SELECT count(*), count("x") FROM (SELECT "x" FROM {ORDERS} LIMIT 100000)'
    )
    assert out["columns"] == [
        {
            "column": "x",
            "type": kind,
            "non_null": 3,
            "null_fraction": 0.25,
            "approx_distinct": None,
            "min": None,
            "max": None,
            "skipped": ["distinct", "min", "max"],
        }
    ]


@pytest.mark.parametrize(
    "kind",
    [
        "boolean",
        "tinyint",
        "smallint",
        "integer",
        "bigint",
        "real",
        "double",
        "decimal(12,2)",
        "varchar",
        "varchar(3)",
        "char(2)",
        "varbinary",
        "date",
        "time(3)",
        "time(6) with time zone",
        "timestamp(3)",
        "timestamp(6) with time zone",
        "interval year to month",
        "interval day to second",
        "uuid",
        "ipaddress",
    ],
)
def test_ordered_types_get_distinct_min_and_max(kind):
    tool, trino = _profile(_described(("x", kind)), _aggregates(1, 1, 1, None, None))
    out = _call(tool, "tpch", "tiny", "orders")
    assert 'approx_distinct("x"), min("x"), max("x")' in trino.calls[1]
    assert "skipped" not in out["columns"][0]


def test_null_fraction_of_an_empty_table_is_null():
    tool, _ = _profile(DESCRIBED, _aggregates(0, 0, 0, None, None, 0, 0, None, None))
    out = _call(tool, "tpch", "tiny", "orders")
    assert out["rows"] == 0
    assert [c["null_fraction"] for c in out["columns"]] == [None, None]


def test_min_and_max_are_rendered_like_the_cells_of_execute_query():
    columns = _described(("price", "decimal(12,2)"), ("day", "date"), ("raw", "varbinary"))
    row = _aggregates(
        1,
        1,
        1,
        decimal.Decimal("10.50"),
        decimal.Decimal("99.90"),
        1,
        1,
        dt.date(2026, 1, 2),
        dt.date(2026, 3, 4),
        1,
        1,
        b"\x00\x01",
        b"\xff",
    )
    tool, _ = _profile(columns, row)
    out = _call(tool, "tpch", "tiny", "orders")
    cells = [[c["min"], c["max"]] for c in out["columns"]]
    assert cells == [["10.50", "99.90"], ["2026-01-02", "2026-03-04"], ["0001", "ff"]]
    # The same rendering as a row of execute_query, cell for cell.
    raw = row["rows"][0]
    assert cells == json.loads(tools_module.dumps([raw[3:5], raw[7:9], raw[11:13]]))


def test_interval_columns_are_profiled_and_rendered_as_trino_prints_them():
    columns = _described(("wait", "interval day to second"), ("term", "interval year to month"))
    row = _aggregates(
        2,
        2,
        2,
        dt.timedelta(hours=3),
        dt.timedelta(days=2),
        2,
        1,
        YearToMonth(0, 2),
        YearToMonth(1, 0),
    )
    tool, trino = _profile(columns, row)
    out = _call(tool, "tpch", "tiny", "orders")
    assert 'approx_distinct("wait"), min("wait"), max("wait")' in trino.calls[1]
    assert [[c["min"], c["max"]] for c in out["columns"]] == [
        ["0 03:00:00.000", "2 00:00:00.000"],
        ["0-2", "1-0"],
    ]


def test_a_column_name_with_a_double_quote_is_quoted_by_doubling():
    tool, trino = _profile(_described(('we"ird', "json")), _aggregates(1, 1))
    _call(tool, "tpch", "tiny", "orders")
    assert trino.calls[1] == (
        f'SELECT count(*), count("we""ird") FROM (SELECT "we""ird" FROM {ORDERS} LIMIT 100000)'
    )


def test_the_aggregate_query_goes_through_the_read_only_guard(monkeypatch):
    seen = []

    def refuse(sql, *args, **kwargs):
        seen.append(sql)
        return False

    monkeypatch.setattr(tools_module, "is_read_only_sql", refuse)
    tool, trino = _profile(DESCRIBED)
    out = _call(tool, "tpch", "tiny", "orders")
    assert out["kind"] == "read_only"
    assert trino.calls == [f"DESCRIBE {ORDERS}"] and seen[0].startswith("SELECT count(*)")


def test_nothing_but_the_aggregates_comes_back():
    """What the client tells of the query beyond its rows never reaches the agent."""
    answer = _aggregates(1, 1, 1, 1, 1, 1, 1, "a", "a")
    answer.update(truncated=False, stats={"anything": 1}, columns=["c"] * 9)
    tool, _ = _profile(DESCRIBED, answer)
    out = _call(tool, "tpch", "tiny", "orders")
    assert set(out) == {"table", "scope", "rows", "columns_cut", "columns"}


def test_a_trino_error_is_returned_with_its_kind():
    tool, trino = _profile(PermissionError("Access Denied: Cannot select from columns"))
    out = _call(tool, "tpch", "tiny", "orders")
    assert out == {"error": "Access Denied: Cannot select from columns", "kind": "permission"}
    tool, trino = _profile(DESCRIBED, TimeoutError("Query exceeded the time budget"))
    out = _call(tool, "tpch", "tiny", "orders", mode="full")
    assert out["kind"] == "time_budget"


def test_a_full_profile_past_the_budget_says_time_budget(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(audit_time, "monotonic", lambda: now[0])

    def slow():
        now[0] += 8
        return _aggregates(1, 1, 1, 1, 1, 1, 1, "a", "a")

    tool, trino = _profile(DESCRIBED, slow, budget=7)
    out = _call(tool, "tpch", "tiny", "orders", mode="full")
    assert out["kind"] == "time_budget"
    assert "columns" not in out and "rows" not in out, "nothing it gathered is returned"


def test_the_audit_line_names_the_aggregate_the_table_and_one_row():
    audit = InMemoryAudit()
    tool, trino = _profile(DESCRIBED, _aggregates(1, 1, 1, 1, 1, 1, 1, "a", "a"), audit=audit)
    _call(tool, "tpch", "tiny", "orders")
    from akko_mcp_trino.audit import sql_digest

    call = audit.events[0].call
    assert audit.events[0].tool == "profile_table" and audit.events[0].ok
    assert (call.sql_sha256, call.tables, call.rows) == (sql_digest(trino.calls[1]), [ORDERS], 1)


def test_the_description_says_the_aggregates_run_as_the_caller():
    tool, _ = _profile()
    doc = tool.__doc__
    assert doc.startswith(
        "Profile a table from the rows you may read: an aggregate query runs as you, so "
        "masks and row filters apply as in execute_query."
    )
    assert "SHOW" not in doc.upper().replace("SHOWN", "")


def test_the_server_bounds_the_profile_by_its_settings(monkeypatch):
    sent = []

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def query(self, sql, user=None, params=None, **keywords):
            sent.append(sql)
            if sql.startswith("DESCRIBE"):
                return _described(("a", "bigint"), ("b", "bigint"))
            return _aggregates(1, 1, 1, 1, 1)

    monkeypatch.setattr("akko_mcp_trino.server.TrinoClient", Client)
    config = core_config(profile_max_columns=1, profile_sample_rows=7)
    mcp, _ = build_server(config, mcp_factory=lambda name, **_: FakeMCP())
    out = _call(mcp.tools["profile_table"], "tpch", "tiny", "orders")
    assert [c["column"] for c in out["columns"]] == ["a"]
    assert sent[1].endswith(f'FROM (SELECT "a" FROM {ORDERS} LIMIT 7)')


# ---- the settings: modes, value length, DESCRIBE rows, client tag, time budget


def test_a_mode_this_server_closed_is_refused_without_a_query():
    tool, trino = _profile(DESCRIBED, profile_modes=("sample", "bernoulli"))
    out = _call(tool, "tpch", "tiny", "orders", mode="full")
    assert out["kind"] == "invalid_argument" and trino.calls == []
    assert "sample, bernoulli" in out["error"], "the refusal names the modes allowed"


def test_the_default_mode_closed_is_refused_too():
    tool, trino = _profile(DESCRIBED, profile_modes=("full",))
    out = _call(tool, "tpch", "tiny", "orders")
    assert out["kind"] == "invalid_argument" and "full" in out["error"] and trino.calls == []


def test_the_description_names_only_the_modes_this_server_allows():
    tool, _ = _profile(profile_modes=("sample", "full"))
    assert '"sample"' in tool.__doc__ and '"full"' in tool.__doc__
    assert "bernoulli" not in tool.__doc__.lower()
    tool, _ = _profile()
    assert all(f'"{m}"' in tool.__doc__ for m in ("sample", "bernoulli", "full"))


def test_a_long_text_value_is_cut_and_says_so():
    columns = _described(("name", "varchar(40)"), ("raw", "varbinary"), ("code", "char(3)"))
    row = _aggregates(2, 2, 2, "abcdefgh", "abc", 2, 2, b"\x00\x01\x02", b"\xff", 2, 1, "x", "y")
    tool, _ = _profile(columns, row, profile_value_chars=5)
    by_name = {c["column"]: c for c in _call(tool, "tpch", "tiny", "orders")["columns"]}
    assert (by_name["name"]["min"], by_name["name"]["max"]) == ("abcde", "abc")
    assert by_name["name"]["cut"] == ["min"]
    assert (by_name["raw"]["min"], by_name["raw"]["max"]) == ("00010", "ff")
    assert by_name["raw"]["cut"] == ["min"]
    assert "cut" not in by_name["code"]
    row = _aggregates(1, 1, 1, "abcdefgh", "abcdefgh")
    tool, _ = _profile(_described(("name", "varchar")), row, profile_value_chars=2)
    assert _call(tool, "tpch", "tiny", "orders")["columns"][0]["cut"] == ["min", "max"]


def test_numbers_dates_and_booleans_are_never_cut():
    columns = _described(
        ("n", "bigint"), ("d", "date"), ("b", "boolean"), ("p", "decimal(12,2)"), ("u", "uuid")
    )
    row = _aggregates(
        1,
        1,
        1,
        123456789,
        987654321,
        1,
        1,
        dt.date(2026, 1, 2),
        dt.date(2026, 3, 4),
        1,
        1,
        False,
        True,
        1,
        1,
        decimal.Decimal("1234567.89"),
        decimal.Decimal("9876543.21"),
        1,
        1,
        "00000000-0000-0000-0000-000000000001",
        "00000000-0000-0000-0000-000000000002",
    )
    tool, _ = _profile(columns, row, profile_value_chars=1)
    out = _call(tool, "tpch", "tiny", "orders")
    assert [c["min"] for c in out["columns"]] == [
        123456789,
        "2026-01-02",
        False,
        "1234567.89",
        "00000000-0000-0000-0000-000000000001",
    ]
    assert not [c for c in out["columns"] if "cut" in c]


def test_describe_is_read_up_to_its_own_bound_and_says_when_it_was_cut():
    three = _described(("a", "bigint"), ("b", "bigint"), ("c", "bigint"))
    tool, trino = _profile(three, _aggregates(0, 0, 0, None, None), profile_describe_rows=2)
    out = _call(tool, "tpch", "tiny", "orders", columns=["a"])
    assert trino.keywords[0] == {"row_limit": 3}, "one row more than its bound, past the cap"
    assert out["columns_cut"] is True
    tool, trino = _profile(three, profile_describe_rows=2)
    out = _call(tool, "tpch", "tiny", "orders", columns=["c"])
    assert out["kind"] == "invalid_argument" and len(trino.calls) == 1
    tool, _ = _profile(three, _aggregates(0, 0, 0, None, None), profile_describe_rows=3)
    assert _call(tool, "tpch", "tiny", "orders", columns=["c"])["columns_cut"] is False


def test_the_description_names_the_setting_that_bounds_describe():
    tool, _ = _profile()
    assert "columns past MCP_PROFILE_DESCRIBE_ROWS for DESCRIBE cannot be named" in tool.__doc__
    assert "TRINO_MAX_ROWS" not in tool.__doc__


def test_the_aggregate_query_has_its_own_budget():
    tool, trino = _profile(DESCRIBED, _aggregates(1, 1, 1, 1, 1, 1, 1, "a", "a"))
    _call(tool, "tpch", "tiny", "orders")
    assert "timeout" not in trino.keywords[1], "0: the call's budget"
    tool, trino = _profile(
        DESCRIBED, _aggregates(1, 1, 1, 1, 1, 1, 1, "a", "a"), profile_timeout_seconds=4.5
    )
    _call(tool, "tpch", "tiny", "orders")
    assert trino.keywords[1]["timeout"] == 4.5 and "timeout" not in trino.keywords[0]


# ---- through the real Trino client: its row cap, its client tags, its cancel


def _through_the_client(monkeypatch, described, aggregate, **settings):
    """profile_table of a server built over the real TrinoClient, with the Trino driver
    replaced: it answers DESCRIBE with ``described`` (tuples) and the aggregate with the
    row ``aggregate``, or, for "slow", only once the query is cancelled. Returns the tool,
    the statements with the client tags each was sent with, and the cursors."""
    seen, cursors = [], []

    class Cursor:
        query_id = "q1"
        description = [("c",)]

        def __init__(self, tags):
            self.tags, self.rows, self.slow, self.cancelled = tags, [], False, 0
            self.stop = threading.Event()

        def execute(self, sql, params=None):
            seen.append((sql, self.tags))
            if sql.startswith("DESCRIBE"):
                self.rows = list(described)
            elif aggregate == "slow":
                self.slow = True
            else:
                self.rows = [tuple(aggregate)]

        def fetchmany(self, n):
            if self.slow:
                assert self.stop.wait(5), "the budget never cancelled the query"
                raise RuntimeError("Query was canceled")
            return self.rows[:n]

        def cancel(self):
            self.cancelled += 1
            self.stop.set()

    def connect(**kw):
        cursor = Cursor(kw.get("client_tags"))
        cursors.append(cursor)
        return type("Connection", (), {"cursor": lambda self: cursor})()

    monkeypatch.setattr(trino_client.trino.dbapi, "connect", connect)
    mcp, _ = build_server(core_config(**settings), mcp_factory=lambda name, **_: FakeMCP())
    return mcp.tools["profile_table"], seen, cursors


def test_a_table_wider_than_the_row_cap_is_profiled_to_its_last_column(monkeypatch):
    wide = [(f"c{i:03d}", "bigint", "", "") for i in range(150)]
    tool, seen, _ = _through_the_client(
        monkeypatch, wide, (5, 4, 4, 1, 9), max_rows=100, profile_max_columns=150
    )
    out = _call(tool, "tpch", "tiny", "orders", columns=["c149"])
    assert out["columns_cut"] is False and out["columns"][0]["column"] == "c149"
    assert seen[1][0].startswith('SELECT count(*), count("c149")')


def test_the_client_tag_is_on_the_aggregate_and_not_on_describe(monkeypatch):
    tool, seen, _ = _through_the_client(
        monkeypatch, [("id", "bigint", "", "")], (1, 1, 1, 1, 1), profile_client_tag="agents"
    )
    _call(tool, "tpch", "tiny", "orders")
    assert [tags for _, tags in seen] == [None, ["agents"]]
    with trino_client.query_tags("tool=profile_table"):
        _call(tool, "tpch", "tiny", "orders")
    assert [tags for _, tags in seen[2:]] == [
        ["tool=profile_table"],
        ["tool=profile_table", "agents"],
    ], "added to the tags an extension sets, never in their place"


def test_its_own_budget_past_says_time_budget_and_cancels_the_query(monkeypatch):
    tool, seen, cursors = _through_the_client(
        monkeypatch, [("id", "bigint", "", "")], "slow", profile_timeout_seconds=0.05
    )
    out = _call(tool, "tpch", "tiny", "orders", mode="full")
    assert out["kind"] == "time_budget" and "columns" not in out
    assert cursors[1].cancelled >= 1, "the query is cancelled in Trino"
