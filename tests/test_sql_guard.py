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
