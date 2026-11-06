"""fingerprint-audit: the audit line adds what the call did, joined to Trino by SHA-256.

Never the SQL text, never the token: the statement's digest, the tables it names, the rows,
and the tables whose catalogue description reached the caller, within the bound of the line.
"""

import json
from dataclasses import replace

from akko_mcp_trino.audit import InMemoryAudit, sql_digest
from akko_mcp_trino.auth import Principal
from akko_mcp_trino.identity import reset_current_principal, set_current_principal
from akko_mcp_trino.modules import fingerprint_audit
from akko_mcp_trino.server import build_server
from akko_mcp_trino.tools import register_query_tools
from tests.tool_doubles import FakeMCP, SchemaTrino, core_config, in_request

FIELDS = [
    "request_id",
    "tool",
    "subject",
    "token_id",
    "ok",
    "error",
    "sql_sha256",
    "tables",
    "duration_ms",
    "rows",
    "truncated",
    "metadata_served",
]
# A made-up token ({"alg": "RS256"}, {"sub": "alice"}, the word "signature"), built from its
# parts so that a secret scanner does not take it for a real one.
TOKEN = ".".join(("eyJhbGciOiJSUzI1NiJ9", "eyJzdWIiOiJhbGljZSJ9", "c2lnbmF0dXJl"))


class _Trino:
    """Answers every statement with one row, and records it."""

    def __init__(self):
        self.statements = []

    def query(self, sql, user=None, params=None, **keywords):
        self.statements.append(sql)
        return {"columns": ["c"], "rows": [[1]], "row_count": 1, "truncated": False}


def _execute(sql, max_names=200):
    inner = InMemoryAudit()
    extension = fingerprint_audit.create(core_config(), {})
    mcp, trino = FakeMCP(), _Trino()
    register_query_tools(mcp, trino, audit=extension.wrap_audit(inner), audit_max_names=max_names)
    principal = Principal(subject="alice", token_id="jti-42")
    token = set_current_principal(principal)
    try:
        mcp.tools["execute_query"](sql)
    finally:
        reset_current_principal(token)
    return inner.events[0].as_dict(), trino


def test_the_line_carries_the_fingerprint_fields_in_order():
    line, _ = _execute("SELECT * FROM tpch.tiny.nation")
    assert list(line) == FIELDS
    assert (line["subject"], line["token_id"], line["tables"]) == (
        "alice",
        "jti-42",
        ["tpch.tiny.nation"],
    )
    assert (line["rows"], line["truncated"], line["metadata_served"]) == (1, False, [])


def test_the_line_never_holds_the_sql_text_or_the_token():
    sql = "SELECT * FROM tpch.tiny.customer WHERE name = 'Customer#000000001'"
    line, _ = _execute(sql)
    text = json.dumps(line)
    assert "Customer#000000001" not in text and "SELECT" not in text
    assert TOKEN not in text and "eyJ" not in text


def test_the_sha256_is_the_one_of_the_statement_sent_to_trino():
    line, trino = _execute("select  1")
    assert trino.statements == ["select  1"]
    assert line["sql_sha256"] == sql_digest(trino.statements[0])


def test_a_line_past_the_bound_says_how_many_names_it_left():
    sql = "SELECT * FROM a.b.t1, a.b.t2, a.b.t3"
    line, _ = _execute(sql, max_names=2)
    assert line["tables"] == ["a.b.t1", "a.b.t2"] and line["tables_cut"] == 1
    assert list(line).index("tables_cut") == list(line).index("tables") + 1


def test_a_call_without_a_record_or_a_principal_writes_empty_fields():
    from akko_mcp_trino.audit import AuditEvent

    inner = InMemoryAudit()
    fingerprint_audit.create(core_config(), {}).wrap_audit(inner).record(
        AuditEvent(request_id="r", tool="t", subject="", ok=False, error="E")
    )
    line = inner.events[0].as_dict()
    assert list(line) == FIELDS
    assert (line["token_id"], line["sql_sha256"], line["tables"]) == ("", "", [])
    assert line["rows"] is None


def test_metadata_served_lists_only_visible_tables(tmp_path):
    """The file provider describes three tables of demo.sales; Trino lists two to the caller:
    the line names those two, and the third nowhere."""
    from akko_mcp_trino.context.file import from_env
    from tests.context.memory_provider import setup_for

    described = [
        {"catalog": "demo", "schema": "sales", "table": name, "description": f"The {name}"}
        for name in ("customers", "orders", "salaries")
    ]
    path = tmp_path / "context.json"
    path.write_text(json.dumps({"version": 1, "catalogs": ["demo"], "tables": described}))
    config = replace(core_config(), context_providers="file")
    setup = setup_for(config, from_env({"MCP_CONTEXT_FILE": str(path)}))
    try:
        inner = InMemoryAudit()
        mcp = FakeMCP()
        register_query_tools(
            mcp,
            SchemaTrino(tables=("customers", "orders")),
            audit=fingerprint_audit.create(config, {}).wrap_audit(inner),
            context=setup,
        )
        out = json.loads(in_request(lambda: mcp.tools["describe_schema"]("demo", "sales")))
    finally:
        setup.close()
    assert [t["description"]["text"] for t in out["tables"]] == ["The customers", "The orders"]
    line = inner.events[0].as_dict()
    assert line["metadata_served"] == ["demo.sales.customers", "demo.sales.orders"]
    assert "salaries" not in json.dumps(line)


def test_the_server_writes_the_fingerprint_through_its_audit(monkeypatch):
    """Named in MCP_EXTENSIONS, the module is found by its entry point and wraps the audit."""
    from akko_mcp_trino.extensions import load_extensions

    monkeypatch.setattr("akko_mcp_trino.server.TrinoClient", lambda *args, **kwargs: _Trino())
    inner = InMemoryAudit()
    config = core_config(extensions="fingerprint-audit")
    mcp, _ = build_server(
        config,
        mcp_factory=lambda name, **_: FakeMCP(),
        audit=inner,
        extensions=load_extensions(config, {}),
    )
    in_request(lambda: mcp.tools["execute_query"]("SELECT 1"))
    assert list(inner.events[0].as_dict()) == FIELDS


def test_a_cut_list_of_served_descriptions_says_so_right_after_it():
    line = fingerprint_audit.FingerprintEvent(
        request_id="r",
        tool="describe_schema",
        subject="s",
        ok=True,
        metadata_served=["a"],
        metadata_served_cut=2,
    ).as_dict()
    assert list(line)[-2:] == ["metadata_served", "metadata_served_cut"]
    assert line["metadata_served_cut"] == 2


def test_the_check_line_says_what_the_line_adds():
    assert fingerprint_audit.create(core_config(), {}).check_lines() == [
        "fingerprint-audit: the audit line adds token_id, sql_sha256, tables, rows, truncated "
        "and metadata_served, up to 200 names a list"
    ]


def test_an_extra_field_never_overwrites_the_fingerprint():
    """An extension's field named like a field of the fingerprint is dropped and counted."""
    line = fingerprint_audit.FingerprintEvent(
        request_id="r",
        tool="execute_query",
        subject="s",
        ok=True,
        sql_sha256="abc",
        extra={"sql_sha256": "forged", "tables": ["x"], "agent": "copilot"},
    ).as_dict()
    assert line["sql_sha256"] == "abc" and line["tables"] == []
    assert line["agent"] == "copilot" and line["extra_dropped"] == 2
