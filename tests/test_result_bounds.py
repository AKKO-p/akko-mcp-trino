"""What an agent is served is bounded: the JSON of an execute_query answer, each cell of it,
and the comments Trino holds on tables and columns, which are text the data's owners wrote."""

import datetime as dt
import decimal
import json
from dataclasses import replace

from akko_mcp_trino.__main__ import check_config
from akko_mcp_trino.audit import InMemoryAudit
from akko_mcp_trino.config import Config
from akko_mcp_trino.modules import fingerprint_audit
from akko_mcp_trino.tools import register_query_tools
from tests.test_search import Trino, clock  # noqa: F401 - the fixture
from tests.test_tools import FakeClient, _rows
from tests.tool_doubles import FakeMCP, in_request

SQL = "SELECT a FROM c.s.t"


def _query(result, **settings):
    """execute_query's answer over a client that answers ``result``, and its audit."""
    mcp, audit = FakeMCP(), InMemoryAudit()
    client = FakeClient(result=result)
    register_query_tools(mcp, client, audit=audit, **settings)
    out = in_request(lambda: mcp.tools["execute_query"](SQL))
    return out, json.loads(out), audit


def _result(columns, rows, truncated=False):
    return {"columns": columns, "rows": rows, "row_count": len(rows), "truncated": truncated}


# ---- cells


def test_one_huge_cell_is_cut_and_said():
    huge = "x" * 5_000_000
    result = _result(
        ["name", "blob", "doc", "n", "ids", "pair", "flag"],
        [
            [
                huge,
                bytes(range(256)) * 400,
                {"k": "v" * 9000},
                12,
                list(range(5000)),
                {"a": [1, 2, 3]},
                True,
            ]
        ],
    )
    text, out, audit = _query(result)
    assert len(text) <= 1_048_576
    row = out["rows"][0]
    assert row[0] == "x" * 4000
    assert row[1] == (bytes(range(256)) * 400).hex()[:4000]
    assert isinstance(row[2], str) and len(row[2]) == 4000 and row[2].startswith('{"k": "vvv')
    assert row[3] == 12 and row[5] == {"a": [1, 2, 3]} and row[6] is True
    assert isinstance(row[4], str) and len(row[4]) == 4000 and row[4].startswith("[0, 1, 2")
    assert out["cut_columns"] == ["name", "blob", "doc", "ids"]
    assert out["truncated"] is False and "truncated_reason" not in out
    assert out["row_count"] == 1
    assert audit.events[0].call.rows == 1 and audit.events[0].call.truncated is False


def test_the_cell_bound_is_the_one_given():
    out = _query(_result(["a"], [["abcdefgh", "xy"]]), max_cell_chars=5)[1]
    assert out["rows"] == [["abcde", "xy"]] and out["cut_columns"] == ["a"]


def test_a_number_a_date_and_a_decimal_are_bounded_like_the_rest():
    big = 10**200
    result = _result(
        ["i", "d", "t", "ok"],
        [[big, decimal.Decimal("1." + "3" * 80), dt.date(2024, 1, 2), None]],
    )
    out = _query(result, max_cell_chars=40)[1]
    assert out["rows"][0][0] == str(big)[:40] and out["rows"][0][2] == "2024-01-02"
    assert len(out["rows"][0][1]) == 40
    assert out["rows"][0][3] is None
    assert out["cut_columns"] == ["i", "d"]


def test_a_binary_cell_that_fits_is_served_as_before():
    out = _query(_result(["b"], [[b"\x01\xff"]]), max_cell_chars=4)[1]
    assert out["rows"] == [["01ff"]] and "cut_columns" not in out
    out = _query(_result(["b"], [[b"\x01\xff\x02"]]), max_cell_chars=5)[1]
    assert out["rows"] == [["01ff0"]] and out["cut_columns"] == ["b"]


def test_cut_columns_name_the_columns_of_the_rows_returned_once_each():
    rows = [["a" * 30, "ok"], ["b" * 30, "c" * 30], ["d" * 30, "e"]]
    out = _query(_result(["same", "same"], rows), max_cell_chars=10)[1]
    assert out["cut_columns"] == ["same"]
    out = _query(_result(["x", "y"], rows), max_cell_chars=10)[1]
    assert out["cut_columns"] == ["x", "y"]
    out = _query(_result(["x", "y"], [["a", "b"]]), max_cell_chars=10)[1]
    assert "cut_columns" not in out


def test_an_answer_that_was_not_cut_is_what_the_client_gave():
    result = {**_result(["a", "b"], [[1, "x"], [2, "y"]]), "note": "kept"}
    out = _query(result)[1]
    assert out == result


def test_cut_columns_leave_with_the_rows_that_do_not_fit():
    big, small = "z" * 200, "k"
    rows = [[small, small], [small, big]]
    unlimited = _query(_result(["a", "b"], rows), max_cell_chars=100)
    assert unlimited[1]["cut_columns"] == ["b"]
    only_first = len(json.dumps(_result(["a", "b"], rows[:1]))) + 120
    cut = _query(_result(["a", "b"], rows), max_cell_chars=100, max_result_bytes=only_first)[1]
    assert len(cut["rows"]) == 1 and "cut_columns" not in cut and cut["truncated_reason"] == "bytes"


# ---- bytes


def _wide(n, cell=1000):
    return _result(["id", "text"], [[i, f"{i:04d}" + "y" * cell] for i in range(n)])


def test_result_bytes_bound_stops_rows():
    result = _wide(50)
    text, out, audit = _query(result, max_result_bytes=10_000)
    assert len(text) <= 10_000
    assert out["truncated"] is True and out["truncated_reason"] == "bytes"
    kept = out["rows"]
    assert 0 < len(kept) < 50 and out["row_count"] == len(kept)
    assert kept == result["rows"][: len(kept)], "the rows come in order, none skipped"
    # as many as fit: the bound at the size of the answer returns the same rows,
    # one byte less returns one row fewer
    same = _query(result, max_result_bytes=len(text))[1]
    assert same["rows"] == kept
    fewer = _query(result, max_result_bytes=len(text) - 1)[1]
    assert len(fewer["rows"]) == len(kept) - 1
    call = audit.events[0].call
    assert (call.rows, call.truncated, call.truncated_reason) == (len(kept), True, "bytes")
    assert "SELECT" not in repr(call.truncated_reason)


def test_the_default_bound_is_a_mebibyte():
    text, out, _ = _query(_wide(400, 4000))
    assert len(text) <= 1_048_576
    assert out["truncated_reason"] == "bytes" and out["row_count"] < 400


def test_an_answer_that_fits_is_not_cut():
    out = _query(_wide(5), max_result_bytes=1_048_576)[1]
    assert out["row_count"] == 5 and out["truncated"] is False
    assert "truncated_reason" not in out


def test_a_first_row_past_the_bound_answers_no_row_and_says_why():
    columns = [f"c{i}" for i in range(40)]
    result = _result(columns, [["q" * 400 for _ in columns]] * 3)
    text, out, audit = _query(result, max_cell_chars=400, max_result_bytes=5000)
    assert len(text) <= 5000
    assert out["rows"] == [] and out["row_count"] == 0
    assert out["truncated"] is True and out["truncated_reason"] == "bytes"
    assert out["columns"] == columns
    assert "cut_columns" not in out
    assert (audit.events[0].call.rows, audit.events[0].call.truncated_reason) == (0, "bytes")


def test_columns_that_alone_pass_the_bound_are_refused():
    columns = [f"column_{i:05d}" for i in range(500)]
    text, out, audit = _query(_result(columns, [[1] * 500]), max_result_bytes=1000)
    assert len(text) < 1000 and out["kind"] == "invalid_argument"
    assert "columns" in out["error"]
    assert (audit.events[0].ok, audit.events[0].error) == (False, "result_columns_too_long")


def test_rows_leave_while_cut_columns_would_overflow_the_bound():
    """cut_columns is the last key written: the text is measured, and rows leave until
    it fits, whatever the estimate said."""
    columns = ["a" * 60, "b" * 60]
    rows = [["x" * 50, "k"] for _ in range(40)]
    result = _result(columns, rows)
    for bound in range(1500, 2200, 7):
        text = _query(result, max_cell_chars=30, max_result_bytes=bound)[0]
        assert len(text) <= bound
        out = json.loads(text)
        assert out["row_count"] == len(out["rows"])
        if out["rows"]:
            assert out["cut_columns"] == ["a" * 60]


# ---- why it was cut


def test_truncated_reason_rows_vs_bytes():
    result = _result(["a"], [["x"], ["y"]], truncated=True)
    out = _query(result)[1]
    assert (out["truncated"], out["truncated_reason"], out["row_count"]) == (True, "rows", 2)
    assert out["rows"] == [["x"], ["y"]]
    # the row cap and the size bound: the size, which is the one the agent can act on
    both = _query(_result(["a"], [["x" * 400]] * 30, truncated=True), max_result_bytes=1500)[1]
    assert both["truncated_reason"] == "bytes" and both["truncated"] is True
    # neither: the keys are absent
    whole = _query(_result(["a"], [["x"]]))[1]
    assert whole["truncated"] is False and "truncated_reason" not in whole


def test_a_row_capped_answer_is_audited_with_its_reason():
    audit = _query(_result(["a"], [["x"]], truncated=True))[2]
    assert (audit.events[0].call.truncated, audit.events[0].call.truncated_reason) == (True, "rows")
    audit = _query(_result(["a"], [["x"]]))[2]
    assert (audit.events[0].call.truncated, audit.events[0].call.truncated_reason) == (False, "")


def test_the_fingerprint_line_carries_the_reason_only_when_cut():
    line = fingerprint_audit.FingerprintEvent(
        request_id="r", tool="execute_query", subject="s", ok=True, truncated_reason="bytes"
    ).as_dict()
    assert line["truncated_reason"] == "bytes"
    plain = fingerprint_audit.FingerprintEvent(
        request_id="r", tool="execute_query", subject="s", ok=True
    ).as_dict()
    assert "truncated_reason" not in plain
    assert "truncated_reason" in fingerprint_audit.FINGERPRINT_FIELDS

    class Sink:
        events: list = []

        def record(self, event):
            self.events.append(event)

    sink = Sink()
    audit = fingerprint_audit.FingerprintAudit(sink)
    mcp, memory = FakeMCP(), InMemoryAudit()
    register_query_tools(mcp, FakeClient(result=_wide(40)), audit=memory, max_result_bytes=5000)
    in_request(lambda: mcp.tools["execute_query"](SQL))
    audit.record(memory.events[0])
    assert sink.events[0].as_dict()["truncated_reason"] == "bytes"
    audit.record(replace(memory.events[0], call=None))
    assert "truncated_reason" not in sink.events[1].as_dict()


def test_bound_result_is_the_whole_contract():
    from akko_mcp_trino.tools import bound_result

    text, done = bound_result(_wide(3), 4000, 1_048_576)
    assert json.loads(text)["row_count"] == 3 and done == {"rows": 3, "reason": None}
    assert bound_result(_result(["c" * 100], []), 10, 50) is None


# ---- comments

NASTY = "ok\x00\x07 text‮ rtl​ zero  line\ttab\nnew"
CLEAN = "ok text rtl zero line\ttab\nnew"


def _server_with(client, **settings):
    mcp = FakeMCP()
    register_query_tools(mcp, client, **settings)
    return mcp


def test_trino_comment_controls_removed_and_bounded_in_describe_table():
    long = "c" * 10_000
    described = {
        "columns": ["Column", "Type", "Extra", "Comment"],
        "rows": [
            ["a", "bigint", "", NASTY],
            ["b", "varchar", "", long],
            ["c", "date", "", None],
            ["d", "date", "", ""],
        ],
        "row_count": 4,
        "truncated": False,
    }
    mcp = _server_with(FakeClient(result=described))
    out = json.loads(in_request(lambda: mcp.tools["describe_table"]("demo", "crm", "t")))
    assert [r[3] for r in out["rows"]] == [CLEAN, "c" * 1000, None, ""]
    assert out["comments_cut"] == ["b"]
    assert out["rows"][0][:3] == ["a", "bigint", ""]
    # the bound is the one given, and a table with nothing cut says nothing
    mcp = _server_with(FakeClient(result=described), comment_chars=30)
    out = json.loads(in_request(lambda: mcp.tools["describe_table"]("demo", "crm", "t")))
    assert out["comments_cut"] == ["b"] and out["rows"][1][3] == "c" * 30
    short = {**described, "rows": described["rows"][:1], "row_count": 1}
    mcp = _server_with(FakeClient(result=short))
    out = json.loads(in_request(lambda: mcp.tools["describe_table"]("demo", "crm", "t")))
    assert "comments_cut" not in out


def test_a_row_without_a_comment_column_is_served_as_it_is():
    short = {"columns": ["Column", "Type"], "rows": [["id", "bigint"]], "row_count": 1}
    mcp = _server_with(FakeClient(result=short))
    out = json.loads(in_request(lambda: mcp.tools["describe_table"]("demo", "crm", "t")))
    assert out["rows"] == [["id", "bigint"]] and "comments_cut" not in out


def test_trino_comment_controls_removed_and_bounded_in_describe_schema():
    long = "t" * 10_000
    results = [
        _rows([("crm",)]),
        _rows([("customers", "BASE TABLE"), ("orders", "VIEW"), ("plain", "VIEW")]),
        _rows(
            [
                ("customers", "id", "bigint", NASTY, 1),
                ("customers", "email", "varchar", long, 2),
                ("orders", "total", "decimal", None, 1),
                ("plain", "x", "int", "fine", 1),
            ]
        ),
        _rows([("customers", NASTY), ("orders", long), ("plain", None)]),
    ]
    mcp = _server_with(FakeClient(results=results))
    out = json.loads(in_request(lambda: mcp.tools["describe_schema"]("demo", "crm")))
    customers, orders, plain = out["tables"]
    assert customers["comment"] == CLEAN and "cut" not in customers
    assert orders["comment"] == "t" * 1000 and orders["cut"] == ["comment"]
    assert plain["comment"] is None and "cut" not in plain
    assert customers["columns"] == [
        {"column": "id", "type": "bigint", "comment": CLEAN},
        {"column": "email", "type": "varchar", "comment": "t" * 1000, "cut": ["comment"]},
    ]
    assert orders["columns"] == [{"column": "total", "type": "decimal", "comment": None}]
    assert plain["columns"] == [{"column": "x", "type": "int", "comment": "fine"}]


def test_trino_comment_controls_removed_and_bounded_in_search_columns(clock):  # noqa: F811
    long = "s" * 10_000
    columns = {
        ("demo", "crm"): [
            ("customers", "email", "varchar", NASTY + " email"),
            ("customers", "notes", "varchar", "email " + long),
            ("customers", "id", "bigint", "email"),
        ]
    }
    tables = {("demo", "crm"): {"customers": "email " + NASTY, "journal": "email " + long}}
    trino = Trino({"demo": ["crm"]}, columns, tables=tables)
    clock["trino"] = trino
    mcp = _server_with(trino, call_timeout_seconds=60.0)
    out = json.loads(mcp.tools["search_columns"]("email"))
    by_column = {m["column"]: m for m in out["matches"]}
    assert by_column["email"]["comment"] == CLEAN + " email" and "cut" not in by_column["email"]
    assert by_column["notes"]["comment"] == ("email " + long)[:1000]
    assert by_column["notes"]["cut"] == ["comment"]
    assert by_column["id"]["comment"] == "email" and "cut" not in by_column["id"]
    by_table = {t["table"]: t for t in out["tables"]}
    assert by_table["customers"]["comment"] == "email " + CLEAN
    assert "cut" not in by_table["customers"]
    assert by_table["journal"]["cut"] == ["comment"]
    assert len(by_table["journal"]["comment"]) == 1000
    mcp = _server_with(trino, call_timeout_seconds=60.0, comment_chars=10)
    out = json.loads(mcp.tools["search_columns"]("email"))
    assert {len(m["comment"]) for m in out["matches"]} == {5, 10}


def test_a_comment_that_is_not_text_is_served_as_it_is():
    from akko_mcp_trino.context.text import clean_comment

    assert clean_comment(None, 5) == (None, False)
    assert clean_comment(7, 5) == (7, False)
    assert clean_comment("abcdefgh​", 5) == ("abcde", True)


# ---- settings


def test_the_new_settings_are_read_validated_and_printed(monkeypatch, capsys):
    for name in ("MCP_MAX_RESULT_BYTES", "MCP_MAX_CELL_CHARS", "MCP_COMMENT_CHARS"):
        monkeypatch.delenv(name, raising=False)
    c = Config.from_env()
    assert (c.max_result_bytes, c.max_cell_chars, c.comment_chars) == (1_048_576, 4000, 1000)
    assert not [p for p in c.problems() if "MCP_M" in p and "RESULT" in p]
    monkeypatch.setenv("MCP_MAX_RESULT_BYTES", "999")
    monkeypatch.setenv("MCP_MAX_CELL_CHARS", "0")
    monkeypatch.setenv("MCP_COMMENT_CHARS", "-1")
    found = [p for p in Config.from_env().problems() if "RESULT" in p or "CHARS" in p]
    assert found == [
        "MCP_MAX_RESULT_BYTES=999: an answer must be allowed 1000 bytes or more",
        "MCP_MAX_CELL_CHARS=0: a text must be allowed 1 character or more",
        "MCP_COMMENT_CHARS=-1: a text must be allowed 1 character or more",
    ]
    monkeypatch.setenv("MCP_MAX_RESULT_BYTES", "2000")
    monkeypatch.setenv("MCP_MAX_CELL_CHARS", "50")
    monkeypatch.setenv("MCP_COMMENT_CHARS", "70")
    check_config(Config.from_env())
    assert (
        "answers: up to 2000 bytes of JSON, a cell of 50 characters and a Trino comment of 70 "
        "at most, cut past them"
    ) in capsys.readouterr().out


# ---- the sample of describe_table and the plan of explain_query


def _described_with_sample(sample, **settings):
    described = {"columns": ["Column", "Type", "Extra", "Comment"], "rows": [["a", "int", "", ""]]}
    mcp = FakeMCP()
    client = FakeClient(results=[{**described, "row_count": 1}, sample])
    register_query_tools(mcp, client, **settings)
    return json.loads(in_request(lambda: mcp.tools["describe_table"]("c", "s", "t", sample_rows=3)))


def test_the_sample_of_describe_table_is_bounded_like_a_query():
    sample = _result(["a", "b"], [["x" * 500, "k"], ["y", "k"]])
    out = _described_with_sample(sample, max_cell_chars=50)["sample"]
    assert out["rows"] == [["x" * 50, "k"], ["y", "k"]] and out["cut_columns"] == ["a"]
    out = _described_with_sample(_result(["a"], [["x" * 300]] * 20), max_result_bytes=1000)[
        "sample"
    ]
    assert out["truncated"] is True and out["truncated_reason"] == "bytes"
    assert 0 < out["row_count"] < 20 and out["row_count"] == len(out["rows"])
    plain = _described_with_sample(_result(["a"], [["x"]]))["sample"]
    assert plain == _result(["a"], [["x"]])


def test_a_sample_whose_columns_pass_the_bound_has_no_row():
    columns = [f"column_{i:05d}" for i in range(200)]
    out = _described_with_sample(_result(columns, [[1] * 200]), max_result_bytes=1000)["sample"]
    assert out["rows"] == [] and out["row_count"] == 0 and out["truncated"] is True


def _plan(text, **settings):
    mcp = FakeMCP()
    client = FakeClient(result={"columns": ["Query Plan"], "rows": [[text]], "row_count": 1})
    register_query_tools(mcp, client, extra_reads=frozenset({"EXPLAIN"}), **settings)
    raw = in_request(lambda: mcp.tools["explain_query"](SQL))
    return raw, json.loads(raw)


def test_the_plan_of_explain_query_is_bounded_and_says_it_was_cut():
    raw, out = _plan("Fragment 0\n" * 5, max_result_bytes=2000)
    assert out == {"plan": "Fragment 0\n" * 5}
    plan = 'node "q"\n' * 2000
    raw, out = _plan(plan, max_result_bytes=2000)
    assert len(raw) <= 2000
    assert out["truncated"] is True and out["truncated_reason"] == "bytes"
    assert plan.startswith(out["plan"]) and 0 < len(out["plan"]) < len(plan)
