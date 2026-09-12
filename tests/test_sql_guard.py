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
