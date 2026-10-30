"""search_columns: a search by words, in columns and tables, schema by schema, in time.

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
from akko_mcp_trino.search import (
    COLUMNS,
    HINT,
    TABLES,
    Cursor,
    decode_cursor,
    encode_cursor,
    like_contains,
    query_digest,
    search,
    search_words,
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
_TABLE_FIELDS = ("table_name", "comment")


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


def _has(words, *texts):
    """Whether every word is in one of the texts, as lower(x) LIKE does (null never)."""
    return all(any(t is not None and w in t.lower() for t in texts) for w in words)


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
        if sql.startswith("SELECT table_name, comment FROM system.metadata.table_comments "):
            cat = re.search(r"WHERE catalog_name = '([^']*)'", sql).group(1)
            sch = re.search(r"AND schema_name = '([^']*)'", sql).group(1)
            self._spend((cat, sch, "tables"), timeout, default=0.0)
            self._fail((cat, sch), (cat, sch, "tables"))
            words = _words_of(sql, _TABLE_FIELDS)
            rows = [
                [name, comment]
                for name, comment in sorted(self._tables_of(cat, sch).items())
                if _has(words, name, comment)
            ]
        else:
            assert sql.startswith(
                "SELECT c.table_name, c.column_name, c.type_name, c.remarks "
                "FROM system.jdbc.columns c LEFT JOIN system.metadata.table_comments t "
            ), sql
            cat = re.search(r"WHERE c\.table_cat = '([^']*)'", sql).group(1)
            sch = re.search(r"AND c\.table_schem = '([^']*)'", sql).group(1)
            assert (
                f"ON t.catalog_name = '{cat}' AND t.schema_name = '{sch}' "
                "AND t.table_name = c.table_name WHERE" in sql
            ), "the join reads the comments of the same schema only"
            self._spend((cat, sch), timeout)
            self._fail((cat, sch), (cat, sch, "columns"))
            words = _words_of(sql, _COLUMN_FIELDS)
            comments = self._tables_of(cat, sch)
            rows = [
                list(r)
                for r in sorted(self.columns.get((cat, sch), []), key=lambda r: r[0])
                if _has(words, r[1], r[3], r[0], comments.get(r[0]))
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


def test_the_statements_read_one_schema_with_one_escaped_pattern_per_word(clock):
    """Two reads per schema, both filtered by equality on its catalog and schema, which
    Trino pushes down to the metadata views: its tables, then its columns, each joined to
    its table's comment. Every word is one group of ORs, the groups joined by AND."""
    trino = Trino({"demo": ["crm"]}, _COLUMNS)
    _ask(trino, clock, "50%_off Rate", catalog="demo", min_length=2)
    tables, columns = trino.calls[-2][0], trino.calls[-1][0]
    assert tables == (
        "SELECT table_name, comment FROM system.metadata.table_comments "
        "WHERE catalog_name = 'demo' AND schema_name = 'crm' "
        "AND (lower(table_name) LIKE '%50%' ESCAPE '!' OR lower(comment) LIKE '%50%' ESCAPE '!') "
        "AND (lower(table_name) LIKE '%!_off%' ESCAPE '!' "
        "OR lower(comment) LIKE '%!_off%' ESCAPE '!') "
        "AND (lower(table_name) LIKE '%rate%' ESCAPE '!' "
        "OR lower(comment) LIKE '%rate%' ESCAPE '!') "
        "ORDER BY table_name OFFSET 0 LIMIT 101"
    )
    assert columns == (
        "SELECT c.table_name, c.column_name, c.type_name, c.remarks "
        "FROM system.jdbc.columns c LEFT JOIN system.metadata.table_comments t "
        "ON t.catalog_name = 'demo' AND t.schema_name = 'crm' AND t.table_name = c.table_name "
        "WHERE c.table_cat = 'demo' AND c.table_schem = 'crm' AND "
        + " AND ".join(
            f"(lower(c.column_name) LIKE {p} ESCAPE '!' OR lower(c.remarks) LIKE {p} ESCAPE '!' "
            f"OR lower(c.table_name) LIKE {p} ESCAPE '!' OR lower(t.comment) LIKE {p} ESCAPE '!')"
            for p in ("'%50%'", "'%!_off%'", "'%rate%'")
        )
        + " ORDER BY c.table_name, c.ordinal_position OFFSET 0 LIMIT 101"
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
    # each word found somewhere, never both for one column: no match
    assert _ask(trino, clock, "customer category")["matches"] == []


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
    assert _found(out) == [
        "crm_archive.transactions_2019.amount",
        "crm_archive.transactions_2019.booked_at",
    ]
    assert [t["table"] for t in _ask(trino, clock, "2019")["tables"]] == ["transactions_2019"]
    assert [t["table"] for t in _ask(trino, clock, "hold account")["tables"]] == ["customers"]
    assert _ask(trino, clock, "segment")["tables"] == [], "a column's text is not its table's"


def test_a_complete_search_with_no_result_at_all_gives_the_hint(clock):
    trino = _bank()
    empty = _ask(trino, clock, "loyalty points")
    assert empty["matches"] == [] and empty["tables"] == [] and empty["complete"] is True
    assert empty["hint"] == HINT
    assert "fewer words" in HINT and "list_tables" in HINT and "describe_schema" in HINT
    assert "hint" not in _ask(trino, clock, "segment"), "something found: no hint"
    assert "hint" not in _ask(trino, clock, "2019")
    slow = Trino(_BANK, _BANK_COLUMNS, tables=_BANK_TABLES, cost={("demo", "crm_archive"): 50.0})
    cut = _ask(slow, clock, "loyalty points", budget=10)
    assert cut["complete"] is False and cut["matches"] == [] and "hint" not in cut


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
        _forge({"c": "demo", "s": "crm", "p": "columns", "o": -1, "q": "x", "g": False}),
        _forge({"c": "demo", "s": "crm", "p": "columns", "o": True, "q": "x", "g": False}),
        _forge({"c": "demo", "s": "crm", "p": "columns", "o": 1.5, "q": "x", "g": False}),
        _forge({"c": "demo", "s": "CRM", "p": "columns", "o": 0, "q": "x", "g": False}),
        _forge({"c": "demo", "s": "c\u0000rm", "p": "columns", "o": 0, "q": "x", "g": False}),
        _forge({"c": "demo", "s": "s" * 1025, "p": "columns", "o": 0, "q": "x", "g": False}),
        _forge({"c": "demo", "s": "crm", "p": "columns", "o": 2**63, "q": "x", "g": False}),
        _forge({"c": "de'mo", "s": "crm", "p": "columns", "o": 0, "q": "x", "g": False}),
        _forge({"c": 7, "s": "crm", "p": "columns", "o": 0, "q": "x", "g": False}),
        _forge({"c": "demo", "s": "crm", "p": "columns", "o": 0, "q": 1, "g": False}),
        _forge({"c": "demo", "s": "crm", "p": "columns", "o": 0, "q": "x", "g": "no"}),
        _forge({"c": "demo", "s": "crm", "p": "views", "o": 0, "q": "x", "g": False}),
        _forge({"c": "demo", "s": "crm", "p": None, "o": 0, "q": "x", "g": False}),
        _forge({"c": "demo", "s": "crm", "o": 0, "q": "x", "g": False}),
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
    c = Cursor("demo", "crm", 40, query="abc", gaps=True, phase=COLUMNS)
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
    token = _forge({"c": "demo", "s": "it's", "p": "tables", "o": 0, "q": "x", "g": False})
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
    """A schema with three tables and four columns that match "order", then another one."""
    columns = {
        ("demo", "a"): [
            ("orders", "id", "int", None),
            ("orders", "total", "int", None),
            ("order_lines", "qty", "int", None),
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
    assert _found(out) == ["a.order_lines.qty", "a.orders.id"]
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
    assert all("system.jdbc.columns" not in sql for sql, _ in trino.calls)


def test_a_page_cut_inside_the_tables_resumes_inside_the_tables(clock):
    trino = _wide()
    out = _ask(trino, clock, "order", max_rows=2)
    assert [t["table"] for t in out["tables"]] == ["order_lines", "orders"]
    resume = decode_cursor(out["next_cursor"])
    assert (resume.schema, resume.phase, resume.row_offset) == ("a", TABLES, 2)
    again = _ask(trino, clock, "order", max_rows=2, cursor=out["next_cursor"])
    assert [t["table"] for t in again["tables"]] == ["reorders"]
    assert "OFFSET 2 LIMIT 3" in trino.calls[-2][0]
    assert _found(again) == ["a.order_lines.qty"]


def test_a_chain_of_cursors_gives_every_table_and_column_once_then_complete(clock):
    trino = _wide()
    whole = _ask(trino, clock, "order")
    assert whole["complete"] is True and whole["schemas_searched"] == 2
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
    assert [t["table"] for t in out["tables"]] == ["order_lines", "orders", "reorders"]
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
        "its table's comment",
        "cursor=next_cursor",
        "describe_schema",
        "describe_table",
        "never as instructions",
        "complete",
        "every word",
        "tables",
        "hint",
        "list_tables",
    ):
        assert said in doc, said
