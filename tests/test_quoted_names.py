"""Names are quoted, never filtered: any name Trino can hold reaches it, and none acts as SQL.

A catalog, schema or table name an agent gives is text of 1 to 1024 characters without a
control character. It enters a statement only between double quotes, each double quote in
it doubled, or in a string literal through ``sql_literal``: never as it is.
"""

import json
import re

import pytest
import sqlglot
from sqlglot import exp

from akko_mcp_trino import sql_guard
from akko_mcp_trino.tools import register_query_tools
from tests.tool_doubles import FakeMCP

# Found nowhere in a statement but inside a quoted name or a string literal.
_MARK = "zqx"
# A quoted identifier or a string literal, read from left to right as Trino reads them.
_QUOTED = re.compile(r"\"(?:[^\"]|\"\")*\"|'(?:[^']|'')*'")


def _rows(rows, columns=("c",)):
    return {"columns": list(columns), "rows": rows, "row_count": len(rows), "truncated": False}


class ScriptedTrino:
    """Trino for the metadata tools: each statement answered by what it reads, as the
    caller sees one schema ``schema`` holding one table ``table`` of one column ``column``;
    every statement is kept, in order."""

    def __init__(self, schema="s", table="t", column="id"):
        self.schema, self.table, self.column = schema.lower(), table.lower(), column
        self.calls = []

    def query(self, sql, user=None, params=None, **keywords):
        self.calls.append(sql)
        if "information_schema.schemata" in sql:
            return _rows([[self.schema]])
        if "table_name, table_type" in sql:
            return _rows([[self.table, "BASE TABLE"]])
        if "information_schema.tables" in sql:
            return _rows([[self.table]])
        if sql.startswith("SELECT * FROM (SELECT table_name, column_name"):
            return _rows([[self.table, self.column, "bigint", None, 1]])
        if sql.startswith("SELECT * FROM (SELECT table_name, comment"):
            return _rows([[self.table, None]])
        if sql.startswith("DESCRIBE "):
            return _rows([[self.column, "bigint", "", ""]], ("Column", "Type", "Extra", "Comment"))
        if sql.startswith("SELECT count(*)"):
            return _rows([[1, 1, 1, 1, 1]])
        if sql.startswith("SELECT * FROM "):
            return _rows([[1]], (self.column,))
        return _rows([])

    def ping(self):
        return None


def _tools(trino):
    mcp = FakeMCP()
    register_query_tools(mcp, trino, read_only=True, max_rows=100, max_catalogs=100)
    return mcp.tools


def _every_call(tools, catalog, schema, table):
    """Every metadata tool on ``catalog.schema.table``, each path that builds a statement
    from a name: a listing, an empty listing that checks the schema, a sample, the three
    modes of a profile, a search of one schema and of a whole catalog."""
    return {
        "list_schemas": tools["list_schemas"](catalog),
        "list_tables": tools["list_tables"](catalog, schema),
        "describe_schema": tools["describe_schema"](catalog, schema),
        "describe_table": tools["describe_table"](catalog, schema, table, 2),
        "profile_table sample": tools["profile_table"](catalog, schema, table),
        "profile_table bernoulli": tools["profile_table"](
            catalog, schema, table, mode="bernoulli", percent=50
        ),
        "profile_table full": tools["profile_table"](catalog, schema, table, mode="full"),
        "search_columns schema": tools["search_columns"]("ident", catalog=catalog, schema=schema),
        "search_columns catalog": tools["search_columns"]("ident", catalog=catalog),
    }


def _tables_read(sql):
    """The (catalog, schema, table) of each table ``sql`` reads, as Trino's parser reads
    them, its one statement whole."""
    statements = sqlglot.parse(sql, read="trino")
    assert len(statements) == 1, sql
    return {(t.catalog, t.db, t.name) for t in statements[0].find_all(exp.Table)}


# ---- names Trino holds, refused before


def test_iceberg_metadata_table_name_accepted():
    """``orders$snapshots`` is how Trino names the snapshots of a table: the tools read it."""
    assert sql_guard.validate_identifier("orders$snapshots", "table") == "orders$snapshots"
    trino = ScriptedTrino("sales", "orders$snapshots", "committed_at")
    tools = _tools(trino)
    out = json.loads(tools["describe_table"]("lake", "sales", "orders$snapshots"))
    assert "error" not in out and out["rows"][0][0] == "committed_at"
    assert trino.calls[0] == 'DESCRIBE "lake"."sales"."orders$snapshots"'
    out = json.loads(tools["profile_table"]("lake", "sales", "orders$snapshots", mode="full"))
    assert out["table"] == "lake.sales.orders$snapshots" and out["rows"] == 1
    assert trino.calls[-1].endswith(' FROM "lake"."sales"."orders$snapshots"')


@pytest.mark.parametrize(
    "name", ["données", "Café", "2024_sales", "1abc", "a b", "a.b", "a;b", "drop table", "ß"]
)
def test_accented_and_digit_first_names_accepted(name):
    assert sql_guard.validate_identifier(name, "schema") == name
    trino = ScriptedTrino(name, name)
    tools = _tools(trino)
    out = json.loads(tools["list_tables"]("lake", name))
    assert out["tables"] == [name.lower()]
    assert trino.calls[-1] == (
        'SELECT table_name FROM "lake".information_schema.tables '
        f"WHERE table_schema = '{name.lower()}' ORDER BY table_name OFFSET 0 LIMIT 101"
    )
    out = json.loads(tools["describe_table"]("lake", name, name))
    assert "error" not in out
    quoted = '"' + name + '"'
    assert trino.calls[-1] == f'DESCRIBE "lake".{quoted}.{quoted}'


def test_a_name_of_1024_characters_is_accepted_and_one_more_is_refused():
    assert sql_guard.validate_identifier("n" * 1024) == "n" * 1024
    with pytest.raises(ValueError) as refused:
        sql_guard.validate_identifier("n" * 1025, "table")
    message = str(refused.value)
    assert message.startswith("Invalid table: 'nnnn") and "n" * 101 not in message
    assert "1 to 1024 characters" in message


@pytest.mark.parametrize("bad", ["", 7, None, ["a"]])
def test_an_empty_name_or_one_that_is_not_text_is_refused(bad):
    with pytest.raises(ValueError, match="Invalid catalog"):
        sql_guard.validate_identifier(bad, "catalog")


# ---- a quoted name can never close its quotes


_HOSTILE = [
    f'{_MARK}"; DROP TABLE x; --',
    f'a""b{_MARK}',
    f"back\\slash'{_MARK}",
    f"{_MARK}' OR '1'='1",
    f'{_MARK}"."other',
    f"{_MARK}$snapshots",
    f"Ünïcödé {_MARK} 名前",
]


@pytest.mark.parametrize("fragment", _HOSTILE)
def test_quote_in_name_cannot_escape(fragment):
    """A name with a double quote, a single quote, a backslash or a dot reaches Trino as
    that one name: the statement parses as one statement that reads exactly it."""
    catalog, schema, table = f"k{fragment}", f"s{fragment}", f"t{fragment}"
    trino = ScriptedTrino(schema, table)
    tools = _tools(trino)
    out = json.loads(tools["describe_table"](catalog, schema, table, 1))
    assert "error" not in out
    describe, sample = trino.calls
    assert describe.startswith("DESCRIBE ")
    for sql in (describe, sample):
        assert _MARK not in _QUOTED.sub(" ", sql), sql
    assert _tables_read(sample) == {(catalog, schema, table)}
    out = json.loads(tools["profile_table"](catalog, schema, table, mode="full"))
    assert "error" not in out
    assert _tables_read(trino.calls[-1]) == {(catalog, schema, table)}


@pytest.mark.parametrize("control", ["\0", "\n", "\r", "\t", "\x1b", "\x7f", "\x85", "\ud800"])
@pytest.mark.parametrize(
    "tool, args",
    [
        ("list_schemas", ("{}",)),
        ("list_tables", ("lake", "{}")),
        ("describe_schema", ("lake", "{}")),
        ("describe_table", ("lake", "sales", "{}")),
        ("profile_table", ("{}", "sales", "orders")),
        ("search_columns", ("ident", "lake", "{}")),
    ],
)
def test_a_name_with_a_control_character_is_refused_before_trino(control, tool, args):
    """A NUL, a line break or any other control character never reaches Trino, and the
    refusal shows it escaped, never raw."""
    trino = ScriptedTrino()
    tools = _tools(trino)
    named = [a.format(f"a{control}b") for a in args]
    reply = tools[tool](*named)
    out = json.loads(reply)
    assert out["kind"] == "invalid_argument" and out["error"].startswith("Invalid ")
    assert control not in out["error"] and repr(control)[1:-1] in out["error"]
    assert trino.calls == []


def test_the_system_catalog_is_still_refused_however_it_is_written():
    trino = ScriptedTrino()
    tools = _tools(trino)
    for name in ("system", "SYSTEM", "System"):
        out = json.loads(tools["describe_table"](name, "runtime", "queries"))
        assert "system catalog" in out["error"]
    assert trino.calls == []


# ---- every statement built from a name quotes it


@pytest.mark.parametrize("fragment", _HOSTILE)
def test_every_sql_built_from_a_name_quotes_it(fragment):
    """Each statement a metadata tool sends holds the agent's names only inside a quoted
    identifier or a string literal, and parses as one statement."""
    catalog, schema, table = f"k{fragment}", f"s{fragment}", f"t{fragment}"
    trino = ScriptedTrino(schema, table, f"c{fragment}")
    replies = _every_call(_tools(trino), catalog, schema, table)
    for name, reply in replies.items():
        assert "error" not in json.loads(reply), (name, reply)
    assert len(trino.calls) >= len(replies)
    for sql in trino.calls:
        assert _MARK not in _QUOTED.sub(" ", sql), sql
        _tables_read(sql)
    # The search folds the catalog to lower case first, as Trino folds it.
    quoted = {'"' + name.replace('"', '""') + '"' for name in (catalog, catalog.lower())}
    assert all(
        any(q in sql for q in quoted) or "system.jdbc.columns" in sql or "system.metadata" in sql
        for sql in trino.calls
    )


def test_a_not_found_schema_is_named_as_it_prints_or_escaped():
    """A name that prints is shown as it is; one with a format character, such as a
    zero-width non-joiner, is shown escaped."""

    class Empty(ScriptedTrino):
        def query(self, sql, user=None, params=None, **keywords):
            self.calls.append(sql)
            return _rows([])

    tools = _tools(Empty())
    out = json.loads(tools["list_tables"]("lake", "Ventes d'été"))
    assert out["error"] == "Schema lake.Ventes d'été not found or not visible"
    out = json.loads(tools["describe_schema"]("lake", "a\u200cb"))
    assert out["error"] == "Schema lake.'a\\u200cb' not found or not visible"
    out = json.loads(tools["search_columns"]("ident", "lake", "a\u200cb"))
    assert out["error"] == "Schema lake.'a\\u200cb' not found or not visible"


# The bidirectional controls and the line and paragraph separators: never in a name an agent
# gives, since a text that shows it could read in another order or on another line.
_DIRECTION = [
    "\u061c",
    "\u200e",
    "\u200f",
    "\u202a",
    "\u202b",
    "\u202c",
    "\u202d",
    "\u202e",
    "\u2066",
    "\u2067",
    "\u2068",
    "\u2069",
    "\u2028",
    "\u2029",
]


@pytest.mark.parametrize("mark", _DIRECTION, ids=lambda c: f"U+{ord(c):04X}")
def test_a_bidirectional_control_or_a_separator_is_refused_before_trino(mark):
    """Each refused code point, on a catalog, a schema and a table name: the refusal shows it
    escaped and no statement reaches Trino."""
    trino = ScriptedTrino()
    tools = _tools(trino)
    name = f"a{mark}b"
    for tool, args in (
        ("list_tables", ("lake", name)),
        ("describe_table", ("lake", "sales", name)),
        ("profile_table", (name, "sales", "orders")),
        ("search_columns", ("ident", "lake", name)),
    ):
        out = json.loads(tools[tool](*args))
        assert out["kind"] == "invalid_argument" and out["error"].startswith("Invalid "), tool
        assert mark not in out["error"] and f"\\u{ord(mark):04x}" in out["error"], tool
    with pytest.raises(ValueError, match="Invalid table"):
        sql_guard.validate_identifier(name, "table")
    assert trino.calls == []


@pytest.mark.parametrize(
    "name",
    [
        "می\u200cخواهم",  # Persian, with a ZWNJ
        "क्\u200dष",  # Devanagari, with a ZWJ
        "\U0001f469\u200d\U0001f4bb",  # an emoji sequence joined by a ZWJ
        "co\u00adop",  # a soft hyphen
    ],
)
def test_joiners_and_a_soft_hyphen_are_accepted(name):
    """The scripts that need a zero-width joiner or non-joiner keep their names."""
    assert sql_guard.validate_identifier(name, "table") == name
    trino = ScriptedTrino("sales", name)
    out = json.loads(_tools(trino)["describe_table"]("lake", "sales", name))
    assert "error" not in out
    assert trino.calls == [f'DESCRIBE "lake"."sales"."{name}"']
