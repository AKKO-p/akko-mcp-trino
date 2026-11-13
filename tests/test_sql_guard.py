"""akko_mcp_trino.sql_guard: literal escaping, identifier validation, read-only classification."""

import pytest

from akko_mcp_trino import sql_guard


def test_safe_sql_string_doubles_quotes_drops_nuls_and_keeps_backslashes():
    """Trino reads no backslash escape in a string literal: a backslash is itself."""
    assert sql_guard.safe_sql_string("O'Brien") == "O''Brien"
    assert sql_guard.safe_sql_string("a\\b") == "a\\b"
    assert sql_guard.safe_sql_string("x\0y") == "xy"
    # quote and backslash combined
    assert sql_guard.safe_sql_string("a\\'b") == "a\\''b"


def test_sql_literal_keeps_backslashes_and_doubles_quotes():
    assert sql_guard.sql_literal("a\\b'c") == "'a\\b''c'"
    assert sql_guard.sql_literal("x\0y") == "'xy'"
    assert sql_guard.sql_literal("") == "''"


def test_the_metadata_views_are_trinos_own_names():
    assert sql_guard.JDBC_COLUMNS == "system.jdbc.columns"
    assert sql_guard.TABLE_COMMENTS == "system.metadata.table_comments"


@pytest.mark.parametrize("name", ["iceberg", "fraud_scores", "a-b", "_x", "A1"])
def test_validate_identifier_accepts_valid(name):
    assert sql_guard.validate_identifier(name) == name


@pytest.mark.parametrize("bad", ["", "1abc", "a.b", "a b", "a;b", "a'b", "drop table"])
def test_validate_identifier_rejects_invalid(bad):
    with pytest.raises(ValueError):
        sql_guard.validate_identifier(bad, "catalog")


def test_governed_catalog_refuses_system_whatever_its_case():
    assert sql_guard.governed_catalog("iceberg") == "iceberg"
    for name in ("system", "SYSTEM", "System"):
        with pytest.raises(ValueError, match="system catalog"):
            sql_guard.governed_catalog(name)
    with pytest.raises(ValueError, match="Invalid catalog"):
        sql_guard.governed_catalog("a;b")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "select * from t",
        "  SHOW CATALOGS",
        "DESCRIBE t",
        "DESC t",
        "WITH x AS (SELECT 1) SELECT * FROM x",
        "SELECT 1 UNION SELECT 2",
        "SHOW SCHEMAS FROM iceberg",
    ],
)
def test_is_read_only_sql_allows_reads(sql):
    assert sql_guard.is_read_only_sql(sql) is True


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO t VALUES (1)",
        "CREATE TABLE t (x int)",
        "DROP TABLE t",
        "UPDATE t SET x=1",
        "DELETE FROM t",
        "ALTER TABLE t ADD COLUMN y int",
        "MERGE INTO t USING s ON t.id=s.id WHEN MATCHED THEN UPDATE SET x=1",
    ],
)
def test_is_read_only_sql_blocks_writes(sql):
    assert sql_guard.is_read_only_sql(sql) is False


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1; DROP TABLE t",  # stacked multi-statement
        "GRANT SELECT ON t TO u",  # side-effecting command
        "WITH x AS (SELECT 1) INSERT INTO t SELECT * FROM x",  # a WITH that wraps a write
        "",  # vide
        "   ",  # blanc
        "this is not sql at all ((",  # non analysable → fail-closed
    ],
)
def test_is_read_only_sql_blocks_edge_cases_the_prefix_filter_missed(sql):
    assert sql_guard.is_read_only_sql(sql) is False


@pytest.mark.parametrize(
    "sql",
    [
        "WITH w AS (DELETE FROM t) SELECT 1",
        "WITH w AS (INSERT INTO t VALUES (1)) SELECT * FROM w",
        "SELECT * FROM (DROP TABLE t) x",
        "WITH a AS (SELECT 1), b AS (UPDATE t SET x = 1) SELECT 1",
    ],
)
def test_write_nested_anywhere_in_the_tree_is_refused(sql):
    """Found by the live adversarial proof on 12 September 2026: the guard only
    looked at the root node, so a write wrapped in a CTE reached Trino. Trino
    happened to refuse it as a syntax error; the guard must not rely on that."""
    from akko_mcp_trino.sql_guard import is_read_only_sql

    assert is_read_only_sql(sql) is False


@pytest.mark.parametrize(
    "sql",
    [
        "EXPLAIN ANALYZE INSERT INTO t VALUES (1)",
        "EXPLAIN ANALYZE DELETE FROM t",
        "EXPLAIN ANALYZE VERBOSE UPDATE t SET x = 1",
        "explain analyze select 1",  # EXPLAIN ANALYZE *runs* the statement, whatever it is
        "EXPLAIN INSERT INTO t VALUES (1)",  # plans a write; nothing to gain, refuse
        "EXPLAIN (TYPE DISTRIBUTED) DELETE FROM t",
    ],
)
def test_explain_of_a_write_or_explain_analyze_is_refused(sql):
    """Found on 13 September 2026 while reading another server's guard: sqlglot
    parses EXPLAIN as an opaque Command, and Trino's EXPLAIN ANALYZE executes
    the statement it explains. `EXPLAIN ANALYZE INSERT …` was a write that passed."""
    from akko_mcp_trino.sql_guard import is_read_only_sql

    assert is_read_only_sql(sql) is False


@pytest.mark.parametrize(
    "sql",
    [
        "EXPLAIN SELECT 1",
        "EXPLAIN (TYPE DISTRIBUTED, FORMAT TEXT) SELECT * FROM t WHERE x = 1",
        "EXPLAIN VERBOSE WITH w AS (SELECT 1) SELECT * FROM w",
        "explain\n  select 1",
    ],
)
def test_explain_of_a_read_is_allowed_once_opened(sql):
    """EXPLAIN is refused by default, allowed for a read once the administrator opens it."""
    from akko_mcp_trino.sql_guard import is_read_only_sql

    assert is_read_only_sql(sql) is False
    assert is_read_only_sql(sql, extra_reads=frozenset({"EXPLAIN"})) is True


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM TABLE(core_postgres.system.query(query => 'select iban from clients'))",
        "SELECT * FROM TABLE(core_postgres.system.query(query => 'DELETE FROM x RETURNING *'))",
        "SELECT c.* FROM iceberg.s.t JOIN TABLE(pg.system.query(query => 'select 1')) c ON true",
        "WITH w AS (SELECT * FROM TABLE(pg.system.query(query => 'select 1'))) SELECT * FROM w",
        "SELECT * FROM TABLE(iceberg.system.table_changes('s', 't', 0, 1))",
        "SELECT * FROM TABLE(sequence(1, 3))",
        "EXPLAIN SELECT * FROM TABLE(pg.system.query(query => 'select 1'))",
    ],
)
def test_table_functions_are_refused(sql):
    """Found on 23 September 2026, proven on the live on 24 September: a
    connector table function such as `system.query` sends raw SQL to the source
    under the connector's own account, so Trino applies neither row filters nor
    column masks. The engine policy refuses it too; the guard refuses every
    table function, fail closed."""
    from akko_mcp_trino.sql_guard import is_read_only_sql

    assert is_read_only_sql(sql) is False


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM system.runtime.queries",
        "SELECT query FROM SYSTEM.runtime.queries",
        'SELECT * FROM "system".jdbc.tables',
    ],
)
def test_system_catalog_is_refused(sql):
    """`system.runtime.queries` shows other users' SQL text."""
    from akko_mcp_trino.sql_guard import is_read_only_sql

    assert is_read_only_sql(sql) is False


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM runtime.queries",  # resolved in the session catalog
        "SELECT query FROM queries",
        "DESCRIBE runtime.queries",
        "EXPLAIN SELECT * FROM runtime.queries",
        "SHOW TABLES",
        "SHOW SCHEMAS",
        "SHOW COLUMNS FROM runtime.queries",
        "SHOW TABLES FROM 'iceberg'",  # not a name: nothing qualifies it
        "SHOW FUNCTIONS",
    ],
)
def test_a_name_without_catalog_is_refused_when_the_session_catalog_is_system(sql):
    """Trino resolves a name without catalog in the session catalog, `system` by default:
    `runtime.queries` there is `system.runtime.queries`, other users' SQL."""
    from akko_mcp_trino.sql_guard import is_read_only_sql

    assert is_read_only_sql(sql, session_catalog="system") is False
    assert is_read_only_sql(sql, session_catalog="SYSTEM") is False


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM runtime.queries",
        "SHOW TABLES",
        "SHOW COLUMNS FROM sales.orders",
    ],
)
def test_a_name_without_catalog_is_read_in_a_data_session_catalog(sql):
    from akko_mcp_trino.sql_guard import is_read_only_sql

    assert is_read_only_sql(sql, session_catalog="iceberg") is True


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "WITH q AS (SELECT 1 AS n) SELECT * FROM q",  # a CTE is not a table of the session catalog
        "SELECT * FROM iceberg.sales.orders",
        "SHOW CATALOGS",
        "SHOW SCHEMAS FROM iceberg",
        "SHOW TABLES FROM iceberg.sales",
    ],
)
def test_qualified_reads_pass_whatever_the_session_catalog(sql):
    from akko_mcp_trino.sql_guard import is_read_only_sql

    assert is_read_only_sql(sql, session_catalog="system") is True


@pytest.mark.parametrize(
    "sql",
    [
        "SHOW TABLES FROM system.runtime",
        "SHOW SCHEMAS FROM system",
        'SHOW SCHEMAS FROM "system"',
        "SHOW COLUMNS FROM system.runtime.queries",
        "SHOW STATS FOR system.runtime.queries",
        "SHOW STATS FOR (SELECT * FROM system.runtime.queries)",
        "SHOW CREATE VIEW system.metadata.x",
    ],
)
def test_a_show_that_names_the_system_catalog_is_refused(sql):
    """SHOW is not structured by the parser: a `system` anywhere in its names refuses it."""
    from akko_mcp_trino.sql_guard import is_read_only_sql

    assert is_read_only_sql(sql) is False
    assert is_read_only_sql(sql, session_catalog="iceberg") is False


# ---- the tables a statement names, for the audit record

from akko_mcp_trino.sql_guard import referenced_tables  # noqa: E402


def test_referenced_tables_are_the_names_as_written_in_lower_case_once_each_sorted():
    sql = (
        "WITH recent AS (SELECT * FROM Lake.Retail.Ventes WHERE d > DATE '2024-01-01') "
        "SELECT r.id, p.libelle FROM recent r JOIN lake.ref.pays p ON r.pays = p.code "
        "WHERE r.id IN (SELECT id FROM lake.retail.ventes) AND r.x IN (SELECT x FROM ref.t)"
    )
    assert referenced_tables(sql) == ["lake.ref.pays", "lake.retail.ventes", "ref.t"]


def test_referenced_tables_see_through_describe_and_explain():
    assert referenced_tables("DESCRIBE lake.ref.pays") == ["lake.ref.pays"]
    assert referenced_tables("EXPLAIN (TYPE VALIDATE) SELECT * FROM lake.ref.pays") == [
        "lake.ref.pays"
    ]


def test_referenced_tables_of_what_names_no_table_or_does_not_parse_is_empty():
    assert referenced_tables("SHOW CATALOGS") == []
    assert referenced_tables("SELECT 1") == []
    assert referenced_tables("SELECT FROM WHERE (((") == []
    assert referenced_tables("EXPLAIN ANALYZE") == []
    assert referenced_tables("") == []


def test_a_table_function_is_not_a_table_name():
    assert referenced_tables("SELECT * FROM TABLE(c.system.query(query => 'x'))") == []


def test_parse_error_says_why_a_statement_does_not_parse():
    from akko_mcp_trino.sql_guard import parse_error

    assert parse_error("SELECT 1") is None
    assert parse_error("SELECT FROM WHERE (((")


def test_other_commands_keep_their_verdict():
    """CALL runs a procedure: refused."""
    from akko_mcp_trino.sql_guard import is_read_only_sql

    assert is_read_only_sql("CALL iceberg.system.rollback_to_snapshot('s', 't', 1)") is False


@pytest.mark.parametrize(
    ("sql", "opened_by"),
    [
        ("SHOW STATS FOR demo.crm.orders", "SHOW STATS"),
        ("SHOW STATS FOR (SELECT * FROM demo.crm.orders)", "SHOW STATS"),
        ("show stats for demo.crm.orders", "SHOW STATS"),
        ("SHOW CREATE VIEW demo.crm.v", "SHOW CREATE"),
        ("SHOW CREATE TABLE demo.crm.orders", "SHOW CREATE"),
        ("SHOW CREATE SCHEMA demo.crm", "SHOW CREATE"),
        ("SHOW GRANTS ON TABLE demo.crm.orders", "SHOW GRANTS"),
        ("SHOW ROLES FROM demo", "SHOW ROLES"),
        ("SHOW CURRENT ROLES FROM demo", "SHOW ROLES"),
        ("SHOW ROLE GRANTS FROM demo", "SHOW ROLES"),
        ("SHOW SESSION", "SHOW SESSION"),
        ("EXPLAIN SELECT * FROM demo.crm.orders", "EXPLAIN"),
        ("EXPLAIN (TYPE IO) SELECT * FROM demo.crm.orders", "EXPLAIN"),
        ("EXPLAIN (TYPE VALIDATE) SELECT 1", "EXPLAIN"),
    ],
)
def test_reads_past_the_access_rules_are_refused_until_opened(sql, opened_by):
    """The allow-list (0.7.1): reads that can show facts beyond a row filter or a column
    mask are refused by default, each opened only by its own name."""
    from akko_mcp_trino.sql_guard import EXTRA_READ_COMMANDS, is_read_only_sql

    assert is_read_only_sql(sql, session_catalog="demo") is False
    others = frozenset(EXTRA_READ_COMMANDS) - {opened_by}
    assert is_read_only_sql(sql, session_catalog="demo", extra_reads=others) is False
    assert is_read_only_sql(sql, session_catalog="demo", extra_reads=frozenset({opened_by}))


@pytest.mark.parametrize(
    "sql",
    [
        "SHOW BRANCHES FROM TABLE demo.crm.orders",  # a kind no allow-list names
        "SHOW SOMETHING",
        "DESCRIBE INPUT q",
        "DESCRIBE OUTPUT q",
        "DESC OUTPUT q",
        "PREPARE q FROM SELECT 1",
        "EXECUTE q",
        "DESCRIBE",
    ],
)
def test_commands_outside_the_allow_list_are_refused_even_with_every_extra(sql):
    from akko_mcp_trino.sql_guard import EXTRA_READ_COMMANDS, is_read_only_sql

    every = frozenset(EXTRA_READ_COMMANDS)
    assert is_read_only_sql(sql, session_catalog="demo") is False
    assert is_read_only_sql(sql, session_catalog="demo", extra_reads=every) is False


@pytest.mark.parametrize(
    "sql",
    [
        "SHOW CATALOGS",
        "SHOW SCHEMAS FROM demo",
        "SHOW TABLES FROM demo.crm",
        "SHOW COLUMNS FROM demo.crm.orders",
        "SHOW FUNCTIONS",
        "DESCRIBE demo.crm.orders",
        "DESC demo.crm.orders",
        "SELECT count(*) FROM demo.crm.orders",
    ],
)
def test_the_allow_list_keeps_what_an_agent_needs(sql):
    from akko_mcp_trino.sql_guard import is_read_only_sql

    assert is_read_only_sql(sql, session_catalog="demo") is True


def test_an_opened_show_still_refuses_the_system_catalog():
    from akko_mcp_trino.sql_guard import is_read_only_sql

    opened = frozenset({"SHOW STATS", "EXPLAIN"})
    assert is_read_only_sql("SHOW STATS FOR system.runtime.queries", extra_reads=opened) is False
    assert (
        is_read_only_sql("EXPLAIN SELECT * FROM system.runtime.queries", extra_reads=opened)
        is False
    )
    assert is_read_only_sql("EXPLAIN ANALYZE SELECT 1", extra_reads=opened) is False


def test_parse_extra_reads_folds_case_and_spaces_and_refuses_an_unknown_name():
    from akko_mcp_trino.sql_guard import parse_extra_reads

    assert parse_extra_reads("") == frozenset()
    assert parse_extra_reads(" , ") == frozenset()
    assert parse_extra_reads("show  stats, explain") == {"SHOW STATS", "EXPLAIN"}
    with pytest.raises(ValueError, match="unknown command 'SHOW STAT'"):
        parse_extra_reads("SHOW STAT")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1 EXCEPT SELECT 2",
        "SELECT 1 INTERSECT SELECT 2",
        "VALUES (1), (2)",
        "(SELECT 1)",
        "-- a comment first\nSELECT 1",
        "/* a comment */ SHOW TABLES FROM demo.crm",
        "-- a comment\nDESCRIBE demo.crm.orders",
        'DESCRIBE "demo"."crm"."orders"',
        "DESCRIBE crm.orders",
    ],
)
def test_reads_an_agent_writes_pass(sql):
    """The review of 0.7.1: set operations, VALUES, a parenthesized read and a comment
    before the statement are reads too."""
    from akko_mcp_trino.sql_guard import is_read_only_sql

    assert is_read_only_sql(sql, session_catalog="demo") is True


@pytest.mark.parametrize(
    "sql",
    [
        "PRAGMA x",
        "DESCRIBE FORMATTED demo.crm.orders",
        "DESCRIBE HISTORY demo.crm.orders",
        "DESCRIBE SELECT 1",
        "describe input q",
        "-- c\nDESCRIBE OUTPUT q",
        "DESCRIBE\tINPUT q",
        "DESCRIBE a.b.c.d",
        "DESCRIBE 'demo'",
        "DESCRIBE OUTPUT q x",
        "DESCRIBE system.runtime.queries",
        'DESCRIBE "system".runtime.queries',
        "SHOW /* c */ STATS FOR demo.crm.orders",
        "-- c\nSHOW STATS FOR demo.crm.orders",
        "/* c */ EXPLAIN SELECT 1",
        "SHOW",
        "SELECT 1 EXCEPT SELECT * FROM system.runtime.queries",
        "VALUES (1) UNION SELECT * FROM TABLE(pg.system.query(query => 'select 1'))",
    ],
)
def test_other_forms_stay_refused(sql):
    from akko_mcp_trino.sql_guard import is_read_only_sql

    assert is_read_only_sql(sql, session_catalog="demo") is False


def test_describe_of_a_short_name_is_refused_when_the_session_catalog_is_system():
    from akko_mcp_trino.sql_guard import is_read_only_sql

    assert is_read_only_sql("DESCRIBE runtime.queries", session_catalog="system") is False
    assert is_read_only_sql("DESCRIBE demo.crm.orders", session_catalog="system") is True


# ---- bounds checked before parsing, and one parse ----

# TPC-H query 20, with its nested IN and scalar subqueries.
_TPCH_Q20 = """
SELECT s_name, s_address
FROM tpch.tiny.supplier, tpch.tiny.nation
WHERE s_suppkey IN (
    SELECT ps_suppkey FROM tpch.tiny.partsupp
    WHERE ps_partkey IN (SELECT p_partkey FROM tpch.tiny.part WHERE p_name LIKE 'forest%')
      AND ps_availqty > (
        SELECT 0.5 * sum(l_quantity) FROM tpch.tiny.lineitem
        WHERE l_partkey = ps_partkey AND l_suppkey = ps_suppkey
          AND l_shipdate >= date '1994-01-01'
          AND l_shipdate < date '1994-01-01' + interval '1' year
      )
)
  AND s_nationkey = n_nationkey AND n_name = 'CANADA'
ORDER BY s_name
"""

# A report six subqueries deep, with functions, CASE, CAST and a window nested inside.
_REPORT_6_LEVELS = """
SELECT region, round(sum(coalesce(net, 0)), 2) AS total
FROM (
  SELECT region, net, rank() OVER (PARTITION BY region ORDER BY net DESC) AS r
  FROM (
    SELECT region, CASE WHEN status = 'F' THEN cast(amount AS decimal(18, 2)) ELSE 0 END AS net
    FROM (
      SELECT r_name AS region, status, amount
      FROM (
        SELECT o_custkey, o_orderstatus AS status, o_totalprice AS amount
        FROM (
          SELECT * FROM tpch.tiny.orders
          WHERE o_custkey IN (
            SELECT c_custkey FROM tpch.tiny.customer
            WHERE c_acctbal > (SELECT avg(coalesce(c_acctbal, 0)) FROM tpch.tiny.customer)
          )
        ) l6
      ) l5
      JOIN tpch.tiny.customer ON c_custkey = o_custkey
      JOIN tpch.tiny.nation ON n_nationkey = c_nationkey
      JOIN tpch.tiny.region ON r_regionkey = n_regionkey
    ) l4
  ) l3
) l2
WHERE r <= 10
GROUP BY region
"""


def test_too_long_sql_refused():
    sql_guard.check_sql_bounds("x" * 1000, max_chars=1000, max_nesting=8)  # at the bound: read
    with pytest.raises(ValueError, match="1001 characters long; this server reads 1000 at most"):
        sql_guard.check_sql_bounds("x" * 1001, max_chars=1000, max_nesting=8)
    with pytest.raises(sql_guard.SQLBoundError) as refused:
        sql_guard.check_sql_bounds("x" * 1001, max_chars=1000, max_nesting=8)
    assert refused.value.code == "sql_too_long"


def test_nesting_past_the_bound_is_refused_with_its_code():
    sql_guard.check_sql_bounds("SELECT " + "(" * 8 + "1" + ")" * 8, max_chars=1000, max_nesting=8)
    with pytest.raises(sql_guard.SQLBoundError, match="more than 8 deep") as refused:
        sql_guard.check_sql_bounds(
            "SELECT " + "ARRAY[" * 9 + "1" + "]" * 9, max_chars=1000, max_nesting=8
        )
    assert refused.value.code == "sql_too_nested"
    assert isinstance(refused.value, ValueError)
    # round and square brackets count together
    assert sql_guard.sql_nesting("SELECT ([([1])])") == 4
    # the scan stops once past the bound
    assert sql_guard.sql_nesting("(" * 1000, stop_above=8) == 9


@pytest.mark.parametrize(
    ("sql", "depth"),
    [
        ("SELECT '((((((((((((((((((((' FROM t", 0),
        ("SELECT 'it''s [[[[[[[[[[[[[[[[[[' FROM t", 0),
        ("SELECT ''''||'((((((((((((((((((((' FROM t", 0),
        ('SELECT "a(((((((((((((((((((" FROM t', 0),
        ('SELECT "a""[[[[[[[[[[[[[[[[[[[[" FROM t', 0),
        ("SELECT 1 -- ((((((((((((((((((((\nFROM t", 0),
        ("SELECT 1 -- ((((((((((((((((((((\r(1)", 1),
        ("SELECT 1 /* [[[[[[[[[[[[[[[[[[[[ */ FROM t", 0),
        ("SELECT 1 /* it's -- ( */ (1)", 1),
        ("SELECT 1 -- /* \n(1)", 1),
        ("SELECT f(g('((((((((((((((((((((')) FROM t", 2),
        ("SELECT f('a)', g(1)) FROM t", 2),
        ('SELECT "x)" FROM (SELECT 1 AS "x)")', 1),
    ],
)
def test_brackets_in_literals_and_comments_do_not_count(sql, depth):
    assert sql_guard.sql_nesting(sql) == depth
    sql_guard.check_sql_bounds(sql, max_chars=1000, max_nesting=8)


@pytest.mark.parametrize(
    ("sql", "depth"),
    [
        ("SELECT ))))]]]] " + "(" * 9 + "1" + ")" * 9, 9),
        ("SELECT (1)) ((1))", 2),
        ("SELECT ]" * 20, 0),
    ],
)
def test_unbalanced_closers_never_make_the_depth_negative(sql, depth):
    assert sql_guard.sql_nesting(sql) == depth


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 'open ((((((((((((((((",
        'SELECT "open ((((((((((((((((',
        "SELECT 'open '' ((((((((((((((((",
        "SELECT 1 /* open ((((((((((((((((",
        "SELECT 1 -- ((((((((((((((((",
    ],
)
def test_an_open_literal_or_comment_hides_the_rest_as_it_does_from_the_parser(sql):
    assert sql_guard.sql_nesting(sql) == 0


def test_realistic_deep_query_is_accepted():
    from akko_mcp_trino.config import DEFAULT_MAX_SQL_CHARS, DEFAULT_MAX_SQL_NESTING

    for sql in (_TPCH_Q20, _REPORT_6_LEVELS):
        sql_guard.check_sql_bounds(
            sql, max_chars=DEFAULT_MAX_SQL_CHARS, max_nesting=DEFAULT_MAX_SQL_NESTING
        )
        assert sql_guard.is_read_only_sql(sql, "tpch")
    assert sql_guard.sql_nesting(_TPCH_Q20) == 3
    assert sql_guard.sql_nesting(_REPORT_6_LEVELS) == 9


def test_a_parsed_statement_is_read_by_every_reader_without_parsing_again(monkeypatch):
    real, texts = sql_guard.sqlglot.parse, []

    def counting(sql, *args, **kwargs):
        texts.append(sql)
        return real(sql, *args, **kwargs)

    monkeypatch.setattr(sql_guard.sqlglot, "parse", counting)
    parsed = sql_guard.parse_once("  SELECT n.nope FROM tpch.tiny.nation n  ")
    assert parsed.text == "SELECT n.nope FROM tpch.tiny.nation n"
    assert texts == [], "nothing is parsed before it is read"
    assert sql_guard.is_read_only_sql(parsed, "tpch")
    assert sql_guard.referenced_tables(parsed) == ["tpch.tiny.nation"]
    assert sql_guard.tables_for_column(parsed, "n.nope") == [("tpch", "tiny", "nation")]
    assert texts == ["SELECT n.nope FROM tpch.tiny.nation n"]
    # an EXPLAIN: the statement and the statement it explains, each parsed once
    texts.clear()
    explain = sql_guard.parse_once("EXPLAIN SELECT nope FROM tpch.tiny.nation")
    assert sql_guard.is_read_only_sql(explain, "tpch", frozenset({"EXPLAIN"}))
    assert sql_guard.referenced_tables(explain) == ["tpch.tiny.nation"]
    assert sql_guard.tables_for_column(explain, "nope") == [("tpch", "tiny", "nation")]
    assert sorted(texts) == [
        "EXPLAIN SELECT nope FROM tpch.tiny.nation",
        "SELECT nope FROM tpch.tiny.nation",
    ]
    # a statement that does not parse is parsed once, and each reader fails closed
    texts.clear()
    broken = sql_guard.parse_once("SELECT FROM WHERE (((")
    assert not sql_guard.is_read_only_sql(broken)
    assert sql_guard.referenced_tables(broken) == []
    assert sql_guard.tables_for_column(broken, "x") == []
    assert texts == ["SELECT FROM WHERE ((("]


@pytest.mark.parametrize(
    "sql",
    [
        "EXPLAIN EXPLAIN SELECT 1",
        "EXPLAIN /* plan */ EXPLAIN SELECT 1",
        "EXPLAIN (TYPE LOGICAL) EXPLAIN (TYPE IO) SELECT 1",
        "EXPLAIN " * 2000 + "SELECT 1",
    ],
)
def test_an_explain_of_an_explain_is_refused_in_linear_time(sql, monkeypatch):
    """A chain of EXPLAIN was read once per link: 900 links took 3 s on one CPU, 2000
    raised RecursionError. Now the statement and the one it explains are read, no more."""
    real, texts = sql_guard.sqlglot.parse, []

    def counting(text, *args, **kwargs):
        texts.append(text)
        return real(text, *args, **kwargs)

    monkeypatch.setattr(sql_guard.sqlglot, "parse", counting)
    parsed = sql_guard.parse_once(sql)
    assert sql_guard.is_read_only_sql(parsed, "c", frozenset({"EXPLAIN"})) is False
    assert sql_guard.referenced_tables(parsed) == []
    assert sql_guard.tables_for_column(parsed, "x") == []
    assert len(texts) <= 2
