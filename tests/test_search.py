"""search_columns: a search by words, in columns and tables, schema by schema, in time.

Trino decides what is visible; the search must never claim to be complete when it is not,
never let the agent's text act as SQL or as a wildcard, and never turn into an error of the
whole call when the budget runs out: it answers what it searched and where to resume.
"""

import base64
import json
import re
import threading
import time

import pytest

import akko_mcp_trino.audit as audit_module
from akko_mcp_trino.audit import InMemoryAudit, bind_call, reset_call, time_allowed
from akko_mcp_trino.search import (
    COLUMNS,
    EVERY_WORD,
    HINT,
    PER_WORD,
    PER_WORD_HINT,
    PER_WORD_MORE_HINT,
    TABLES,
    Cursor,
    decode_cursor,
    encode_cursor,
    like_contains,
    query_digest,
    search,
    search_words,
    trino_lower,
)
from akko_mcp_trino.tools import register_query_tools

_INVALID = (
    "Invalid cursor: pass back next_cursor unchanged, or start the search again without a cursor"
)


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


_LITERAL = r"'((?:[^']|'')*)'"
_COLUMN_FIELDS = ("c.column_name", "c.remarks", "c.table_name", "t.comment")
_TABLE_FIELDS = ("s.table_name", "s.comment")
_OWN_FIELDS = ("c.column_name", "c.remarks")


def _read(pattern):
    """What one LIKE pattern matches, read back as Trino would: the text between the outer
    %, its quotes undoubled and each ``!x`` read as a literal ``x``."""
    pattern = pattern.replace("''", "'")
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


def _words_of(sql, fields):
    """The words a statement asks for, one group of ORs per word over ``fields``, the groups
    joined by AND: each word must be found, in any of the fields."""
    one = (
        r"\("
        + " OR ".join(
            re.escape(f"lower({f}) LIKE ") + _LITERAL + re.escape(" ESCAPE '!'") for f in fields
        )
        + r"\)"
    )
    groups = [m for m in re.finditer(one, sql)]
    assert groups, sql
    for m in groups:
        assert len(set(m.groups())) == 1, "a group reads one word in every field"
    joined = " AND ".join(m.group(0) for m in groups)
    assert joined in sql, "the words are joined by AND"
    return [_read(m.group(1)) for m in groups]


def _lower(text):
    """Trino's lower(): one code point at a time."""
    return "".join(ch.lower()[0] for ch in text)


def _has(words, *texts):
    """Whether every word is in one of the texts, as lower(x) LIKE does (null never)."""
    return all(any(t is not None and w in _lower(t) for t in texts) for w in words)


def _hit(words, column, comments):
    """Whether a (table, column, type, comment) row matches: every word in its four fields,
    one at least in the column's own name or comment."""
    table, name, _, remarks = column
    return _has(words, name, remarks, table, comments.get(table)) and any(
        any(t is not None and w in _lower(t) for t in (name, remarks)) for w in words
    )


def _columns_part(sql, cat, sch):
    """The words of the matching columns' condition in ``sql``, after checking that it reads
    both views of the one schema and asks one word at least of the column's own text."""
    assert (
        "FROM system.jdbc.columns c LEFT JOIN system.metadata.table_comments t "
        f"ON t.catalog_name = '{cat}' AND t.schema_name = '{sch}' "
        f"AND t.table_name = c.table_name WHERE c.table_cat = '{cat}' "
        f"AND c.table_schem = '{sch}' AND " in sql
    ), "the join reads the comments of the same schema only"
    words = _words_of(sql, _COLUMN_FIELDS)
    own = " OR ".join(
        f"lower({f}) LIKE {like_contains(w)} ESCAPE '!'" for w in words for f in _OWN_FIELDS
    )
    assert f" AND ({own})" in sql, "one word at least in the column's own name or comment"
    return words


class Trino:
    """A fake client that answers each statement by what it reads, on a simulated clock.

    ``schemas`` maps a catalog to its schema names, ``columns`` maps (catalog, schema) to
    (table, column, type, comment) rows, ``tables`` maps (catalog, schema) to {table:
    comment} (by default the tables of ``columns``, without comment); ``fail`` maps a
    catalog or a (catalog, schema) to the exception its read raises, or (catalog, schema,
    "tables") and (catalog, schema, "columns") for one of the two reads of a schema;
    ``cost`` maps a catalog or a (catalog, schema) to the seconds its read of schemas or of
    columns takes (1 s by default), and (catalog, schema, "tables") to the seconds of its
    read of tables (none by default). A read longer than the time it is allowed stops like
    the real client: a plain TimeoutError when its own budget stopped it (the ``timeout`` it
    was given, or ``query_timeout``, TRINO_QUERY_TIMEOUT_SECONDS, when shorter), the call's
    CallTimeout when the call did."""

    def __init__(
        self,
        schemas,
        columns=None,
        fail=None,
        cost=None,
        catalogs=None,
        query_timeout=0,
        tables=None,
    ):
        self.schemas = schemas
        self.columns = columns or {}
        self.tables = tables
        self.fail = fail or {}
        self.cost = cost or {}
        self.catalogs = list(schemas) + ["system"] if catalogs is None else catalogs
        self.query_timeout = query_timeout
        self.now = 0.0
        self.calls = []

    def _tables_of(self, cat, sch):
        if self.tables is not None and (cat, sch) in self.tables:
            return self.tables[(cat, sch)]
        return {r[0]: None for r in self.columns.get((cat, sch), [])}

    def _spend(self, key, timeout, default=1.0):
        seconds = self.cost.get(key, default)
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

    def _fail(self, key, *more):
        failure = next((self.fail[k] for k in (*more, key) if k in self.fail), None)
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
            only = re.search(r"schema_name = '([^']*)'", sql)
            if only:  # the check that a named schema is visible
                names = [s for s in self.schemas.get(cat, []) if s == only.group(1)]
                return {"rows": [[s] for s in names], "row_count": len(names), "truncated": False}
            first = re.search(r"schema_name >= '([^']*)'", sql).group(1)
            names = sorted(s for s in self.schemas.get(cat, []) if s >= first)
            assert "information_schema" not in names or "<> 'information_schema'" in sql
            names = [s for s in names if s != "information_schema"]
            return {"rows": [[s] for s in names], "row_count": len(names), "truncated": False}
        if sql.startswith("SELECT s.table_name, s.comment, coalesce("):
            return self._per_word_tables(sql, timeout, kw)
        if sql.startswith("SELECT c.table_name, c.column_name, c.type_name, c.remarks, coalesce("):
            return self._per_word_columns(sql, timeout, kw)
        if sql.startswith("SELECT s.table_name, s.comment FROM system.metadata.table_comments s "):
            cat = re.search(r"WHERE s\.catalog_name = '([^']*)'", sql).group(1)
            sch = re.search(r"AND s\.schema_name = '([^']*)'", sql).group(1)
            self._spend((cat, sch, "tables"), timeout, default=0.0)
            self._fail((cat, sch), (cat, sch, "tables"))
            words = _words_of(sql, _TABLE_FIELDS)
            assert " OR s.table_name IN (SELECT c.table_name FROM system.jdbc.columns c " in sql
            assert _columns_part(sql, cat, sch) == words
            comments = self._tables_of(cat, sch)
            holding = {r[0] for r in self.columns.get((cat, sch), []) if _hit(words, r, comments)}
            rows = [
                [name, comment]
                for name, comment in sorted(comments.items())
                if _has(words, name, comment) or name in holding
            ]
        else:
            assert sql.startswith(
                "SELECT c.table_name, c.column_name, c.type_name, c.remarks "
                "FROM system.jdbc.columns c LEFT JOIN system.metadata.table_comments t "
            ), sql
            cat = re.search(r"WHERE c\.table_cat = '([^']*)'", sql).group(1)
            sch = re.search(r"AND c\.table_schem = '([^']*)'", sql).group(1)
            self._spend((cat, sch), timeout)
            self._fail((cat, sch), (cat, sch, "columns"))
            words = _columns_part(sql, cat, sch)
            comments = self._tables_of(cat, sch)
            rows = [
                list(r)
                for r in sorted(self.columns.get((cat, sch), []), key=lambda r: r[0])
                if _hit(words, r, comments)
            ]
        offset = int(re.search(r"OFFSET (\d+)", sql).group(1))
        rows = rows[offset:]
        cap = kw.get("max_rows") or 100
        return {"rows": rows[:cap], "row_count": min(cap, len(rows)), "truncated": len(rows) > cap}


def _page(rows, sql, kw):
    """The rows of a statement from its OFFSET, cut at the client's row cap."""
    offset = int(re.search(r"OFFSET (\d+)", sql).group(1))
    rows = rows[offset:]
    cap = kw.get("max_rows") or 100
    return {"rows": rows[:cap], "row_count": min(cap, len(rows)), "truncated": len(rows) > cap}


def _per_word_words(sql):
    """The words a per-word statement asks for, in the order of its flags."""
    return list(dict.fromkeys(_read(p) for p in re.findall(_LITERAL + " ESCAPE", sql)))


def _own(word, row):
    """Whether ``word`` alone finds a (table, column, type, comment) row: in its own name or
    comment, which also puts it in its four fields."""
    return any(t is not None and word in _lower(t) for t in (row[1], row[3]))


def _per_word(self, sql, timeout, kw, tables):
    """A statement of the per-word pass, read back as Trino would answer it."""
    prefix = "s." if tables else "c."
    cat = re.search(rf"WHERE {prefix}(?:catalog_name|table_cat) = '([^']*)'", sql).group(1)
    sch = re.search(rf"AND {prefix}(?:schema_name|table_schem) = '([^']*)'", sql).group(1)
    assert f"WHERE c.table_cat = '{cat}' AND c.table_schem = '{sch}' AND (" in sql
    phase = "tables" if tables else "columns"
    self._spend((cat, sch, phase) if tables else (cat, sch), timeout, 0.0 if tables else 1.0)
    self._fail((cat, sch), (cat, sch, phase))
    words = _per_word_words(sql)
    columns = sorted(self.columns.get((cat, sch), []), key=lambda r: r[0])
    if not tables:
        assert sql.count(" FROM ") == 1, "the columns' statement reads system.jdbc.columns alone"
        rows = [
            list(r) + [_own(w, r) for w in words] for r in columns if any(_own(w, r) for w in words)
        ]
        rows.sort(key=lambda r: -sum(r[4:]))  # most words first, stable after
        return _page(rows, sql, kw)
    assert f"WHERE s.catalog_name = '{cat}' AND s.schema_name = '{sch}' AND (" in sql
    assert "GROUP BY c.table_name) h ON h.table_name = s.table_name" in sql
    rows = []
    for name, comment in sorted(self._tables_of(cat, sch).items()):
        flags = [
            any(t is not None and w in _lower(t) for t in (name, comment))
            or any(r[0] == name and _own(w, r) for r in columns)
            for w in words
        ]
        if any(flags):
            rows.append([name, comment, *flags])
    rows.sort(key=lambda r: -sum(r[2:]))  # most words first, stable after
    return _page(rows, sql, kw)


Trino._per_word_tables = lambda self, sql, timeout, kw: _per_word(self, sql, timeout, kw, True)
Trino._per_word_columns = lambda self, sql, timeout, kw: _per_word(self, sql, timeout, kw, False)


@pytest.fixture
def clock(monkeypatch):
    """The audit's clock, moved by the fake client's reads. The patch is on the time
    module itself, so a thread another test left running (an event loop) reads the real
    clock until a test sets its fake client."""
    state = {"trino": None}
    real = audit_module.time.monotonic
    monkeypatch.setattr(
        audit_module.time,
        "monotonic",
        lambda: state["trino"].now if state["trino"] is not None else real(),
    )
    return state


def _server(
    trino,
    *,
    budget=60.0,
    max_rows=100,
    min_length=3,
    reserve=2.0,
    audit=None,
    max_words=8,
    max_word_length=64,
    concurrency=1,
):
    mcp = FakeMCP()
    register_query_tools(
        mcp,
        trino,
        audit=audit,
        call_timeout_seconds=budget,
        max_rows=max_rows,
        search_min_length=min_length,
        search_max_words=max_words,
        search_max_word_length=max_word_length,
        search_reserve_seconds=reserve,
        search_concurrency=concurrency,
    )
    return mcp.tools["search_columns"]


def _ask(trino, clock, *args, **kw):
    clock["trino"] = trino
    server_kw = {
        k: kw.pop(k)
        for k in (
            "budget",
            "max_rows",
            "min_length",
            "reserve",
            "audit",
            "max_words",
            "max_word_length",
            "concurrency",
        )
        if k in kw
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


def test_the_statements_read_one_schema_with_one_escaped_pattern_per_word(clock):
    """Two reads per schema, every view filtered by equality on its catalog and schema,
    which Trino pushes down to the metadata views: its tables (by their own text, or by a
    matching column they hold), then its columns, each joined to its table's comment. Every
    word is one group of ORs, the groups joined by AND; one word at least must be in the
    column's own name or comment."""
    trino = Trino({"demo": ["crm"]}, _COLUMNS)
    _ask(trino, clock, "50%_off Rate", catalog="demo", min_length=2)
    tables, columns = trino.calls[1][0], trino.calls[2][0]  # after the schemas of demo
    patterns = ("'%50%'", "'%!_off%'", "'%rate%'")
    match = (
        "FROM system.jdbc.columns c LEFT JOIN system.metadata.table_comments t "
        "ON t.catalog_name = 'demo' AND t.schema_name = 'crm' AND t.table_name = c.table_name "
        "WHERE c.table_cat = 'demo' AND c.table_schem = 'crm' AND "
        + " AND ".join(
            f"(lower(c.column_name) LIKE {p} ESCAPE '!' OR lower(c.remarks) LIKE {p} ESCAPE '!' "
            f"OR lower(c.table_name) LIKE {p} ESCAPE '!' OR lower(t.comment) LIKE {p} ESCAPE '!')"
            for p in patterns
        )
        + " AND ("
        + " OR ".join(
            f"lower(c.column_name) LIKE {p} ESCAPE '!' OR lower(c.remarks) LIKE {p} ESCAPE '!'"
            for p in patterns
        )
        + ")"
    )
    assert tables == (
        "SELECT s.table_name, s.comment FROM system.metadata.table_comments s "
        "WHERE s.catalog_name = 'demo' AND s.schema_name = 'crm' AND ("
        + " AND ".join(
            f"(lower(s.table_name) LIKE {p} ESCAPE '!' OR lower(s.comment) LIKE {p} ESCAPE '!')"
            for p in patterns
        )
        + f" OR s.table_name IN (SELECT c.table_name {match})) "
        "ORDER BY s.table_name OFFSET 0 LIMIT 101"
    )
    assert columns == (
        f"SELECT c.table_name, c.column_name, c.type_name, c.remarks {match} "
        "ORDER BY c.table_name, c.ordinal_position OFFSET 0 LIMIT 101"
    )


def test_a_hostile_text_matches_only_itself(clock):
    """Punctuation splits the words, so a quote or a % never reaches a pattern; an _ is part
    of a word and is escaped, never a wildcard."""
    columns = {("demo", "crm"): [("t", "a_b", "int", None), ("t", "axb", "int", "it's 100%")]}
    trino = Trino({"demo": ["crm"]}, columns)
    assert [m["column"] for m in _ask(trino, clock, "a_b")["matches"]] == ["a_b"]
    assert [m["column"] for m in _ask(trino, clock, "'s 100%")["matches"]] == ["axb"]
    assert all("'" not in _read_all(sql) for sql, _ in trino.calls if "LIKE" in sql)


def _read_all(sql):
    import re as _re

    return "".join(_read(p) for p in _re.findall(_LITERAL + " ESCAPE", sql))


# ---- words: every word, in a column's name or comment or in its table's name or comment

_BANK = {"demo": ["crm", "crm_archive"]}
_BANK_COLUMNS = {
    ("demo", "crm"): [
        ("customers", "id", "bigint", "Customer key"),
        ("customers", "segment", "varchar", "Marketing tier"),
        ("transactions", "amount", "decimal", "Amount in the account currency"),
        ("transactions", "category", "varchar", "Spending category"),
    ],
    ("demo", "crm_archive"): [
        ("transactions_2019", "amount", "decimal", None),
        ("transactions_2019", "booked_at", "date", None),
    ],
}
_BANK_TABLES = {
    ("demo", "crm"): {"customers": "People who hold an account", "transactions": None},
    ("demo", "crm_archive"): {"transactions_2019": "Archived card transactions of 2019"},
}


def _bank():
    return Trino(_BANK, _BANK_COLUMNS, tables=_BANK_TABLES)


def _found(out):
    return [f"{m['schema']}.{m['table']}.{m['column']}" for m in out["matches"]]


def test_every_word_must_appear_in_the_column_or_its_table(clock):
    trino = _bank()
    # "customer" in the table's name, "segment" in the column's name
    assert _found(_ask(trino, clock, "customer segment")) == ["crm.customers.segment"]
    # both words in the column's comment
    assert _found(_ask(trino, clock, "spending category")) == ["crm.transactions.category"]
    # "archived" in the table's comment only: every column of that table
    assert _found(_ask(trino, clock, "archived amount")) == ["crm_archive.transactions_2019.amount"]
    # each word found somewhere, never both for one column: nothing holds both, so the
    # answer is the per-word pass
    assert _ask(trino, clock, "customer category")["partial"] is True


def test_the_words_ignore_case_order_and_punctuation(clock):
    trino = _bank()
    one = _ask(trino, clock, "Segment, CUSTOMER!")
    assert _found(one) == ["crm.customers.segment"]


def test_tables_whose_name_or_comment_has_every_word_are_listed_with_their_comment(clock):
    trino = _bank()
    out = _ask(trino, clock, "archived transactions")
    assert out["tables"] == [
        {
            "catalog": "demo",
            "schema": "crm_archive",
            "table": "transactions_2019",
            "comment": "Archived card transactions of 2019",
        }
    ]
    assert out["matches"] == [], "a hit on the table's text alone lists the table, not its columns"
    assert [t["table"] for t in _ask(trino, clock, "2019")["tables"]] == ["transactions_2019"]
    assert [t["table"] for t in _ask(trino, clock, "hold account")["tables"]] == ["customers"]


def test_a_column_needs_one_word_in_its_own_name_or_comment(clock):
    """Every word in the column's four fields is not enough: one word at least must be its
    own, or every column of a table named by the words would flood the answer."""
    trino = _bank()
    out = _ask(trino, clock, "archived amount")
    assert _found(out) == ["crm_archive.transactions_2019.amount"]
    assert [t["table"] for t in out["tables"]] == ["transactions_2019"], "it holds a match"
    assert _ask(trino, clock, "transactions_2019")["matches"] == []
    booked = _ask(trino, clock, "booked archived 2019")
    assert _found(booked) == ["crm_archive.transactions_2019.booked_at"]


def test_a_table_that_holds_a_listed_column_is_listed_with_its_comment(clock):
    trino = _bank()
    out = _ask(trino, clock, "segment")
    assert _found(out) == ["crm.customers.segment"]
    assert out["tables"] == [
        {
            "catalog": "demo",
            "schema": "crm",
            "table": "customers",
            "comment": "People who hold an account",
        }
    ]


def test_no_hint_at_the_end_of_a_chain_that_found_something(clock):
    """The row cap reached exactly at the end of the first schema: the next page reads an
    empty second schema and ends complete, yet the chain found something, so no hint."""
    trino = Trino({"demo": ["a", "b"]}, {("demo", "a"): [("orders", "order_id", "int", None)]})
    first = _ask(trino, clock, "order", max_rows=2)
    assert (len(first["tables"]), len(first["matches"])) == (1, 1)
    assert decode_cursor(first["next_cursor"]).found is True
    last = _ask(trino, clock, "order", max_rows=2, cursor=first["next_cursor"])
    assert last["tables"] == [] and last["matches"] == [] and last["complete"] is True
    assert "hint" not in last


def test_a_chain_that_found_nothing_on_any_page_ends_with_the_hint(clock):
    slow = Trino(_BANK, _BANK_COLUMNS, tables=_BANK_TABLES, cost={("demo", "crm_archive"): 50.0})
    first = _ask(slow, clock, "loyalty points", budget=10)
    assert decode_cursor(first["next_cursor"]).found is False and "hint" not in first
    slow.cost = {}
    last = _ask(slow, clock, "loyalty points", budget=10, cursor=first["next_cursor"])
    assert last["complete"] is True and last["hint"] == HINT


def test_a_complete_search_with_no_result_at_all_gives_the_hint(clock):
    trino = _bank()
    empty = _ask(trino, clock, "loyalty points")
    assert empty["matches"] == [] and empty["tables"] == [] and empty["complete"] is True
    assert empty["hint"] == HINT
    assert "one word" in HINT and "list_tables" in HINT and "describe_schema" in HINT
    assert "hint" not in _ask(trino, clock, "segment"), "something found: no hint"
    assert "hint" not in _ask(trino, clock, "2019")
    slow = Trino(_BANK, _BANK_COLUMNS, tables=_BANK_TABLES, cost={("demo", "crm_archive"): 50.0})
    cut = _ask(slow, clock, "loyalty points", budget=10)
    assert cut["complete"] is False and cut["matches"] == [] and "hint" not in cut


def test_the_words_are_lower_cased_one_code_point_at_a_time_as_trino_does(clock):
    """str.lower writes a final sigma and turns İ into two code points; Trino's lower()
    maps each code point alone, so the words must too, or they never match."""
    assert trino_lower("ΟΔΟΣ") == "οδοσ" and "ΟΔΟΣ".lower() == "οδος"
    assert trino_lower("İZMİR") == "izmir"
    assert search_words("ΟΔΟΣ İzmir", 3) == ["οδοσ", "izmir"]
    trino = Trino({"demo": ["crm"]}, {("demo", "crm"): [("addr", "ΟΔΟΣ", "varchar", None)]})
    assert _found(_ask(trino, clock, "οδοσ")) == ["crm.addr.ΟΔΟΣ"]
    assert _found(_ask(trino, clock, "ΟΔΟΣ")) == ["crm.addr.ΟΔΟΣ"]


def test_words_shorter_than_the_minimum_are_ignored(clock):
    trino = _bank()
    assert _found(_ask(trino, clock, "a customer of segment", min_length=3)) == [
        "crm.customers.segment"
    ]
    assert search_words(" a customer, of the Customer-segment! ", 3) == [
        "customer",
        "the",
        "segment",
    ]


@pytest.mark.parametrize("text", ["  ab  ", "a b c", "%%%", "'; --", ""])
def test_a_text_without_a_word_long_enough_is_refused_and_says_so(clock, text):
    trino = Trino(_DEMO, _COLUMNS)
    out = _ask(trino, clock, text, min_length=3)
    assert out["kind"] == "invalid_argument"
    assert "no word" in out["error"] and "3 characters" in out["error"]
    assert trino.calls == []


@pytest.mark.parametrize(
    ("text", "said"),
    [
        (" ".join(f"word{i}" for i in range(9)), "9 words: give 8 at most"),
        ("x" * 65, "a word of 65 characters: a word is 64 characters at most"),
    ],
)
def test_too_many_words_or_a_word_too_long_is_refused_before_trino(clock, text, said):
    trino = Trino(_DEMO, _COLUMNS)
    out = _ask(trino, clock, text)
    assert out["kind"] == "invalid_argument" and said in out["error"]
    assert trino.calls == []


def test_the_word_bounds_count_the_words_kept(clock):
    trino = Trino(_DEMO, _COLUMNS)
    text = " ".join(["email"] * 20 + ["a", "b"] * 20)
    assert "error" not in _ask(trino, clock, text), "repeated and short words do not count"
    assert "error" not in _ask(trino, clock, "x" * 64 + " email")
    narrow = _ask(trino, clock, "email contact", max_words=1)
    assert narrow["kind"] == "invalid_argument" and "give 1 at most" in narrow["error"]
    short = _ask(trino, clock, "contact", max_word_length=5)
    assert short["kind"] == "invalid_argument" and "5 characters at most" in short["error"]


def test_each_word_is_escaped_on_its_own(clock):
    columns = {
        ("demo", "crm"): [("t", "rate_pct", "int", "100% sure"), ("t", "ratexpct", "int", None)]
    }
    trino = Trino({"demo": ["crm"]}, columns)
    out = _ask(trino, clock, "rate_ pct", catalog="demo")
    assert [m["column"] for m in out["matches"]] == ["rate_pct"]
    assert "LIKE '%rate!_%' ESCAPE '!'" in trino.calls[-1][0]


def test_the_cursor_is_tied_to_the_words_not_to_the_spelling(clock):
    assert query_digest(["customer", "segment"], "", "") != query_digest(["customer"], "", "")
    slow = Trino(_BANK, _BANK_COLUMNS, tables=_BANK_TABLES, cost={("demo", "crm_archive"): 50.0})
    first = _ask(slow, clock, "customer segment", budget=10)
    slow.cost = {}
    again = _ask(slow, clock, "Customer,  SEGMENT", budget=10, cursor=first["next_cursor"])
    assert "error" not in again and again["complete"] is True


def test_catalog_and_schema_names_are_folded_as_trino_folds_them(clock):
    trino = Trino({"demo": ["crm"]}, _COLUMNS)
    out = _ask(trino, clock, "email", catalog="Demo", schema="CRM")
    assert [(m["catalog"], m["schema"]) for m in out["matches"]] == [("demo", "crm")]
    assert all("schema_name >=" not in sql for sql, _ in trino.calls), "no schema is listed"
    assert "WHERE schema_name = 'crm'" in trino.calls[0][0]


@pytest.mark.parametrize("schema", ["crm_missing", "hidden"])
def test_a_named_schema_missing_or_hidden_answers_kind_schema_never_complete(clock, schema):
    """information_schema.schemata, filtered by Trino, shows neither a schema that does not
    exist nor one the policy hides; system.jdbc.columns filtered on either answers no row
    and no error. The search checks the schema first, as list_tables and describe_schema
    do, and answers the same for both, never an empty search taken as complete."""
    trino = Trino({"demo": ["crm"]}, {**_COLUMNS, ("demo", "hidden"): _COLUMNS[("demo", "crm")]})
    out = _ask(trino, clock, "email", catalog="demo", schema=schema)
    assert out == {"error": f"Schema demo.{schema} not found or not visible", "kind": "schema"}
    assert all("system.jdbc.columns" not in sql for sql, _ in trino.calls)


def test_a_named_schema_refusal_is_audited_as_a_failure(clock):
    trino = Trino({"demo": ["crm"]}, _COLUMNS)
    audit = InMemoryAudit()
    _ask(trino, clock, "email", catalog="demo", schema="nosuch", audit=audit)
    assert audit.events[0].ok is False and audit.events[0].error == "schema_not_found"


def test_a_named_schema_check_keeps_the_error_of_a_missing_or_forbidden_catalog(clock):
    """The check reads the catalog's information_schema: a catalog that does not exist or
    that the caller may not access answers Trino's own error and kind, as describe_schema."""
    denied = _UserError("PERMISSION_DENIED", "Access Denied: Cannot access catalog tpcds")
    missing = _UserError("CATALOG_NOT_FOUND", "Catalog 'nosuchcat' not found")
    trino = Trino({"demo": ["crm"]}, _COLUMNS, fail={"tpcds": denied, "nosuchcat": missing})
    out = _ask(trino, clock, "email", catalog="tpcds", schema="tiny")
    assert out == {"error": "Access Denied: Cannot access catalog tpcds", "kind": "permission"}
    out = _ask(trino, clock, "email", catalog="nosuchcat", schema="x")
    assert out["kind"] == "catalog" and "nosuchcat" in out["error"]
    assert all("system.jdbc.columns" not in sql for sql, _ in trino.calls)


def test_a_named_catalog_alone_that_is_missing_is_a_gap_never_complete(clock):
    missing = _UserError("CATALOG_NOT_FOUND", "Catalog 'nosuchcat' not found")
    trino = Trino({"demo": ["crm"]}, _COLUMNS, fail={"nosuchcat": missing})
    out = _ask(trino, clock, "email", catalog="nosuchcat")
    assert out["not_searched"] == [{"catalog": "nosuchcat", "schema": None, "reason": "catalog"}]
    assert out["complete"] is False and out["schemas_searched"] == 0


def test_a_named_catalog_searches_only_its_schemas(clock):
    trino = Trino(_DEMO, _COLUMNS)
    out = _ask(trino, clock, "email", catalog="lake")
    assert [m["catalog"] for m in out["matches"]] == ["lake"]
    assert all(sql != "SHOW CATALOGS" for sql, _ in trino.calls)


# ---- refusals: nothing reaches Trino


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
        {
            "c": "system",
            "s": "runtime",
            "p": "tables",
            "o": 0,
            "q": _query_of(trino, clock),
            "g": False,
        }
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
        _forge(
            {
                "c": "demo",
                "s": "crm",
                "p": "columns",
                "o": -1,
                "q": "x",
                "g": False,
                "f": False,
                "m": "every_word",
            }
        ),
        _forge(
            {
                "c": "demo",
                "s": "crm",
                "p": "columns",
                "o": True,
                "q": "x",
                "g": False,
                "f": False,
                "m": "every_word",
            }
        ),
        _forge(
            {
                "c": "demo",
                "s": "crm",
                "p": "columns",
                "o": 1.5,
                "q": "x",
                "g": False,
                "f": False,
                "m": "every_word",
            }
        ),
        _forge(
            {
                "c": "demo",
                "s": "CRM",
                "p": "columns",
                "o": 0,
                "q": "x",
                "g": False,
                "f": False,
                "m": "every_word",
            }
        ),
        _forge(
            {
                "c": "demo",
                "s": "c\u0000rm",
                "p": "columns",
                "o": 0,
                "q": "x",
                "g": False,
                "f": False,
            }
        ),
        _forge(
            {
                "c": "demo",
                "s": "s" * 1025,
                "p": "columns",
                "o": 0,
                "q": "x",
                "g": False,
                "f": False,
                "m": "every_word",
            }
        ),
        _forge(
            {
                "c": "demo",
                "s": "crm",
                "p": "columns",
                "o": 2**63,
                "q": "x",
                "g": False,
                "f": False,
                "m": "every_word",
            }
        ),
        _forge(
            {
                "c": "de'mo",
                "s": "crm",
                "p": "columns",
                "o": 0,
                "q": "x",
                "g": False,
                "f": False,
                "m": "every_word",
            }
        ),
        _forge(
            {
                "c": 7,
                "s": "crm",
                "p": "columns",
                "o": 0,
                "q": "x",
                "g": False,
                "f": False,
                "m": "every_word",
            }
        ),
        _forge(
            {
                "c": "demo",
                "s": "crm",
                "p": "columns",
                "o": 0,
                "q": 1,
                "g": False,
                "f": False,
                "m": "every_word",
            }
        ),
        _forge(
            {
                "c": "demo",
                "s": "crm",
                "p": "columns",
                "o": 0,
                "q": "x",
                "g": "no",
                "f": False,
                "m": "every_word",
            }
        ),
        _forge(
            {
                "c": "demo",
                "s": "crm",
                "p": "views",
                "o": 0,
                "q": "x",
                "g": False,
                "f": False,
                "m": "every_word",
            }
        ),
        _forge(
            {
                "c": "demo",
                "s": "crm",
                "p": "tables",
                "o": 0,
                "q": "x",
                "g": False,
                "m": "every_word",
            }
        ),
        _forge({"c": "demo", "s": "crm", "p": "tables", "o": 0, "q": "x", "g": False, "f": False}),
        _forge(
            {
                "c": "demo",
                "s": "crm",
                "p": "tables",
                "o": 0,
                "q": "x",
                "g": False,
                "f": False,
                "m": "x",
            }
        ),
        _forge(
            {
                "c": "demo",
                "s": "crm",
                "p": "tables",
                "o": 0,
                "q": "x",
                "g": False,
                "f": "yes",
                "m": "every_word",
            }
        ),
        _forge(
            {
                "c": "demo",
                "s": "crm",
                "p": None,
                "o": 0,
                "q": "x",
                "g": False,
                "f": False,
                "m": "every_word",
            }
        ),
        _forge(
            {"c": "demo", "s": "crm", "o": 0, "q": "x", "g": False, "f": False, "m": "every_word"}
        ),
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
        "error": _INVALID,
        "kind": "invalid_argument",
    }
    assert trino.calls == []


def test_a_cursor_round_trips():
    c = Cursor("demo", "crm", 40, query="abc", gaps=True, phase=COLUMNS, found=True, mode=PER_WORD)
    assert decode_cursor(encode_cursor(c)) == c
    assert decode_cursor(encode_cursor(Cursor("demo", "", 0))) == Cursor("demo", "", 0)
    assert Cursor("demo", "", 0).phase == TABLES


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
    trino = Trino(_DEMO, _COLUMNS)
    for catalog, schema, at in (("demo", "", "lake"), ("demo", "crm", "demo")):
        digest = query_digest(["email"], catalog, schema)
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


def test_a_schema_that_always_outlasts_the_call_is_skipped_by_the_next_call(clock):
    """With no query timeout of its own (TRINO_QUERY_TIMEOUT_SECONDS=0), every read is bounded
    by what is left of the call: a schema slower than a whole call would stop every resumed
    call at the same place. A call that stops exactly where it resumed, having moved nothing,
    skips that schema, so each call advances."""
    trino = Trino({"demo": ["crm", "sales"]}, _COLUMNS, cost={("demo", "crm"): 50.0})
    first = _ask(trino, clock, "email", budget=10)
    assert decode_cursor(first["next_cursor"]).schema == "crm"
    second = _ask(trino, clock, "email", budget=10, cursor=first["next_cursor"])
    assert second["not_searched"] == [
        {"catalog": "demo", "schema": "crm", "reason": "time_budget"},
        {"catalog": "demo", "schema": "sales", "reason": "time_budget"},
    ]
    resume = decode_cursor(second["next_cursor"])
    assert (resume.schema, resume.row_offset, resume.gaps) == ("sales", 0, True)
    third = _ask(trino, clock, "email", budget=10, cursor=second["next_cursor"])
    assert [m["schema"] for m in third["matches"]] == ["sales"]
    assert third["next_cursor"] is None and third["complete"] is False


def test_a_catalog_whose_schema_list_always_outlasts_the_call_is_skipped_next_call(clock):
    trino = Trino({"a": ["s"], "b": ["s"]}, {("b", "s"): [("t", "email", "int", None)]})
    trino.cost = {"a": 50.0}
    first = _ask(trino, clock, "email", budget=10)
    assert decode_cursor(first["next_cursor"]).catalog == "a"
    second = _ask(trino, clock, "email", budget=10, cursor=first["next_cursor"])
    assert second["not_searched"][0] == {"catalog": "a", "schema": None, "reason": "time_budget"}
    assert decode_cursor(second["next_cursor"]).catalog == "b"
    third = _ask(trino, clock, "email", budget=10, cursor=second["next_cursor"])
    assert len(third["matches"]) == 1 and third["complete"] is False


def test_a_call_that_moved_on_still_resumes_at_the_schema_it_stopped_in(clock):
    """Only a call that made no progress skips: one that searched a schema first and then
    ran out of time resumes the next call at the schema it stopped in."""
    trino = Trino({"a": ["s1", "s2", "s3"]}, {}, cost={("a", "s1"): 50.0})
    first = _ask(trino, clock, "email", budget=10)
    trino.cost = {("a", "s2"): 50.0}
    second = _ask(trino, clock, "email", budget=10, cursor=first["next_cursor"])
    assert second["schemas_searched"] == 1
    assert second["not_searched"] == [{"catalog": "a", "schema": "s2", "reason": "time_budget"}]
    assert decode_cursor(second["next_cursor"]).gaps is False


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
        if "system.metadata.table_comments" in sql:
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
    assert [t["table"] for t in out["tables"]] == ["t"], "the table that holds them"
    assert [m["column"] for m in out["matches"]] == ["email_0", "email_1"]
    assert out["not_searched"] == [
        {"catalog": "demo", "schema": "crm", "reason": "row_cap"},
        {"catalog": "lake", "schema": None, "reason": "row_cap"},
    ]
    assert trino.calls[-1][1]["max_rows"] == 2
    resume = decode_cursor(out["next_cursor"])
    assert (resume.catalog, resume.schema, resume.phase, resume.row_offset) == (
        "demo",
        "crm",
        COLUMNS,
        2,
    )
    second = _ask(trino, clock, "email_", max_rows=3, cursor=out["next_cursor"])
    assert [m["column"] for m in second["matches"]] == ["email_2", "email_3", "email_4"]
    assert second["tables"] == [] and "OFFSET 2 LIMIT 4" in trino.calls[-1][0]
    third = _ask(trino, clock, "email_", max_rows=3, cursor=second["next_cursor"])
    assert [m["column"] for m in third["matches"]] == ["email_5", "email_6"]
    assert third["complete"] is True


def test_a_full_page_stops_before_the_next_schema_rather_than_claim_it(clock):
    columns = {
        ("demo", "crm"): [("t", "email", "int", None)],
        ("demo", "sales"): [("t", "email", "int", None)],
    }
    trino = Trino({"demo": ["crm", "sales"]}, columns)
    out = _ask(trino, clock, "email", max_rows=2)
    assert (len(out["tables"]), len(out["matches"]), out["complete"]) == (1, 1, False)
    assert out["not_searched"] == [{"catalog": "demo", "schema": "sales", "reason": "row_cap"}]
    resume = decode_cursor(out["next_cursor"])
    assert (resume.schema, resume.phase, resume.row_offset) == ("sales", TABLES, 0)
    full_at_a_catalog = Trino({"a": ["s"], "b": ["s"]}, {("a", "s"): [("t", "email", "int", None)]})
    out = _ask(full_at_a_catalog, clock, "email", max_rows=2)
    assert out["not_searched"] == [{"catalog": "b", "schema": None, "reason": "row_cap"}]


def test_a_cursor_whose_schema_is_gone_starts_the_next_one_from_its_first_row(clock):
    trino = Trino({"demo": ["crm", "sales"]}, _COLUMNS)
    gone = encode_cursor(Cursor("demo", "crm_old", 5, query=query_digest(["email"], "", "")))
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
    token = _forge(
        {
            "c": "demo",
            "s": "it's",
            "p": "tables",
            "o": 0,
            "q": "x",
            "g": False,
            "f": False,
            "m": "every_word",
        }
    )
    assert decode_cursor(token).schema == "it's"


@pytest.mark.parametrize("catalog", ["data", "hidden"])
def test_a_cursor_naming_a_catalog_the_caller_does_not_see_is_refused(clock, catalog):
    trino = Trino({"demo": ["crm", "sales"]}, _COLUMNS)
    token = encode_cursor(Cursor(catalog, "x", 5, query=query_digest(["email"], "", "")))
    out = _ask(trino, clock, "email", cursor=token)
    assert out == {
        "error": _INVALID,
        "kind": "invalid_argument",
    }
    assert [sql for sql, _ in trino.calls] == ["SHOW CATALOGS"], "only the catalog listing"


def _wide():
    """A schema whose three tables match "order" and three of whose columns do (reorders
    by its name only), then a schema whose one column does."""
    columns = {
        ("demo", "a"): [
            ("orders", "order_id", "int", None),
            ("orders", "total", "int", "Order total"),
            ("order_lines", "order_id", "int", None),
            ("reorders", "at", "date", None),
        ],
        ("demo", "b"): [("t", "order_ref", "int", None)],
    }
    return Trino({"demo": ["a", "b"]}, columns)


def test_the_row_cap_is_shared_by_the_tables_and_the_columns(clock):
    trino = _wide()
    out = _ask(trino, clock, "order", max_rows=5)
    assert len(out["tables"]) + len(out["matches"]) == 5
    assert [t["table"] for t in out["tables"]] == ["order_lines", "orders", "reorders"]
    assert _found(out) == ["a.order_lines.order_id", "a.orders.order_id"]
    assert trino.calls[-1][1]["max_rows"] == 2, "the columns get what the tables left"
    resume = decode_cursor(out["next_cursor"])
    assert (resume.schema, resume.phase, resume.row_offset) == ("a", COLUMNS, 2)
    assert out["not_searched"][0] == {"catalog": "demo", "schema": "a", "reason": "row_cap"}


def test_a_page_filled_by_tables_resumes_at_the_columns_of_that_schema(clock):
    trino = _wide()
    out = _ask(trino, clock, "order", max_rows=3)
    assert len(out["tables"]) == 3 and out["matches"] == [] and out["complete"] is False
    resume = decode_cursor(out["next_cursor"])
    assert (resume.schema, resume.phase, resume.row_offset) == ("a", COLUMNS, 0)
    assert not any(sql.startswith("SELECT c.table_name, c.column_name") for sql, _ in trino.calls)


def test_a_page_cut_inside_the_tables_resumes_inside_the_tables(clock):
    trino = _wide()
    out = _ask(trino, clock, "order", max_rows=2)
    assert [t["table"] for t in out["tables"]] == ["order_lines", "orders"]
    resume = decode_cursor(out["next_cursor"])
    assert (resume.schema, resume.phase, resume.row_offset) == ("a", TABLES, 2)
    again = _ask(trino, clock, "order", max_rows=2, cursor=out["next_cursor"])
    assert [t["table"] for t in again["tables"]] == ["reorders"]
    assert "OFFSET 2 LIMIT 3" in trino.calls[-2][0]
    assert _found(again) == ["a.order_lines.order_id"]


def test_a_chain_of_cursors_gives_every_table_and_column_once_then_complete(clock):
    trino = _wide()
    whole = _ask(trino, clock, "order")
    assert whole["complete"] is True and whole["schemas_searched"] == 2
    assert [t["table"] for t in whole["tables"]] == ["order_lines", "orders", "reorders", "t"]
    assert _found(whole) == [
        "a.order_lines.order_id",
        "a.orders.order_id",
        "a.orders.total",
        "b.t.order_ref",
    ], "reorders.at holds no word of its own"
    tables, columns, token, calls = [], [], "", 0
    while True:
        out = _ask(trino, clock, "order", max_rows=2, cursor=token)
        tables += out["tables"]
        columns += out["matches"]
        calls += 1
        if out["next_cursor"] is None:
            break
        assert out["complete"] is False
        token = out["next_cursor"]
    assert tables == whole["tables"] and columns == whole["matches"]
    assert out["complete"] is True and calls == 4 and "hint" not in out


def test_a_schema_whose_tables_read_outlasts_the_call_resumes_then_is_skipped(clock):
    trino = _wide()
    trino.cost = {("demo", "a", "tables"): 50.0}
    first = _ask(trino, clock, "order", budget=10)
    resume = decode_cursor(first["next_cursor"])
    assert (resume.schema, resume.phase, resume.row_offset) == ("a", TABLES, 0)
    second = _ask(trino, clock, "order", budget=10, cursor=first["next_cursor"])
    assert second["not_searched"] == [
        {"catalog": "demo", "schema": "a", "reason": "time_budget"},
        {"catalog": "demo", "schema": "b", "reason": "time_budget"},
    ]
    assert second["tables"] == [] and second["matches"] == []
    third = _ask(trino, clock, "order", budget=10, cursor=second["next_cursor"])
    assert _found(third) == ["b.t.order_ref"], "a skipped schema gives none of its columns"
    assert third["complete"] is False and third["next_cursor"] is None and "hint" not in third


def test_a_schema_whose_columns_fail_after_its_tables_is_named_and_keeps_its_tables(clock):
    trino = _wide()
    trino.fail = {("demo", "a", "columns"): _InternalError("metastore down")}
    out = _ask(trino, clock, "order")
    assert [t["table"] for t in out["tables"]] == ["order_lines", "orders", "reorders", "t"]
    assert out["not_searched"] == [{"catalog": "demo", "schema": "a", "reason": "unavailable"}]
    assert _found(out) == ["b.t.order_ref"] and out["schemas_searched"] == 1
    assert out["complete"] is False and "hint" not in out


def test_a_refused_comment_view_answers_the_error_that_names_what_to_allow(clock):
    denied = _UserError(
        "PERMISSION_DENIED",
        "Access Denied: Cannot select from table system.metadata.table_comments",
    )
    trino = _wide()
    trino.fail = {("demo", "a", "tables"): denied}
    out = _ask(trino, clock, "order")
    assert out["kind"] == "permission" and "system.metadata.table_comments" in out["error"]
    assert "tables" not in out and "matches" not in out


# ---- the per-word pass: nothing holds every word, so each word alone

_SHOP = {"demo": ["crm"]}
_SHOP_COLUMNS = {
    ("demo", "crm"): [
        ("accounts", "id", "bigint", None),
        ("accounts", "customer_id", "bigint", "Customer who holds it"),
        ("accounts", "balance", "decimal", "Current balance"),
        ("customers", "id", "bigint", "Customer key"),
        ("customers", "segment", "varchar", "Retail or business"),
        ("transactions", "amount", "decimal", "Booked amount: the revenue of a sale"),
        ("transactions", "category", "varchar", "Spending category"),
        ("transactions", "booked_at", "date", None),
    ]
}
_SHOP_TABLES = {
    ("demo", "crm"): {
        "accounts": "Bank accounts",
        "customers": "People who hold an account",
        "transactions": "Card transactions",
    }
}


def _shop(**kw):
    return Trino(_SHOP, _SHOP_COLUMNS, tables=_SHOP_TABLES, **kw)


def _marked(out):
    return (
        [(t["table"], t["words"]) for t in out["tables"]],
        [(f"{m['table']}.{m['column']}", m["words"]) for m in out["matches"]],
    )


def test_several_words_that_match_nothing_together_give_each_word_alone(clock):
    """The question put whole into a search: no row holds every word, and a date is no
    metadata. The same call answers each word alone, marked partial with its words."""
    out = _ask(_shop(), clock, "spending category March 2026")
    assert out["partial"] is True and out["hint"] == PER_WORD_HINT
    assert out["schemas_searched"] == 1, "the per-word pass's schemas, not both passes'"
    assert _marked(out) == (
        [("transactions", ["spending", "category"])],
        [("transactions.category", ["spending", "category"])],
    )
    assert out["complete"] is True and out["next_cursor"] is None and out["not_searched"] == []


def test_each_word_alone_finds_its_tables_and_columns_by_the_same_rules(clock):
    out = _ask(_shop(), clock, "revenue customer")
    assert _marked(out) == (
        [("accounts", ["customer"]), ("customers", ["customer"]), ("transactions", ["revenue"])],
        [
            ("accounts.customer_id", ["customer"]),
            ("customers.id", ["customer"]),
            ("transactions.amount", ["revenue"]),
        ],
    )
    assert "customers.segment" not in _marked(out)[1], "a column needs a word of its own"
    assert all(m["comment"] is not None for m in out["matches"])


def test_the_per_word_pass_reads_each_schema_in_two_bounded_statements(clock):
    trino = _shop()
    _ask(trino, clock, "revenue customer", catalog="demo")
    tables, columns = trino.calls[-2][0], trino.calls[-1][0]
    own = [
        f"(lower(c.column_name) LIKE {p} ESCAPE '!' OR lower(c.remarks) LIKE {p} ESCAPE '!')"
        for p in ("'%revenue%'", "'%customer%'")
    ]
    any_own = (
        "(lower(c.column_name) LIKE '%revenue%' ESCAPE '!' "
        "OR lower(c.remarks) LIKE '%revenue%' ESCAPE '!' "
        "OR lower(c.column_name) LIKE '%customer%' ESCAPE '!' "
        "OR lower(c.remarks) LIKE '%customer%' ESCAPE '!')"
    )
    assert columns == (
        "SELECT c.table_name, c.column_name, c.type_name, c.remarks, "
        f"coalesce({own[0]}, false), coalesce({own[1]}, false) "
        "FROM system.jdbc.columns c WHERE c.table_cat = 'demo' AND c.table_schem = 'crm' "
        f"AND {any_own} ORDER BY (CAST(coalesce({own[0]}, false) AS integer) + "
        f"CAST(coalesce({own[1]}, false) AS integer)) DESC, c.table_name, c.ordinal_position "
        "OFFSET 0 LIMIT 98"
    ), "the three tables found leave 97 rows of the cap"
    assert tables == (
        "SELECT s.table_name, s.comment, "
        "coalesce(lower(s.table_name) LIKE '%revenue%' ESCAPE '!' "
        "OR lower(s.comment) LIKE '%revenue%' ESCAPE '!' OR h.w0, false), "
        "coalesce(lower(s.table_name) LIKE '%customer%' ESCAPE '!' "
        "OR lower(s.comment) LIKE '%customer%' ESCAPE '!' OR h.w1, false) "
        "FROM system.metadata.table_comments s LEFT JOIN (SELECT c.table_name, "
        f"bool_or(coalesce({own[0]}, false)) AS w0, bool_or(coalesce({own[1]}, false)) AS w1 "
        "FROM system.jdbc.columns c WHERE c.table_cat = 'demo' AND c.table_schem = 'crm' "
        f"AND {any_own} GROUP BY c.table_name) h ON h.table_name = s.table_name "
        "WHERE s.catalog_name = 'demo' AND s.schema_name = 'crm' "
        "AND ((lower(s.table_name) LIKE '%revenue%' ESCAPE '!' "
        "OR lower(s.comment) LIKE '%revenue%' ESCAPE '!' "
        "OR lower(s.table_name) LIKE '%customer%' ESCAPE '!' "
        "OR lower(s.comment) LIKE '%customer%' ESCAPE '!') OR h.table_name IS NOT NULL) "
        "ORDER BY (CAST(coalesce(lower(s.table_name) LIKE '%revenue%' ESCAPE '!' "
        "OR lower(s.comment) LIKE '%revenue%' ESCAPE '!' OR h.w0, false) AS integer) + "
        "CAST(coalesce(lower(s.table_name) LIKE '%customer%' ESCAPE '!' "
        "OR lower(s.comment) LIKE '%customer%' ESCAPE '!' OR h.w1, false) AS integer)) DESC, "
        "s.table_name OFFSET 0 LIMIT 101"
    )
    assert len(trino.calls) == 6, "for each pass, the schemas of demo, then two reads"


def test_the_rows_most_words_found_come_first_in_the_per_word_pass(clock):
    """A common word fills pages of tables in name order; the row that holds several words
    of the question must not wait behind them."""
    columns = {
        ("demo", "crm"): [
            ("a_customers", "customer_name", "varchar", None),
            ("b_customers", "customer_code", "varchar", None),
            ("z_ledger", "customer_balance", "decimal", None),
        ]
    }
    out = _ask(Trino(_SHOP, columns), clock, "customer balance points")
    assert [m["column"] for m in out["matches"]] == [
        "customer_balance",
        "customer_name",
        "customer_code",
    ]
    assert out["tables"][0]["table"] == "z_ledger"


def test_one_word_that_matches_nothing_gives_the_hint_and_no_second_pass(clock):
    trino = _shop()
    out = _ask(trino, clock, "loyalty")
    assert out["hint"] == HINT and "partial" not in out
    assert "never a value" in HINT and "one word" in HINT
    assert not any(", coalesce(" in sql for sql, _ in trino.calls)


def test_words_that_match_nothing_alone_either_end_with_the_hint(clock):
    out = _ask(_shop(), clock, "loyalty points")
    assert out["partial"] is True and out["hint"] == HINT
    assert out["tables"] == [] and out["matches"] == [] and out["complete"] is True


def test_a_search_that_found_every_word_has_no_per_word_pass(clock):
    trino = _shop()
    out = _ask(trino, clock, "spending category")
    assert "partial" not in out and "hint" not in out
    assert _found(out) == ["crm.transactions.category"]
    assert not any(", coalesce(" in sql for sql, _ in trino.calls)


def test_an_incomplete_search_that_found_nothing_has_no_per_word_pass_yet(clock):
    trino = _shop(cost={("demo", "crm"): 50.0})
    out = _ask(trino, clock, "revenue customer", budget=10)
    assert out["complete"] is False and "partial" not in out and "hint" not in out
    assert decode_cursor(out["next_cursor"]).mode == EVERY_WORD


def test_the_per_word_pass_cut_by_the_row_cap_says_so_and_resumes_to_the_end(clock):
    trino = _shop()
    whole = _ask(trino, clock, "revenue customer")
    pages, token = [], ""
    while True:
        out = _ask(trino, clock, "revenue customer", max_rows=2, cursor=token)
        pages.append(out)
        assert out["partial"] is True and all("words" in r for r in out["tables"] + out["matches"])
        assert len(out["tables"]) + len(out["matches"]) <= 2
        if out["next_cursor"] is None:
            break
        assert out["complete"] is False and out["not_searched"][0]["reason"] == "row_cap"
        assert out["hint"] == PER_WORD_MORE_HINT, "a cut page sends to next_cursor"
        token = out["next_cursor"]
        assert decode_cursor(token).mode == PER_WORD
    assert [t for p in pages for t in p["tables"]] == whole["tables"]
    assert [m for p in pages for m in p["matches"]] == whole["matches"]
    assert pages[-1]["complete"] is True and len(pages) == 3
    assert pages[-1]["hint"] == PER_WORD_HINT


def test_the_per_word_pass_out_of_time_says_so_and_resumes(clock):
    """The first pass takes the call's time: the per-word pass starts, finds no time left,
    and says what it did not search, never complete."""
    trino = _shop(cost={("demo", "crm"): 6.0})
    first = _ask(trino, clock, "revenue customer", budget=10)
    assert first["partial"] is True and first["complete"] is False
    assert first["tables"] == [] and first["matches"] == []
    assert first["hint"] == PER_WORD_MORE_HINT, "an empty page with more to read is no end"
    assert "next_cursor" in PER_WORD_MORE_HINT and "not finished" in PER_WORD_MORE_HINT
    assert first["not_searched"] == [{"catalog": "demo", "schema": "crm", "reason": "time_budget"}]
    resume = decode_cursor(first["next_cursor"])
    assert (resume.mode, resume.schema, resume.phase) == (PER_WORD, "crm", TABLES)
    trino.cost = {}
    again = _ask(trino, clock, "revenue customer", budget=10, cursor=first["next_cursor"])
    assert again["partial"] is True and again["complete"] is True
    assert [m["column"] for m in again["matches"]] == ["customer_id", "id", "amount"]
    assert [t["table"] for t in again["tables"]] == ["accounts", "customers", "transactions"]
    assert again["hint"] == PER_WORD_HINT


def test_a_per_word_pass_that_skipped_a_schema_and_found_nothing_gives_the_hint(clock):
    """A schema skipped in the per-word pass (an error of Trino, or a read slower than a
    query may be): the pass ends incomplete, with no cursor and no row. It has no partial
    match to pick from, so its hint is the one for nothing found."""
    trino = Trino({"demo": ["a", "b"]}, {("demo", "a"): [], ("demo", "b"): []})
    real = trino.query

    def query(sql, user=None, params=None, **kw):
        if ", coalesce(" in sql and "'b'" in sql:
            trino.calls.append((sql, kw))
            raise _InternalError("metastore down")
        return real(sql, user, params, **kw)

    trino.query = query
    out = _ask(trino, clock, "loyalty points")
    assert out["partial"] is True and out["complete"] is False and out["next_cursor"] is None
    assert out["not_searched"] == [{"catalog": "demo", "schema": "b", "reason": "unavailable"}]
    assert out["tables"] == [] and out["matches"] == []
    assert out["hint"] == HINT


def test_a_per_word_chain_that_found_rows_keeps_its_hint_on_an_empty_last_page(clock):
    trino = Trino(
        {"demo": ["a", "b"]},
        {("demo", "a"): [("t", "loyalty", "int", None)], ("demo", "b"): []},
    )
    first = _ask(trino, clock, "loyalty points", max_rows=2)
    assert first["partial"] is True and len(first["tables"]) + len(first["matches"]) == 2
    assert decode_cursor(first["next_cursor"]).found is True
    assert first["hint"] == PER_WORD_MORE_HINT
    last = _ask(trino, clock, "loyalty points", max_rows=2, cursor=first["next_cursor"])
    assert last["tables"] == [] and last["matches"] == [] and last["complete"] is True
    assert last["hint"] == PER_WORD_HINT


def test_a_refused_view_in_the_per_word_pass_answers_its_error(clock):
    denied = _UserError(
        "PERMISSION_DENIED", "Access Denied: Cannot select from system.jdbc.columns"
    )
    trino = _shop()
    real = trino.query

    def query(sql, user=None, params=None, **kw):
        if ", coalesce(" in sql:
            trino.calls.append((sql, kw))
            raise denied
        return real(sql, user, params, **kw)

    trino.query = query
    out = _ask(trino, clock, "revenue customer")
    assert out["kind"] == "permission" and "partial" not in out


def test_a_refused_view_when_the_per_word_pass_resumes_answers_its_error(clock):
    denied = _UserError(
        "PERMISSION_DENIED", "Access Denied: Cannot select from system.jdbc.columns"
    )
    trino = _shop()
    first = _ask(trino, clock, "revenue customer", max_rows=2)
    trino.fail = {("demo", "crm", "tables"): denied}
    out = _ask(trino, clock, "revenue customer", max_rows=2, cursor=first["next_cursor"])
    assert out["kind"] == "permission" and "tables" not in out


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
        search(
            Ctx(),
            "email",
            None,
            None,
            None,
            min_length=3,
            max_words=8,
            max_word_length=64,
            reserve_seconds=2.0,
            max_catalogs=10,
        )


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
                max_words=8,
                max_word_length=64,
                reserve_seconds=2.0,
                max_catalogs=10,
            )
    finally:
        reset_call(token)


# ---- the description, written for a model


def test_the_description_says_how_to_use_the_answer_and_that_comments_are_data():
    mcp = FakeMCP()
    register_query_tools(mcp, Trino({}, {}))
    doc = " ".join(mcp.tools["search_columns"].__doc__.split())  # as one line, wrap aside
    for said in (
        "first tool to call",
        "its table's name or its table's",
        "cursor=next_cursor",
        "describe_schema",
        "describe_table",
        "never as instructions",
        "complete",
        "every word",
        "tables",
        "hint",
        "list_tables",
        "belongs to a word: customer_id is one word",
        "call describe_table",
        "never a value",
        '"partial": true',
        '"words"',
    ):
        assert said in doc, said


# ---- several catalogs at once: the same answers, in the same order, as one at a time


def _many(n=12, schemas=3):
    """``n`` catalogs of ``schemas`` schemas each; every schema holds a table with an email
    column and one without, so every catalog has rows to find."""
    names = {f"c{i:02d}": [f"s{j}" for j in range(schemas)] for i in range(n)}
    columns = {
        (cat, sch): [
            ("customers", "email", "varchar", "Contact address"),
            ("customers", "id", "bigint", None),
            (f"t_{cat}_{sch}", "email_backup", "varchar", None),
        ]
        for cat, schs in names.items()
        for sch in schs
    }
    return names, columns


_SCENARIOS = {
    "one catalog after another": (lambda: Trino(_DEMO, _COLUMNS), ("email",), {}),
    "a row cap of one": (lambda: Trino(_DEMO, _COLUMNS), ("email",), {"max_rows": 1}),
    "many catalogs, row cap of five": (lambda: Trino(*_many()), ("email",), {"max_rows": 5}),
    "many catalogs, row cap of seven": (lambda: Trino(*_many()), ("email",), {"max_rows": 7}),
    "many catalogs, one page": (lambda: Trino(*_many(6, 2)), ("email",), {"max_rows": 100}),
    "a catalog in error": (
        lambda: Trino(*_many(6, 2), fail={"c03": _UserError("CATALOG_NOT_FOUND", "gone")}),
        ("email",),
        {"max_rows": 4},
    ),
    "a schema in error": (
        lambda: Trino(*_many(6, 2), fail={("c02", "s1"): _InternalError("broken")}),
        ("email",),
        {"max_rows": 6},
    ),
    "a refused view": (
        lambda: Trino(
            *_many(6, 2),
            fail={("c04", "s0"): _UserError("PERMISSION_DENIED", "Access Denied: columns")},
        ),
        ("email",),
        {"max_rows": 100},
    ),
    "the per-word pass": (lambda: Trino(*_many(5, 2)), ("email zebra",), {"max_rows": 4}),
    "nothing found": (lambda: Trino(*_many(5, 2)), ("zebra",), {}),
    "one catalog named": (lambda: Trino(*_many(4, 6)), ("email", "c01"), {"max_rows": 4}),
}


# The scenarios that answer in one page; every other one follows a chain of cursors.
_ONE_PAGE = {
    "one catalog after another",
    "many catalogs, one page",
    "a refused view",
    "nothing found",
}


def _chain(clock, make, args, settings, concurrency):
    """Every page of a search, following next_cursor to its end."""
    pages, cursor = [], ""
    for _ in range(100):
        trino = make()
        out = _ask(
            trino, clock, *args, cursor=cursor, budget=100000, concurrency=concurrency, **settings
        )
        pages.append(out)
        cursor = out.get("next_cursor")
        if not cursor:
            return pages
    raise AssertionError("the chain of cursors never ends")


@pytest.mark.parametrize("name", list(_SCENARIOS))
@pytest.mark.parametrize("concurrency", [2, 8, 32])
def test_several_catalogs_at_once_answer_as_one_at_a_time(clock, name, concurrency):
    make, args, settings = _SCENARIOS[name]
    one = _chain(clock, make, args, settings, 1)
    assert _chain(clock, make, args, settings, concurrency) == one
    assert len(one) > 1 or name in _ONE_PAGE, "the scenario follows a chain of cursors"


# What the search of 0.7 (5a97846) sends, and answers, on the fixtures of _many(4, 2): the
# digest of its statements, one per line, in their order, and of its answer, recorded by
# running that version.
_RECORDED_0_7 = {
    ("email", 5): (
        5,
        "92d101e28573a21f4d3b96746376c10149d6e4ce59a04d9e7384fd01932faeb0",
        "e47ad6e36daa2887876de9cda8267b192ea358a758a81008f4a3224605716860",
    ),
    ("email zebra", 4): (
        24,
        "919d68da5d8d023b683f1009561359d47e665b25ee8cb59b920eff3004a4401a",
        "3400a252f5293041888de7efcea0c9d4c8bed03a9aac65d16db3b3a21b513fa4",
    ),
}


@pytest.mark.parametrize("text, rows", list(_RECORDED_0_7))
def test_one_catalog_at_a_time_sends_the_statements_of_0_7_in_their_order(clock, text, rows):
    """MCP_SEARCH_CONCURRENCY=1 is the search of 0.7: the same statements in the same order,
    and the same answer, byte for byte."""
    import hashlib

    trino = Trino(*_many(4, 2))
    clock["trino"] = trino
    out = _server(trino, budget=100000, max_rows=rows, concurrency=1)(text)
    statements = [sql for sql, _ in trino.calls]
    count, sent, answer = _RECORDED_0_7[(text, rows)]
    assert len(statements) == count
    assert hashlib.sha256("\n".join(statements).encode()).hexdigest() == sent
    assert hashlib.sha256(out.encode()).hexdigest() == answer


class _Barrier(Trino):
    """Trino whose read of the schemas of each catalog waits for ``n`` such reads at once."""

    def __init__(self, n, *args, **kw):
        super().__init__(*args, **kw)
        self.barrier = threading.Barrier(n, timeout=5)

    def query(self, sql, user=None, params=None, **kw):
        if "information_schema.schemata" in sql:
            self.barrier.wait()
        return super().query(sql, user, params, **kw)


def test_several_catalogs_are_read_at_once(clock):
    names, columns = _many(4, 1)
    out = _ask(_Barrier(4, names, columns), clock, "email", budget=100000, concurrency=4)
    assert out["not_searched"] == [] and out["complete"] is True
    assert {m["catalog"] for m in out["matches"]} == set(names)


def test_one_at_a_time_never_reads_two_catalogs_at_once(clock):
    names, columns = _many(2, 1)
    trino = _Barrier(2, names, columns)
    trino.barrier = threading.Barrier(2, timeout=0.2)
    out = _ask(trino, clock, "email", budget=100000, concurrency=1)
    assert out["complete"] is False, "the reads waited for a second one that never came"


class _Slow(Trino):
    """Trino whose catalogs after the first answer their schemas only after ``pause``
    seconds of real time, and that records which catalogs it was asked about."""

    def __init__(self, pause, *args, **kw):
        super().__init__(*args, **kw)
        self.pause = pause
        self.opened = []
        self.lock = threading.Lock()

    def query(self, sql, user=None, params=None, **kw):
        found = re.search(r"FROM (\S+)\.information_schema\.schemata", sql)
        if found:
            with self.lock:
                self.opened.append(found.group(1))
            if found.group(1) != "c00":
                time.sleep(self.pause)
        return super().query(sql, user, params, **kw)


def _search_threads():
    """The threads of a search: its walks and the stops of its cancellation."""
    return [t for t in threading.enumerate() if t.name.startswith(("mcp-search", "mcp-cancel"))]


def test_no_thread_outlives_the_call_and_no_catalog_starts_after_the_stop(clock):
    names, columns = _many(20, 1)
    trino = _Slow(0.2, names, columns)
    out = _ask(trino, clock, "email", max_rows=2, budget=100000, concurrency=2)
    assert out["tables"][0]["catalog"] == "c00" and out["next_cursor"] is not None
    assert _search_threads() == [], "every thread of the call has ended with it"
    assert "c19" not in trino.opened, "a catalog not started when the page was full never is"


def test_each_read_of_several_at_once_is_bounded_by_what_is_left_of_the_call(clock):
    trino = Trino(*_many(6, 2))
    _ask(trino, clock, "email", budget=500, reserve=2, concurrency=4)
    reads = [kw for sql, kw in trino.calls if sql != "SHOW CATALOGS"]
    assert reads and all(0 < kw["timeout"] <= 498 for kw in reads)


# ---- a window of catalogs, cancelled at once when the pass stops, and a bound per process


class _Paced(Trino):
    """Trino whose read of the schemas of a catalog of ``pauses`` takes that many seconds of
    real time: stopped at once by the cancellation it was given, or as the client stops it
    at its own ``timeout``. It records the catalogs opened, those cancelled, and the
    threads that read them."""

    def __init__(self, pauses, *args, finish=(), **kw):
        super().__init__(*args, **kw)
        self.pauses = pauses
        self.finish = set(finish)  # catalogs whose read ends normally even once cancelled
        self.opened, self.cancelled, self.threads = [], [], []
        self.lock = threading.Lock()

    def _spend(self, key, timeout, default=1.0):
        """No simulated time: this fake waits for real."""

    def query(self, sql, user=None, params=None, **kw):
        found = re.search(r"FROM (\S+)\.information_schema\.schemata", sql)
        if found:
            cat = found.group(1)
            with self.lock:
                self.opened.append(cat)
                self.threads.append(threading.current_thread().name)
            pause = self.pauses.get(cat, 0)
            if pause:
                woken = threading.Event()
                if kw.get("cancel") is not None:
                    kw["cancel"].watch(woken.set)
                timeout = kw.get("timeout", 0)
                if woken.wait(min(pause, timeout) if timeout > 0 else pause):
                    with self.lock:
                        self.cancelled.append(cat)
                    if cat not in self.finish:
                        raise RuntimeError("Query was canceled")
                    return super().query(sql, user, params, **kw)
                if 0 < timeout < pause:
                    raise TimeoutError(f"the query exceeded its time budget of {timeout:g} s")
        return super().query(sql, user, params, **kw)


def test_a_slow_first_catalog_lets_no_more_than_n_catalogs_be_read_ahead(clock):
    names, columns = _many(30, 1)
    trino = _Paced({"c00": 0.5}, names, columns)
    _ask(trino, clock, "email", max_rows=2, budget=100000, concurrency=4)
    assert set(trino.opened) <= {"c00", "c01", "c02", "c03"}, "a window of 4 catalogs"


def test_a_read_in_flight_is_cancelled_at_once_when_the_page_is_full(clock):
    names, columns = _many(6, 1)
    trino = _Paced({"c00": 0.3, "c01": 8}, names, columns)  # c01 runs while c00 answers
    started = time.monotonic()
    out = _ask(trino, clock, "email", max_rows=2, budget=100000, concurrency=2)
    assert time.monotonic() - started < 3, "the answer never waits for a read it discards"
    assert trino.cancelled == ["c01"] and out["next_cursor"] is not None
    assert _search_threads() == [], "no walk and no stop of the cancellation outlives the call"


def test_a_catalog_out_of_time_stops_the_pass_as_one_at_a_time(monkeypatch):
    """The time path at several catalogs at once, on the real clock: a catalog that runs past
    what is left of the call stops the pass there, as it does one catalog at a time."""
    answers = []
    for concurrency in (1, 4):
        names, columns = _many(6, 1)
        trino = _Paced({"c01": 30}, names, columns)
        tool = _server(trino, budget=4, reserve=2, concurrency=concurrency)
        answers.append(json.loads(tool("email")))
    one, several = answers
    assert one["not_searched"][0] == {"catalog": "c01", "schema": None, "reason": "time_budget"}
    assert several == one


def test_with_no_search_thread_free_a_call_reads_its_catalogs_itself(clock):
    from akko_mcp_trino.search import search as run_search

    answers = []
    for concurrency in (1, 8):
        trino = _Paced({}, *_many(6, 2))
        clock["trino"] = trino
        tools = register_query_tools(FakeMCP(), trino, max_rows=5)
        slots = threading.BoundedSemaphore(1)
        slots.acquire()  # every slot of the process is taken by other calls
        call, token = bind_call("search_columns", 100000)
        try:
            answers.append(
                run_search(
                    tools,
                    "email",
                    None,
                    None,
                    None,
                    min_length=3,
                    max_words=8,
                    max_word_length=64,
                    reserve_seconds=2,
                    max_catalogs=100,
                    concurrency=concurrency,
                    slots=slots,
                )
            )
        finally:
            reset_call(token)
        assert not [t for t in trino.threads if t.startswith("mcp-search")], "read by the call"
    assert answers[0] == answers[1]


class _Peak(_Paced):
    """Trino that counts the reads of search threads in flight at once."""

    def __init__(self, *args, **kw):
        super().__init__({}, *args, **kw)
        self.now_in, self.peak = 0, 0

    def query(self, sql, user=None, params=None, **kw):
        pooled = threading.current_thread().name.startswith("mcp-search")
        if pooled:
            with self.lock:
                self.now_in += 1
                self.peak = max(self.peak, self.now_in)
        try:
            time.sleep(0.01)
            return super().query(sql, user, params, **kw)
        finally:
            if pooled:
                with self.lock:
                    self.now_in -= 1


def test_the_reads_of_search_threads_in_a_process_stay_under_their_bound(clock):
    trino = _Peak(*_many(12, 1))
    clock["trino"] = trino
    mcp = FakeMCP()
    register_query_tools(
        mcp, trino, call_timeout_seconds=100000, search_concurrency=8, search_max_threads=2
    )
    out = json.loads(mcp.tools["search_columns"]("email"))
    assert out["complete"] is True and trino.peak <= 2


def test_a_walk_the_pass_no_longer_needs_sends_no_other_statement(clock):
    """A read that ends just as the pass stops: the walk of its catalog goes no further."""
    names, columns = _many(4, 1)
    trino = _Paced({"c00": 0.3, "c01": 8}, names, columns, finish={"c01"})
    out = _ask(trino, clock, "email", max_rows=2, budget=100000, concurrency=2)
    assert out["next_cursor"] is not None and trino.cancelled == ["c01"]
    assert not [sql for sql, _ in trino.calls if "'c01'" in sql], "no read of c01's schemas"
