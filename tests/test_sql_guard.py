"""akko_mcp_trino.sql_guard: literal escaping, identifier validation, read-only classification."""

import pytest

from akko_mcp_trino import sql_guard


def test_safe_sql_string_escapes_quotes_backslashes_nulls():
    assert sql_guard.safe_sql_string("O'Brien") == "O''Brien"
    assert sql_guard.safe_sql_string("a\\b") == "a\\\\b"
    assert sql_guard.safe_sql_string("x\0y") == "xy"
    # quote and backslash combined
    assert sql_guard.safe_sql_string("a\\'b") == "a\\\\''b"


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
        "EXPLAIN SELECT 1",
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
def test_explain_of_a_read_is_allowed(sql):
    from akko_mcp_trino.sql_guard import is_read_only_sql

    assert is_read_only_sql(sql) is True


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
    """CALL runs a procedure: refused. DESCRIBE OUTPUT reads a statement shape: allowed."""
    from akko_mcp_trino.sql_guard import is_read_only_sql

    assert is_read_only_sql("CALL iceberg.system.rollback_to_snapshot('s', 't', 1)") is False
    assert is_read_only_sql("DESCRIBE OUTPUT q", session_catalog="iceberg") is True
