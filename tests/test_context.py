"""Context providers: what Trino cannot say about a table, from wherever the platform keeps it.

The server knows no catalogue. It knows an interface, and a chain of providers
fills it: Trino's own comments (zero installation), a versioned file, or a
product's catalogue plugged in from outside the package. A provider describes;
it never decides access. When nobody knows anything, tools behave exactly as
before.
"""

from __future__ import annotations

import json

import pytest

from akko_mcp_trino.context import (
    ChainContext,
    ColumnContext,
    FileContext,
    NoContext,
    TableContext,
    TrinoCommentsContext,
    build_context,
    cached,
)


def _query_factory(table_rows=None, column_rows=None, calls=None):
    """A fake `run(sql)`: answers the table-comment and column-comment queries."""
    calls = calls if calls is not None else []

    def run(sql: str) -> dict:
        calls.append(sql)
        if "table_comments" in sql:
            return {
                "columns": ["comment"],
                "rows": table_rows or [],
                "row_count": len(table_rows or []),
            }
        if "information_schema.columns" in sql:
            rows = [r for r in (column_rows or []) if f"column_name = '{r[0]}'" in sql]
            return {"columns": ["column_name", "comment"], "rows": rows, "row_count": len(rows)}
        raise AssertionError(sql)

    run.calls = calls  # type: ignore[attr-defined]
    return run


def test_no_context_knows_nothing():
    p = NoContext()
    assert p.table("c", "s", "t") is None and p.column("c", "s", "t", "x") is None


def test_trino_comments_reads_table_and_column_comments_under_the_callers_query():
    run = _query_factory(
        table_rows=[["Customers, one row per person"]],
        column_rows=[["email", "Contact address"], ["segment", None]],
    )
    p = TrinoCommentsContext(run)
    t = p.table("core_postgres", "clients", "customers")
    assert t == TableContext(description="Customers, one row per person")
    assert p.column("core_postgres", "clients", "customers", "email") == ColumnContext(
        description="Contact address"
    )
    assert p.column("core_postgres", "clients", "customers", "segment") is None
    assert (
        "catalog_name = 'core_postgres'" in run.calls[0]
        and "table_name = 'customers'" in run.calls[0]
    )


def test_trino_comments_escapes_identifiers_it_puts_in_literals():
    run = _query_factory()
    TrinoCommentsContext(run).table("c", "s", "it's")
    assert "table_name = 'it''s'" in run.calls[0]


def test_trino_comments_returns_none_when_trino_refuses():
    def run(sql):
        raise RuntimeError("Access Denied")

    p = TrinoCommentsContext(run)
    assert p.table("c", "s", "t") is None and p.column("c", "s", "t", "x") is None


def test_file_context_reads_a_versioned_json_document(tmp_path):
    doc = {
        "version": 1,
        "tables": {
            "core_postgres.clients.customers": {
                "description": "One row per customer",
                "owner": "Customer data team",
                "tier": "gold",
                "grain": ["customer_id"],
                "joins": [
                    {
                        "columns": ["customer_id"],
                        "target": "core_postgres.clients.accounts",
                        "target_columns": ["customer_id"],
                    }
                ],
                "columns": {
                    "email": {"description": "Contact address", "classification": ["PII"]},
                    "segment": {
                        "description": "Commercial segment",
                        "values": ["retail", "business", "premium"],
                    },
                },
            }
        },
    }
    f = tmp_path / "context.json"
    f.write_text(json.dumps(doc))
    p = FileContext(f)
    t = p.table("core_postgres", "clients", "customers")
    assert (
        t.description == "One row per customer"
        and t.owner == "Customer data team"
        and t.tier == "gold"
    )
    assert t.grain == ["customer_id"] and t.joins[0].target == "core_postgres.clients.accounts"
    c = p.column("core_postgres", "clients", "customers", "segment")
    assert c.values == ["retail", "business", "premium"]
    assert p.column("core_postgres", "clients", "customers", "email").classification == ["PII"]
    assert p.table("core_postgres", "clients", "nothing") is None


def test_file_context_reloads_when_the_file_changes(tmp_path):
    f = tmp_path / "context.json"
    f.write_text(json.dumps({"version": 1, "tables": {"c.s.t": {"description": "v1"}}}))
    p = FileContext(f)
    assert p.table("c", "s", "t").description == "v1"
    import os

    f.write_text(json.dumps({"version": 1, "tables": {"c.s.t": {"description": "v2"}}}))
    os.utime(f, (os.stat(f).st_mtime + 5, os.stat(f).st_mtime + 5))
    assert p.table("c", "s", "t").description == "v2"


def test_file_context_refuses_an_unknown_version_or_a_missing_file(tmp_path):
    f = tmp_path / "context.json"
    f.write_text(json.dumps({"version": 99, "tables": {}}))
    with pytest.raises(ValueError):
        FileContext(f)
    with pytest.raises(FileNotFoundError):
        FileContext(tmp_path / "absent.json")


def test_chain_merges_field_by_field_first_provider_wins():
    class _A:
        def table(self, c, s, t):
            return TableContext(description="from A")

        def column(self, c, s, t, col):
            return None

    class _B:
        def table(self, c, s, t):
            return TableContext(description="from B", owner="team B", grain=["id"])

        def column(self, c, s, t, col):
            return ColumnContext(description="col from B")

    chain = ChainContext([_A(), _B()])
    t = chain.table("c", "s", "t")
    assert t.description == "from A" and t.owner == "team B" and t.grain == ["id"]
    assert chain.column("c", "s", "t", "x").description == "col from B"
    assert ChainContext([]).table("c", "s", "t") is None


def test_cached_asks_the_provider_once_per_ttl():
    class _Counting:
        n = 0

        def table(self, c, s, t):
            self.n += 1
            return TableContext(description=f"call {self.n}")

        def column(self, c, s, t, col):
            self.n += 1
            return None

    clock = [0.0]
    inner = _Counting()
    p = cached(inner, ttl_seconds=30, clock=lambda: clock[0])
    assert p.table("c", "s", "t").description == "call 1"
    assert p.table("c", "s", "t").description == "call 1"
    assert p.column("c", "s", "t", "x") is None and p.column("c", "s", "t", "x") is None
    assert inner.n == 2
    clock[0] += 31
    assert p.table("c", "s", "t").description == "call 3"


def test_build_context_from_env_chains_named_providers(tmp_path):
    f = tmp_path / "ctx.json"
    f.write_text(json.dumps({"version": 1, "tables": {"c.s.t": {"description": "from file"}}}))
    run = _query_factory(table_rows=[["from trino"]])
    p = build_context(
        {
            "MCP_CONTEXT_PROVIDERS": "file, trino-comments",
            "MCP_CONTEXT_FILE": str(f),
            "MCP_CONTEXT_TTL_SECONDS": "0",
        },
        run,
    )
    assert p.table("c", "s", "t").description == "from file"
    assert p.table("c", "s", "other").description == "from trino"


def test_build_context_defaults_to_nothing_and_refuses_unknown_names():
    run = _query_factory()
    assert build_context({}, run).table("c", "s", "t") is None
    with pytest.raises(ValueError):
        build_context({"MCP_CONTEXT_PROVIDERS": "datahub"}, run)
    with pytest.raises(ValueError):
        build_context({"MCP_CONTEXT_PROVIDERS": "file"}, run)  # file without MCP_CONTEXT_FILE


def test_context_documents_serialise_to_plain_dicts():
    t = TableContext(description="d", owner="o", tier="gold", grain=["id"], joins=[], tags=["x"])
    assert t.as_dict() == {
        "description": "d",
        "owner": "o",
        "tier": "gold",
        "grain": ["id"],
        "tags": ["x"],
    }
    assert ColumnContext(description="d").as_dict() == {"description": "d"}


def test_file_context_unknown_column_and_chain_without_column_answers(tmp_path):
    f = tmp_path / "ctx.json"
    f.write_text(json.dumps({"version": 1, "tables": {"c.s.t": {"description": "d"}}}))
    assert FileContext(f).column("c", "s", "t", "nope") is None
    assert ChainContext([NoContext()]).column("c", "s", "t", "x") is None


def test_build_context_ignores_the_name_none_in_a_list(tmp_path):
    f = tmp_path / "ctx.json"
    f.write_text(json.dumps({"version": 1, "tables": {"c.s.t": {"description": "d"}}}))
    p = build_context(
        {
            "MCP_CONTEXT_PROVIDERS": "none,file",
            "MCP_CONTEXT_FILE": str(f),
            "MCP_CONTEXT_TTL_SECONDS": "0",
        },
        lambda sql: {"rows": []},
    )
    assert p.table("c", "s", "t").description == "d"
