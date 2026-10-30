"""search_columns: a search by name or comment, schema by schema, within the call's time.

Trino decides what is visible; the search must never claim to be complete when it is not,
never let the agent's text act as SQL or as a wildcard, and never turn into an error of the
whole call when the budget runs out: it answers what it searched and where to resume.
"""

import base64
import json
import re

import pytest

import akko_mcp_trino.audit as audit_module
from akko_mcp_trino.audit import InMemoryAudit, bind_call, reset_call, time_allowed
from akko_mcp_trino.search import Cursor, decode_cursor, encode_cursor, like_contains, search
from akko_mcp_trino.tools import register_query_tools


class FakeMCP:
    """Captures the tools so a test calls them directly."""

    def __init__(self):
        self.tools = {}

    def tool(self, annotations=None, **kw):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn

        return deco


class _UserError(Exception):
    def __init__(self, name, message):
        super().__init__(message)
        self.error_name, self.error_type, self.message = name, "USER_ERROR", message


class _InternalError(Exception):
    def __init__(self, message):
        super().__init__(message)
        self.error_name, self.error_type, self.message = (
            "GENERIC_INTERNAL_ERROR",
            "INTERNAL_ERROR",
            message,
        )


_PATTERN = re.compile(r"LIKE '((?:[^']|'')*)' ESCAPE '!'")


def _matcher(sql):
    """What the statement's LIKE pattern matches, read back as Trino would: the text between
    the outer %, its quotes undoubled and each ``!x`` read as a literal ``x``."""
    pattern = _PATTERN.search(sql).group(1).replace("''", "'")
    assert pattern.startswith("%") and pattern.endswith("%")
    body, out, i = pattern[1:-1], [], 0
    while i < len(body):
        if body[i] == "!":
            out.append(body[i + 1])
            i += 2
        else:
            assert body[i] not in "%_", "an unescaped wildcard in the pattern"
            out.append(body[i])
            i += 1
    return "".join(out)


class Trino:
    """A fake client that answers each statement by what it reads, on a simulated clock.

    ``schemas`` maps a catalog to its schema names, ``columns`` maps (catalog, schema) to
    (table, column, type, comment) rows; ``fail`` maps a catalog or a (catalog, schema) to the
    exception its read raises; ``cost`` maps the same keys to the seconds a read takes (1 s by
    default). A read longer than the time it is allowed stops like the real client: a plain
    TimeoutError when its own budget stopped it (the ``timeout`` it was given, or
    ``query_timeout``, TRINO_QUERY_TIMEOUT_SECONDS, when shorter), the call's CallTimeout when
    the call did."""

    def __init__(self, schemas, columns=None, fail=None, cost=None, catalogs=None, query_timeout=0):
        self.schemas = schemas
        self.columns = columns or {}
        self.fail = fail or {}
        self.cost = cost or {}
        self.catalogs = list(schemas) + ["system"] if catalogs is None else catalogs
        self.query_timeout = query_timeout
        self.now = 0.0
        self.calls = []

    def _spend(self, key, timeout):
        seconds = self.cost.get(key, 1.0)
        call = audit_module.current_call()
        budget = timeout
        if self.query_timeout > 0:
            budget = min(self.query_timeout, timeout) if timeout > 0 else self.query_timeout
        allowed = time_allowed(budget)
        if allowed > 0 and seconds > allowed:
            self.now += allowed
            if allowed == budget:
                raise TimeoutError(f"the query exceeded its time budget of {timeout:g} s")
            raise call.timed_out()
        self.now += seconds

    def _fail(self, key):
        failure = self.fail.get(key)
        if failure == "deadline":  # the call's deadline reached while the read ran
            raise audit_module.current_call().timed_out()
        if failure is not None:
            raise failure

    def query(self, sql, user=None, params=None, **kw):
        self.calls.append((sql, kw))
        timeout = kw.get("timeout", 0.0)
        if sql == "SHOW CATALOGS":
            rows = [[c] for c in self.catalogs]
            limit = kw.get("row_limit", 100)
            return {"rows": rows[:limit], "row_count": len(rows), "truncated": len(rows) > limit}
        found = re.search(r"FROM (\S+)\.information_schema\.schemata", sql)
        if found:
            cat = found.group(1)
            self._spend(cat, timeout)
            self._fail(cat)
            first = re.search(r"schema_name >= '([^']*)'", sql).group(1)
            names = sorted(s for s in self.schemas.get(cat, []) if s >= first)
            assert "information_schema" not in names or "<> 'information_schema'" in sql
            names = [s for s in names if s != "information_schema"]
            return {"rows": [[s] for s in names], "row_count": len(names), "truncated": False}
        assert "FROM system.jdbc.columns" in sql
        cat = re.search(r"table_cat = '([^']*)'", sql).group(1)
        sch = re.search(r"table_schem = '([^']*)'", sql).group(1)
        self._spend((cat, sch), timeout)
        self._fail((cat, sch))
        text = _matcher(sql)
        rows = [
            list(r)
            for r in self.columns.get((cat, sch), [])
            if text in r[1].lower() or (r[3] is not None and text in r[3].lower())
        ]
        offset = int(re.search(r"OFFSET (\d+)", sql).group(1))
        rows = rows[offset:]
        cap = kw.get("max_rows") or 100
        return {"rows": rows[:cap], "row_count": min(cap, len(rows)), "truncated": len(rows) > cap}


@pytest.fixture
def clock(monkeypatch):
    """The audit's clock, moved by the fake client's reads."""
    state = {"trino": None}
    monkeypatch.setattr(audit_module.time, "monotonic", lambda: state["trino"].now)
    return state


def _server(trino, *, budget=60.0, max_rows=100, min_length=3, reserve=2.0, audit=None):
    mcp = FakeMCP()
    register_query_tools(
        mcp,
        trino,
        audit=audit,
        call_timeout_seconds=budget,
        max_rows=max_rows,
        search_min_length=min_length,
        search_reserve_seconds=reserve,
    )
    return mcp.tools["search_columns"]


def _ask(trino, clock, *args, **kw):
    clock["trino"] = trino
    server_kw = {
        k: kw.pop(k) for k in ("budget", "max_rows", "min_length", "reserve", "audit") if k in kw
    }
    return json.loads(_server(trino, **server_kw)(*args, **kw))


_DEMO = {
    "demo": ["crm", "information_schema", "sales"],
    "lake": ["raw"],
}
_COLUMNS = {
    ("demo", "crm"): [
        ("customers", "id", "bigint", "Customer key"),
        ("customers", "email", "varchar", "Contact address of the customer"),
    ],
    ("demo", "sales"): [("orders", "customer_email", "varchar", None)],
    ("lake", "raw"): [("events", "payload", "varchar", "Raw EMAIL events")],
}


# ---- the pattern: the agent's text is a value, never SQL and never a wildcard


def test_percent_underscore_and_bang_are_literal():
    assert like_contains("10%_a!") == "'%10!%!_a!!%'"


def test_quotes_and_backslashes_cannot_break_the_statement():
    assert like_contains("o'b\\c") == "'%o''b\\c%'"


def test_the_pattern_is_lower_case_so_the_match_ignores_case():
    assert like_contains("EMail") == "'%email%'"


def test_a_match_on_a_comment_is_found(clock):
    trino = Trino(_DEMO, _COLUMNS)
    out = _ask(trino, clock, "contact")
    assert out["matches"] == [
        {
            "catalog": "demo",
            "schema": "crm",
            "table": "customers",
            "column": "email",
            "type": "varchar",
            "comment": "Contact address of the customer",
        }
    ]


def test_every_visible_catalog_and_schema_is_searched_in_order(clock):
    trino = Trino(_DEMO, _COLUMNS)
    out = _ask(trino, clock, "email")
    assert [(m["catalog"], m["schema"], m["column"]) for m in out["matches"]] == [
        ("demo", "crm", "email"),
        ("demo", "sales", "customer_email"),
        ("lake", "raw", "payload"),
    ]
    assert out["schemas_searched"] == 3 and out["not_searched"] == []
    assert out["complete"] is True and out["next_cursor"] is None


def test_the_statement_reads_one_schema_with_the_escaped_pattern(clock):
    trino = Trino({"demo": ["crm"]}, _COLUMNS)
    _ask(trino, clock, "50%_off", catalog="demo")
    sql = trino.calls[-1][0]
    assert sql == (
        "SELECT table_name, column_name, type_name, remarks FROM system.jdbc.columns "
        "WHERE table_cat = 'demo' AND table_schem = 'crm' "
        "AND (lower(column_name) LIKE '%50!%!_off%' ESCAPE '!' "
        "OR lower(remarks) LIKE '%50!%!_off%' ESCAPE '!') "
        "ORDER BY table_name, ordinal_position OFFSET 0 LIMIT 101"
    )


def test_a_hostile_text_matches_only_itself(clock):
    columns = {("demo", "crm"): [("t", "a_b", "int", None), ("t", "axb", "int", "it's 100%")]}
    trino = Trino({"demo": ["crm"]}, columns)
    assert [m["column"] for m in _ask(trino, clock, "a_b")["matches"]] == ["a_b"]
    assert [m["column"] for m in _ask(trino, clock, "'s 100%")["matches"]] == ["axb"]


def test_catalog_and_schema_names_are_folded_as_trino_folds_them(clock):
    trino = Trino({"demo": ["crm"]}, _COLUMNS)
    out = _ask(trino, clock, "email", catalog="Demo", schema="CRM")
    assert [(m["catalog"], m["schema"]) for m in out["matches"]] == [("demo", "crm")]
    assert all("information_schema" not in sql for sql, _ in trino.calls)


def test_a_named_catalog_searches_only_its_schemas(clock):
    trino = Trino(_DEMO, _COLUMNS)
    out = _ask(trino, clock, "email", catalog="lake")
    assert [m["catalog"] for m in out["matches"]] == ["lake"]
    assert all(sql != "SHOW CATALOGS" for sql, _ in trino.calls)


# ---- refusals: nothing reaches Trino


def test_a_text_shorter_than_the_minimum_is_refused_with_the_length(clock):
    trino = Trino(_DEMO, _COLUMNS)
    out = _ask(trino, clock, "  ab  ", min_length=3)
    assert out["kind"] == "invalid_argument" and "3 characters" in out["error"]
    assert trino.calls == []


def test_a_schema_without_its_catalog_is_refused(clock):
    trino = Trino(_DEMO, _COLUMNS)
    out = _ask(trino, clock, "email", schema="crm")
    assert out["kind"] == "invalid_argument" and "catalog" in out["error"]
    assert trino.calls == []


def test_all_catalogs_without_a_call_budget_is_refused(clock):
    trino = Trino(_DEMO, _COLUMNS)
    out = _ask(trino, clock, "email", budget=0)
    assert out["kind"] == "invalid_argument" and "name a catalog" in out["error"]
    assert trino.calls == []
    named = _ask(trino, clock, "email", catalog="demo", budget=0)
    assert named["complete"] is True and len(named["matches"]) == 2


def test_the_system_catalog_is_refused_as_an_argument(clock):
    trino = Trino(_DEMO, _COLUMNS)
    out = _ask(trino, clock, "query", catalog="system")
    assert out["kind"] == "invalid_argument" and "system catalog" in out["error"]
    assert trino.calls == []


def test_the_system_catalog_is_never_searched_even_from_a_cursor(clock):
    trino = Trino({**_DEMO, "system": ["runtime"]}, _COLUMNS)
    out = _ask(trino, clock, "email")
    assert all("system" not in (m["catalog"]) for m in out["matches"])
    assert all("table_cat = 'system'" not in sql for sql, _ in trino.calls)
    assert all("system.information_schema" not in sql for sql, _ in trino.calls)
    forged = _forge(
        {"c": "system", "s": "runtime", "o": 0, "q": _query_of(trino, clock), "g": False}
    )
    calls = len(trino.calls)
    refused = _ask(trino, clock, "email", cursor=forged)
    assert refused["kind"] == "invalid_argument" and "Invalid cursor" in refused["error"]
    assert len(trino.calls) == calls


def _forge(data):
    return base64.urlsafe_b64encode(json.dumps(data).encode()).decode()


def _query_of(trino, clock):
    """The query digest a real cursor carries for the search of "email" in every catalog."""
    paused = Trino(trino.schemas, trino.columns, cost={("demo", "crm"): 100.0})
    out = _ask(paused, clock, "email", budget=10)
    clock["trino"] = trino
    return decode_cursor(out["next_cursor"]).query


@pytest.mark.parametrize(
    "token",
    [
        "not base64 at all!",
        _forge(["a", "list"]),
        _forge({"c": "demo"}),
        _forge({"c": "demo", "s": "crm", "o": -1, "q": "x", "g": False}),
        _forge({"c": "demo", "s": "crm", "o": True, "q": "x", "g": False}),
        _forge({"c": "demo", "s": "crm", "o": 1.5, "q": "x", "g": False}),
        _forge({"c": "demo", "s": "CRM", "o": 0, "q": "x", "g": False}),
        _forge({"c": "demo", "s": "c\u0000rm", "o": 0, "q": "x", "g": False}),
        _forge({"c": "demo", "s": "s" * 1025, "o": 0, "q": "x", "g": False}),
        _forge({"c": "demo", "s": "crm", "o": 2**63, "q": "x", "g": False}),
        _forge({"c": "de'mo", "s": "crm", "o": 0, "q": "x", "g": False}),
        _forge({"c": 7, "s": "crm", "o": 0, "q": "x", "g": False}),
        _forge({"c": "demo", "s": "crm", "o": 0, "q": 1, "g": False}),
        _forge({"c": "demo", "s": "crm", "o": 0, "q": "x", "g": "no"}),
        base64.urlsafe_b64encode(b"\xff\xfe").decode(),
        "é",
    ],
)
def test_a_forged_cursor_is_invalid_argument_and_nothing_reaches_trino(clock, token):
    with pytest.raises(ValueError, match="Invalid cursor"):
        decode_cursor(token)  # refused by its own checks, not only by its search's digest
    trino = Trino(_DEMO, _COLUMNS)
    out = _ask(trino, clock, "email", cursor=token)
    assert out == {
        "error": "Invalid cursor: pass back next_cursor unchanged",
        "kind": "invalid_argument",
    }
    assert trino.calls == []


def test_a_cursor_round_trips():
    c = Cursor("demo", "crm", 40, query="abc", gaps=True)
    assert decode_cursor(encode_cursor(c)) == c
    assert decode_cursor(encode_cursor(Cursor("demo", "", 0))) == Cursor("demo", "", 0)


def test_a_cursor_of_another_search_is_refused(clock):
    """A cursor is a position in one search: kept for another text, it would skip what that
    search never read and end with an answer that says complete."""
    trino = Trino(_DEMO, _COLUMNS, cost={("demo", "sales"): 100.0})
    first = _ask(trino, clock, "email", budget=10)
    calls = len(trino.calls)
    for kw in ({"text": "customer"}, {"text": "email", "catalog": "demo"}):
        text = kw.pop("text")
        out = _ask(trino, clock, text, cursor=first["next_cursor"], **kw)
        assert out["kind"] == "invalid_argument" and "Invalid cursor" in out["error"]
    assert len(trino.calls) == calls


def test_a_cursor_outside_the_named_catalog_or_schema_is_refused(clock):
    from akko_mcp_trino.search import query_digest

    trino = Trino(_DEMO, _COLUMNS)
    for catalog, schema, at in (("demo", "", "lake"), ("demo", "crm", "demo")):
        digest = query_digest("email", catalog, schema)
        forged = encode_cursor(Cursor(at, "sales" if schema else "", 0, query=digest))
        out = _ask(trino, clock, "email", catalog=catalog, schema=schema, cursor=forged)
        assert out["kind"] == "invalid_argument"
    assert trino.calls == []


# ---- the time budget: stop before it, say what was not searched, resume there


def test_out_of_time_says_what_was_not_searched_and_resumes_there(clock):
    # each read takes 1 s; 10 s of budget, 2 in reserve: SHOW CATALOGS is instant, then
    # demo's schemata (1 s), crm (1 s), sales (1 s), lake's schemata (1 s), raw (1 s)...
    trino = Trino(_DEMO, _COLUMNS, cost={("demo", "sales"): 6.0})
    out = _ask(trino, clock, "email", budget=10)
    # 1 + 1 + 6 = 8 s spent: 2 s left, all of them in reserve, so raw is not read
    assert [m["schema"] for m in out["matches"]] == ["crm", "sales"]
    assert out["schemas_searched"] == 2
    assert out["not_searched"] == [{"catalog": "lake", "schema": None, "reason": "time_budget"}]
    assert out["complete"] is False
    assert decode_cursor(out["next_cursor"]).catalog == "lake"
    again = _ask(trino, clock, "email", budget=10, cursor=out["next_cursor"])
    assert [m["schema"] for m in again["matches"]] == ["raw"]
    assert again["complete"] is True and again["next_cursor"] is None


def test_out_of_time_inside_a_catalog_names_the_schema_and_the_unopened_catalogs(clock):
    trino = Trino(
        {"a": ["s1", "s2"], "b": ["s"], "c": ["s"]},
        {("a", "s2"): [("t", "email", "varchar", None)]},
        cost={("a", "s1"): 7.0},
    )
    out = _ask(trino, clock, "email", budget=10)
    assert out["not_searched"] == [
        {"catalog": "a", "schema": "s2", "reason": "time_budget"},
        {"catalog": "b", "schema": None, "reason": "time_budget"},
        {"catalog": "c", "schema": None, "reason": "time_budget"},
    ]
    assert decode_cursor(out["next_cursor"]) == Cursor(
        "a", "s2", 0, query=decode_cursor(out["next_cursor"]).query
    )
    again = _ask(trino, clock, "email", budget=10, cursor=out["next_cursor"])
    assert [m["schema"] for m in again["matches"]] == ["s2"] and again["complete"] is True


def test_a_schema_stopped_by_the_calls_remaining_time_is_resumed_not_skipped(clock):
    trino = Trino({"demo": ["crm", "sales"]}, _COLUMNS, cost={("demo", "crm"): 50.0})
    out = _ask(trino, clock, "email", budget=10)
    assert out["matches"] == [] and out["schemas_searched"] == 0
    assert out["not_searched"] == [{"catalog": "demo", "schema": "crm", "reason": "time_budget"}]
    resume = decode_cursor(out["next_cursor"])
    assert (resume.catalog, resume.schema, resume.row_offset) == ("demo", "crm", 0)
    # the query was given what was left minus the reserve: 10 - 1 (schemata) - 2 = 7 s
    assert trino.calls[-1][1]["timeout"] == pytest.approx(7.0)
    trino.cost = {}
    again = _ask(trino, clock, "email", budget=10, cursor=out["next_cursor"])
    assert [m["schema"] for m in again["matches"]] == ["crm", "sales"]
    assert again["complete"] is True


def test_the_search_ends_before_the_call_budget_so_the_call_never_times_out(clock):
    many = {"big": [f"s{i:04d}" for i in range(500)]}
    trino = Trino(many, {}, cost={("big", f"s{i:04d}"): 0.7 for i in range(500)})
    audit = InMemoryAudit()
    out = _ask(trino, clock, "email", budget=5, reserve=1.5, audit=audit)
    assert "error" not in out and out["complete"] is False and out["next_cursor"]
    assert trino.now < 5
    assert audit.events[-1].ok is True and audit.events[-1].call.expired is False


@pytest.mark.parametrize("where", ["demo", ("demo", "crm")])
def test_a_read_stopped_by_the_call_deadline_answers_the_calls_error_never_a_part(clock, where):
    """Only a reserve set to nothing lets a read reach the call's deadline (``--check``
    shows the reserve): the call then answers its own error, never what it gathered."""
    trino = Trino(_DEMO, _COLUMNS, fail={where: "deadline"})
    out = _ask(trino, clock, "email", budget=10)
    assert out["kind"] == "time_budget" and "nothing it gathered" in out["error"]
    assert "matches" not in out


def test_out_of_time_before_opening_a_catalog_resumes_at_its_first_schema(clock):
    trino = Trino(
        {"a": ["s"], "b": ["s1", "s2"]},
        {("b", "s1"): [("t", "email", "int", None)]},
        cost={("a", "s"): 7.0},
    )
    out = _ask(trino, clock, "email", budget=10)
    assert out["not_searched"] == [{"catalog": "b", "schema": None, "reason": "time_budget"}]
    resume = decode_cursor(out["next_cursor"])
    assert (resume.catalog, resume.schema, resume.row_offset) == ("b", "", 0)
    again = _ask(trino, clock, "email", budget=10, cursor=out["next_cursor"])
    assert [m["schema"] for m in again["matches"]] == ["s1"] and again["schemas_searched"] == 2


def test_a_catalog_whose_schemata_read_times_out_is_resumed(clock):
    trino = Trino({"a": ["s"]}, {("a", "s"): [("t", "email", "int", None)]}, cost={"a": 50.0})
    out = _ask(trino, clock, "email", budget=10)
    assert out["not_searched"] == [{"catalog": "a", "schema": None, "reason": "time_budget"}]
    trino.cost = {}
    again = _ask(trino, clock, "email", budget=10, cursor=out["next_cursor"])
    assert again["complete"] is True and len(again["matches"]) == 1


def test_without_a_call_budget_a_named_catalog_is_bounded_by_the_query_timeout(clock):
    trino = Trino({"demo": ["crm"]}, _COLUMNS)

    def stopped(sql, user=None, params=None, **kw):
        trino.calls.append((sql, kw))
        if "system.jdbc.columns" in sql:
            raise TimeoutError("the query exceeded its time budget of 60 s and was cancelled")
        return {"rows": [["crm"]], "row_count": 1, "truncated": False}

    trino.query = stopped
    out = _ask(trino, clock, "email", catalog="demo", budget=0)
    assert out["not_searched"] == [{"catalog": "demo", "schema": "crm", "reason": "time_budget"}]
    assert out["next_cursor"] is None and out["complete"] is False
    assert "timeout" not in trino.calls[-1][1], "no call budget: the query keeps its own"


def test_a_schema_slower_than_the_query_budget_is_skipped_and_named(clock):
    """A schema stopped by TRINO_QUERY_TIMEOUT_SECONDS while the call has time left would
    stop every resumed call at the same place: it is named and skipped, never resumed."""
    trino = Trino(_DEMO, _COLUMNS, cost={("demo", "crm"): 50.0}, query_timeout=3)
    out = _ask(trino, clock, "email", budget=60)
    assert out["not_searched"] == [{"catalog": "demo", "schema": "crm", "reason": "time_budget"}]
    assert [m["schema"] for m in out["matches"]] == ["sales", "raw"]
    assert out["schemas_searched"] == 2
    assert out["next_cursor"] is None and out["complete"] is False


def test_a_catalog_whose_schemas_are_slower_than_the_query_budget_is_skipped(clock):
    trino = Trino(_DEMO, _COLUMNS, cost={"demo": 50.0}, query_timeout=3)
    out = _ask(trino, clock, "email", budget=60)
    assert out["not_searched"] == [{"catalog": "demo", "schema": None, "reason": "time_budget"}]
    assert [m["catalog"] for m in out["matches"]] == ["lake"]
    assert out["next_cursor"] is None and out["complete"] is False


def test_a_skip_in_one_call_keeps_the_rest_of_the_chain_incomplete(clock):
    trino = Trino(
        _DEMO, _COLUMNS, cost={("demo", "crm"): 4.0, ("demo", "sales"): 3.0}, query_timeout=3.5
    )
    # crm is skipped by the query budget (3.5 s), sales takes 3 s, then lake is out of time
    out = _ask(trino, clock, "email", budget=10)
    assert out["not_searched"][0] == {"catalog": "demo", "schema": "crm", "reason": "time_budget"}
    assert out["next_cursor"] is not None
    again = _ask(trino, clock, "email", budget=10, cursor=out["next_cursor"])
    assert again["not_searched"] == [] and again["complete"] is False


# ---- the row cap: stop inside a schema, the cursor moves past what was read


def test_the_row_cap_stops_inside_a_schema_and_the_cursor_moves_past_what_was_read(clock):
    wide = {("demo", "crm"): [("t", f"email_{i}", "varchar", None) for i in range(7)]}
    trino = Trino({"demo": ["crm", "sales"], "lake": ["raw"]}, {**_COLUMNS, **wide})
    out = _ask(trino, clock, "email_", max_rows=3)
    assert [m["column"] for m in out["matches"]] == ["email_0", "email_1", "email_2"]
    assert out["not_searched"] == [
        {"catalog": "demo", "schema": "crm", "reason": "row_cap"},
        {"catalog": "lake", "schema": None, "reason": "row_cap"},
    ]
    assert trino.calls[-1][1]["max_rows"] == 3
    resume = decode_cursor(out["next_cursor"])
    assert (resume.catalog, resume.schema, resume.row_offset) == ("demo", "crm", 3)
    second = _ask(trino, clock, "email_", max_rows=3, cursor=out["next_cursor"])
    assert [m["column"] for m in second["matches"]] == ["email_3", "email_4", "email_5"]
    assert "OFFSET 3 LIMIT 4" in trino.calls[-1][0]
    third = _ask(trino, clock, "email_", max_rows=3, cursor=second["next_cursor"])
    assert [m["column"] for m in third["matches"]] == ["email_6"]
    assert third["complete"] is True


def test_a_full_page_stops_before_the_next_schema_rather_than_claim_it(clock):
    columns = {
        ("demo", "crm"): [("t", "email", "int", None)],
        ("demo", "sales"): [("t", "email", "int", None)],
    }
    trino = Trino({"demo": ["crm", "sales"]}, columns)
    out = _ask(trino, clock, "email", max_rows=1)
    assert len(out["matches"]) == 1 and out["complete"] is False
    assert out["not_searched"] == [{"catalog": "demo", "schema": "sales", "reason": "row_cap"}]
    assert decode_cursor(out["next_cursor"]).row_offset == 0
    full_at_a_catalog = Trino({"a": ["s"], "b": ["s"]}, {("a", "s"): [("t", "email", "int", None)]})
    out = _ask(full_at_a_catalog, clock, "email", max_rows=1)
    assert out["not_searched"] == [{"catalog": "b", "schema": None, "reason": "row_cap"}]


def test_a_cursor_whose_schema_is_gone_starts_the_next_one_from_its_first_row(clock):
    trino = Trino({"demo": ["crm", "sales"]}, _COLUMNS)
    from akko_mcp_trino.search import query_digest

    gone = encode_cursor(Cursor("demo", "crm_old", 5, query=query_digest("email", "", "")))
    out = _ask(trino, clock, "email", cursor=gone)
    assert [m["schema"] for m in out["matches"]] == ["sales"]
    assert "OFFSET 0 " in trino.calls[-1][0]


def test_a_search_stopped_in_a_schema_that_is_not_an_identifier_resumes(clock):
    """Hive accepts a schema named 2024_sales: the cursor the server gave must resume it."""
    columns = {("demo", "2024_sales"): [("t", "email", "varchar", None)]}
    trino = Trino({"demo": ["2024_sales", "crm"]}, {**_COLUMNS, **columns})
    trino.cost = {("demo", "2024_sales"): 50.0}
    out = _ask(trino, clock, "email", budget=10)
    assert out["not_searched"] == [
        {"catalog": "demo", "schema": "2024_sales", "reason": "time_budget"}
    ]
    trino.cost = {}
    again = _ask(trino, clock, "email", budget=10, cursor=out["next_cursor"])
    assert [m["schema"] for m in again["matches"]] == ["2024_sales", "crm"]
    assert again["complete"] is True


def test_a_schema_in_a_cursor_only_ever_enters_a_literal():
    token = _forge({"c": "demo", "s": "it's", "o": 0, "q": "x", "g": False})
    assert decode_cursor(token).schema == "it's"


@pytest.mark.parametrize("catalog", ["data", "hidden"])
def test_a_cursor_naming_a_catalog_the_caller_does_not_see_is_refused(clock, catalog):
    from akko_mcp_trino.search import query_digest

    trino = Trino({"demo": ["crm", "sales"]}, _COLUMNS)
    token = encode_cursor(Cursor(catalog, "x", 5, query=query_digest("email", "", "")))
    out = _ask(trino, clock, "email", cursor=token)
    assert out == {
        "error": "Invalid cursor: pass back next_cursor unchanged",
        "kind": "invalid_argument",
    }
    assert [sql for sql, _ in trino.calls] == ["SHOW CATALOGS"], "only the catalog listing"


# ---- failures: one schema does not stop the others, and nothing is complete with a gap


def test_one_failing_schema_does_not_stop_the_others_and_complete_is_false(clock):
    trino = Trino(
        _DEMO, _COLUMNS, fail={("demo", "crm"): _InternalError("Hive metastore unreachable")}
    )
    out = _ask(trino, clock, "email")
    assert [m["schema"] for m in out["matches"]] == ["sales", "raw"]
    assert out["not_searched"] == [{"catalog": "demo", "schema": "crm", "reason": "unavailable"}]
    assert out["schemas_searched"] == 2 and out["complete"] is False and out["next_cursor"] is None


def test_a_catalog_whose_schemas_cannot_be_listed_is_named_with_a_null_schema(clock):
    denied = _UserError("PERMISSION_DENIED", "Access Denied: Cannot access catalog demo")
    trino = Trino(_DEMO, _COLUMNS, fail={"demo": denied})
    out = _ask(trino, clock, "email")
    assert out["not_searched"] == [{"catalog": "demo", "schema": None, "reason": "permission"}]
    assert [m["catalog"] for m in out["matches"]] == ["lake"] and out["complete"] is False


def test_a_catalog_name_that_is_not_an_identifier_is_named_never_queried(clock):
    trino = Trino({"lake": ["raw"]}, _COLUMNS, catalogs=["lake", "we'ird"])
    out = _ask(trino, clock, "email")
    assert out["not_searched"] == [
        {"catalog": "we'ird", "schema": None, "reason": "invalid_argument"}
    ]
    assert all("we'ird" not in sql for sql, _ in trino.calls)


def test_a_refused_metadata_view_answers_the_error_that_names_what_to_allow(clock):
    denied = _UserError(
        "PERMISSION_DENIED",
        "Access Denied: Cannot select from columns [remarks] in table or view system.jdbc.columns",
    )
    trino = Trino({"demo": ["crm", "sales"]}, _COLUMNS, fail={("demo", "crm"): denied})
    audit = InMemoryAudit()
    out = _ask(trino, clock, "email", audit=audit)
    assert out["kind"] == "permission" and "system.jdbc.columns" in out["error"]
    assert "the access control must allow reading them" in out["error"]
    assert "matches" not in out and audit.events[-1].ok is False
    assert audit.events[-1].error == "USER_ERROR:PERMISSION_DENIED"


def test_a_gap_left_by_an_earlier_call_keeps_the_end_of_the_search_incomplete(clock):
    trino = Trino(
        {"demo": ["crm", "sales"], "lake": ["raw"]},
        _COLUMNS,
        fail={("demo", "crm"): _InternalError("down")},
        cost={("demo", "sales"): 6.0},
    )
    out = _ask(trino, clock, "email", budget=10)
    assert out["not_searched"][0] == {"catalog": "demo", "schema": "crm", "reason": "unavailable"}
    again = _ask(trino, clock, "email", budget=10, cursor=out["next_cursor"])
    assert again["next_cursor"] is None and again["not_searched"] == []
    assert again["complete"] is False, "crm was never searched: the search is not complete"


def test_complete_is_true_only_when_everything_was_searched(clock):
    trino = Trino(_DEMO, _COLUMNS)
    assert _ask(trino, clock, "email")["complete"] is True
    assert _ask(trino, clock, "nothing-like-this")["complete"] is True
    cut = _ask(trino, clock, "email", max_rows=1)
    failed = _ask(
        Trino(_DEMO, _COLUMNS, fail={("lake", "raw"): _InternalError("x")}), clock, "email"
    )
    late = _ask(Trino(_DEMO, _COLUMNS, cost={("demo", "crm"): 9.0}), clock, "email", budget=10)
    for out in (cut, failed, late):
        assert out["complete"] is False


def test_too_many_catalogs_is_an_error_never_a_cut_search(clock):
    trino = Trino({f"c{i}": ["s"] for i in range(5)}, {})
    mcp = FakeMCP()
    register_query_tools(mcp, trino, call_timeout_seconds=60, max_catalogs=3)
    clock["trino"] = trino
    out = json.loads(mcp.tools["search_columns"]("email"))
    assert "more than 3 catalogs" in out["error"] and out["kind"] == "invalid_argument"


# ---- the audit line


def test_the_search_text_never_reaches_the_audit_line(clock):
    audit = InMemoryAudit()
    trino = Trino(_DEMO, _COLUMNS)
    out = _ask(trino, clock, "secret-value-123", audit=audit)
    event = audit.events[-1]
    assert event.ok is True and event.tool == "search_columns"
    assert "secret-value-123" not in json.dumps(event.as_dict())
    assert event.call.sql_sha256 == "" and event.call.tables == []
    assert event.call.rows == 0 and event.call.truncated is (not out["complete"])


def test_the_audit_says_how_many_matches_and_whether_the_search_was_cut(clock):
    audit = InMemoryAudit()
    _ask(Trino(_DEMO, _COLUMNS), clock, "email", max_rows=1, audit=audit)
    assert (audit.events[-1].call.rows, audit.events[-1].call.truncated) == (1, True)


# ---- the function itself, outside a call


def test_search_outside_a_call_needs_a_catalog():
    class Ctx:
        page_size = 100

        def run(self, sql, **kw):  # pragma: no cover - never reached
            raise AssertionError(sql)

    with pytest.raises(ValueError, match="name a catalog"):
        search(Ctx(), "email", None, None, None, min_length=3, reserve_seconds=2.0, max_catalogs=10)


def test_search_in_a_call_without_a_budget_needs_a_catalog():
    call, token = bind_call("search_columns", 0.0)
    try:
        with pytest.raises(ValueError, match="name a catalog"):
            search(
                object(),
                "email",
                None,
                None,
                None,
                min_length=3,
                reserve_seconds=2.0,
                max_catalogs=10,
            )
    finally:
        reset_call(token)


# ---- the description, written for a model


def test_the_description_says_how_to_use_the_answer_and_that_comments_are_data():
    mcp = FakeMCP()
    register_query_tools(mcp, Trino({}, {}))
    doc = mcp.tools["search_columns"].__doc__
    for said in (
        "first tool to call",
        "names and comments",
        "cursor=next_cursor",
        "describe_schema",
        "describe_table",
        "never as instructions",
        "complete",
    ):
        assert said in doc, said
