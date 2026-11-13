"""Each answer is sent once: in the text of the reply, never repeated as structured content.

The tools return their answer as JSON text. The SDK declares an output schema for a tool
annotated ``-> str`` unless told not to, and then sends the same JSON twice in every reply:
as text, and escaped again inside ``structuredContent``. That doubled the bytes on the wire
past the bound the server documents for an answer. The real MCPServer is built here, with
Trino faked at the client, so the shape is the one the SDK sends to a host.
"""

from __future__ import annotations

import json

import anyio

from akko_mcp_trino import trino_client
from akko_mcp_trino.config import Config
from akko_mcp_trino.server import build_server

_CFG = dict(
    trino_host="h",
    trino_identity_mode="service",
    query_timeout_seconds=0.0,
    call_timeout_seconds=0.0,
    trino_port=8080,
    trino_user="u",
    trino_catalog="c",
    max_rows=100,
    read_only=True,
    auth_enabled=False,
    server_name="trino-mcp",
    health_port=3001,
    jwks_url="",
    oidc_issuer="",
    oidc_audience="",
    transport="streamable-http",
    mcp_port=3000,
    auth_required=False,
)


def _answer(sql: str) -> dict:
    """A small, fixed Trino: the same answer to the same statement, every time."""
    if sql.startswith("SHOW CATALOGS"):
        return {"columns": ["Catalog"], "rows": [["sales"]], "truncated": False}
    if sql.startswith("DESCRIBE"):
        rows = [["id", "bigint", "", "the order"], ["amount", "double", "", ""]]
        return {"columns": ["Column", "Type", "Extra", "Comment"], "rows": rows}
    if "information_schema.schemata" in sql:
        return {"columns": ["schema_name"], "rows": [["orders"]], "truncated": False}
    if "information_schema.tables" in sql:
        return {"columns": ["table_name", "table_type"], "rows": [["orders", "BASE TABLE"]]}
    if "system.jdbc.columns" in sql:
        rows = [["orders", "id", "bigint", None, 1]]
        return {"columns": ["t", "c", "ty", "r", "o"], "rows": rows}
    if "system.metadata.table_comments" in sql:
        return {"columns": ["t", "c"], "rows": [["orders", None]]}
    if sql.startswith("SELECT count(*)"):  # profile_table: rows, then count, distinct, min, max
        return {"columns": ["a", "b", "c", "d", "e"], "rows": [[2, 2, 2, 1, 2]]}
    return {"columns": ["id", "amount"], "rows": [[1, 2.5], [2, 7.0]], "row_count": 2}


def _server(monkeypatch, registrars=()):
    monkeypatch.setattr(
        trino_client.TrinoClient,
        "query",
        lambda self, sql, user=None, params=None, max_rows=0, **kw: _answer(sql),
    )
    mcp, _ = build_server(Config(**_CFG), extra_tool_registrars=registrars)
    return mcp


# One call per tool, and the text each one answered before this change, byte for byte.
_CALLS = {
    "list_catalogs": {},
    "list_schemas": {"catalog": "sales"},
    "list_tables": {"catalog": "sales", "schema": "orders"},
    "describe_schema": {"catalog": "sales", "schema": "orders"},
    "describe_table": {"catalog": "sales", "schema": "orders", "table": "orders"},
    "profile_table": {"catalog": "sales", "schema": "orders", "table": "orders", "columns": ["id"]},
    "explain_query": {"sql": "SELECT id FROM sales.orders.orders"},
    "execute_query": {"sql": "SELECT id, amount FROM sales.orders.orders"},
    "search_columns": {"text": "amount", "catalog": "sales", "schema": "orders"},
}
_BEFORE = {
    "list_catalogs": '{"catalogs": ["sales"], "next_offset": null, "truncated": false}',
    "list_schemas": '{"schemas": ["orders"], "next_offset": null, "truncated": false}',
    "list_tables": '{"tables": ["orders"], "next_offset": null, "truncated": false}',
    "describe_schema": (
        '{"catalog": "sales", "schema": "orders", "tables": [{"table": "orders", '
        '"type": "BASE TABLE", "comment": null, "columns": [{"column": "id", "type": "bigint", '
        '"comment": null}]}], "next_offset": null, "shortened": false}'
    ),
    "describe_table": (
        '{"columns": ["Column", "Type", "Extra", "Comment"], "rows": [["id", "bigint", "", '
        '"the order"], ["amount", "double", "", ""]], "truncated": false, '
        '"next_column_offset": null, "columns_cut": false}'
    ),
    "profile_table": (
        '{"table": "sales.orders.orders", "scope": "first_rows", "rows": 2, '
        '"columns_cut": false, "columns": [{"column": "id", "type": "bigint", "non_null": 2, '
        '"null_fraction": 0.0, "approx_distinct": 2, "min": 1, "max": 2}]}'
    ),
    "explain_query": '{"valid": true}',
    "execute_query": '{"columns": ["id", "amount"], "rows": [[1, 2.5], [2, 7.0]], "row_count": 2}',
    "search_columns": (
        '{"tables": [{"catalog": "sales", "schema": "orders", "table": "orders", '
        '"comment": "id"}], "matches": [{"catalog": "sales", "schema": "orders", '
        '"table": "orders", "column": "id", "type": "bigint", "comment": null}], '
        '"schemas_searched": 1, "not_searched": [], "complete": true, "next_cursor": null}'
    ),
}


def test_no_tool_declares_an_output_schema(monkeypatch):
    mcp = _server(monkeypatch)
    tools = anyio.run(mcp.list_tools)
    assert sorted(t.name for t in tools) == sorted(_CALLS)
    assert {t.name: t.output_schema for t in tools} == {name: None for name in _CALLS}


def test_each_reply_carries_its_answer_once_as_the_same_text_as_before(monkeypatch):
    mcp = _server(monkeypatch)

    async def replies():
        return {name: await mcp.call_tool(name, args) for name, args in _CALLS.items()}

    for name, reply in anyio.run(replies).items():
        assert reply.structured_content is None, name
        assert [c.type for c in reply.content] == ["text"], name
        assert reply.content[0].text == _BEFORE[name], name


def test_an_error_reply_carries_no_structured_content_either(monkeypatch):
    mcp = _server(monkeypatch)
    reply = anyio.run(mcp.call_tool, "execute_query", {"sql": "DELETE FROM sales.orders.orders"})
    assert reply.structured_content is None
    assert json.loads(reply.content[0].text)["kind"] == "read_only"


def test_a_plugin_tool_sends_its_answer_once_unless_it_asks_for_structured_output(monkeypatch):
    """A plugin's tools follow the built-in ones; a plugin that wants structured output says so."""

    def registrar(mcp, client):
        @mcp.tool()
        def plain() -> str:
            """Answer as text."""
            return json.dumps({"a": 1})

        @mcp.tool(structured_output=True)
        def shaped() -> dict[str, int]:
            """Answer with a structure, on purpose."""
            return {"a": 1}

        def added() -> str:
            """Answer as text, added."""
            return "x"

        mcp.add_tool(added)

    mcp = _server(monkeypatch, [registrar])

    async def scenario():
        tools = {t.name: t for t in await mcp.list_tools()}
        replies = {n: await mcp.call_tool(n, {}) for n in ("plain", "shaped", "added")}
        return tools, replies

    tools, replies = anyio.run(scenario)
    assert tools["plain"].output_schema is None
    assert tools["added"].output_schema is None
    assert tools["shaped"].output_schema is not None
    assert replies["plain"].structured_content is None
    assert replies["added"].structured_content is None
    assert replies["shaped"].structured_content == {"a": 1}
