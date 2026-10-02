"""Tool plugins: audited like the built-in tools, and handed the server's machinery.

A registrar written ``(mcp, client)`` keeps working unchanged: every tool it
adds through ``mcp.tool()`` or ``mcp.add_tool()`` now leaves the same audit
record as a built-in tool. A registrar written ``(mcp, client, tools)`` also
receives a ``ToolContext``: the audit, a governed ``run``, ``note`` and the
describers of ``describe_table``.
"""

from __future__ import annotations

import json

import anyio
import pytest

from akko_mcp_trino.audit import (
    InMemoryAudit,
    current_call,
    current_request_id,
    note,
    sql_digest,
)
from akko_mcp_trino.auth import Principal
from akko_mcp_trino.config import Config
from akko_mcp_trino.identity import reset_current_principal, set_current_principal
from akko_mcp_trino.server import build_server
from akko_mcp_trino.tools import ToolContext


class FakeTrino:
    """Answers one row to any statement and records who asked what, from which call."""

    def __init__(self):
        self.calls: list[tuple] = []  # (sql, user, request id, tool, max_rows)

    def query(self, sql, user=None, params=None, max_rows=0):
        call = current_call()
        self.calls.append((sql, user, current_request_id(), call.tool if call else None, max_rows))
        return {"columns": ["c"], "rows": [["v"]], "row_count": 1, "truncated": False}


_CFG = dict(
    trino_host="h",
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
    transport="sse",
    mcp_port=3000,
    auth_required=False,
)


class SdkLikeMCP:
    """The SDK's shape: ``tool(name=None, …, annotations=None)``, ``add_tool(fn, name=None)``."""

    def __init__(self):
        self.tools, self.annotations = {}, {}
        self.version = "2.2"

    def tool(self, name=None, title=None, description=None, annotations=None, **kw):
        def deco(fn):
            self.tools[name or fn.__name__] = fn
            self.annotations[name or fn.__name__] = annotations
            return fn

        return deco

    def add_tool(self, fn, name=None, **kw):
        self.tools[name or fn.__name__] = fn


def _server(monkeypatch, registrars, **config):
    from akko_mcp_trino import trino_client

    trino = FakeTrino()
    monkeypatch.setattr(
        trino_client.TrinoClient,
        "query",
        lambda self, sql, user=None, params=None, max_rows=0: trino.query(
            sql, user=user, max_rows=max_rows
        ),
    )
    audit = InMemoryAudit()
    mcp, client = build_server(
        Config(**_CFG, **config),
        extra_tool_registrars=registrars,
        mcp_factory=lambda name: SdkLikeMCP(),
        audit=audit,
    )
    return mcp, client, trino, audit


def _as(user, fn):
    token = set_current_principal(Principal(subject=user, token_id="jti-1"))
    try:
        return fn()
    finally:
        reset_current_principal(token)


# ---- (mcp, client): unchanged, and audited


def test_a_two_argument_plugin_tool_is_audited_like_a_built_in_one(monkeypatch):
    def registrar(mcp, client):
        @mcp.tool()
        def ask(question: str) -> str:
            note(sql="SELECT 42", tables=["lake.ref.pays"], rows=1)
            return json.dumps({"answer": 42})

        @mcp.tool(name="renamed")
        def other() -> str:
            return json.dumps({"error": "refused by the plugin"})

        @mcp.tool()
        def coded() -> str:
            note(error="upstream_down")
            return json.dumps({"error": "the upstream answered 502 for 'FR76…'"})

        mcp.add_tool(lambda: "plain text, not JSON", name="plain")

    mcp, _, _, audit = _server(monkeypatch, [registrar])
    assert mcp.version == "2.2", "anything else on the server is reached as before"
    _as("carol_analyst", lambda: mcp.tools["ask"]("how many?"))
    _as("carol_analyst", lambda: mcp.tools["renamed"]())
    _as("carol_analyst", lambda: mcp.tools["coded"]())
    _as("carol_analyst", lambda: mcp.tools["plain"]())
    ask, other, coded, plain = audit.events
    assert (ask.tool, ask.subject, ask.ok, ask.call.rows) == ("ask", "carol_analyst", True, 1)
    assert ask.call.sql_sha256 == sql_digest("SELECT 42") and ask.call.tables == ["lake.ref.pays"]
    # the audit keeps a code, never the plugin's message: a plugin names its failure
    assert (other.tool, other.ok, other.error) == ("renamed", False, "tool_error")
    assert (coded.tool, coded.ok, coded.error) == ("coded", False, "upstream_down")
    assert (plain.tool, plain.ok) == ("plain", True)


def test_a_registrar_with_its_own_keyword_arguments_is_still_called_with_two(monkeypatch):
    seen = {}

    def registrar(mcp, client, *, http_factory=None, env=None):
        seen["called"] = True

    _server(monkeypatch, [registrar])
    assert seen == {"called": True}


def test_a_coroutine_plugin_tool_stays_a_coroutine_and_is_audited(monkeypatch):
    def registrar(mcp, client):
        @mcp.tool()
        async def slow() -> dict:
            note(rows=7)
            return {"error": "late"}

    mcp, _, _, audit = _server(monkeypatch, [registrar])
    out = _as("alice_analyst", lambda: anyio.run(mcp.tools["slow"]))
    assert out == {"error": "late"}
    e = audit.events[-1]
    assert (e.tool, e.call.rows, e.ok, e.error, e.subject) == (
        "slow",
        7,
        False,
        "tool_error",
        "alice_analyst",
    )


def test_a_plugin_tool_that_raises_is_audited_then_the_error_propagates(monkeypatch):
    def registrar(mcp, client):
        @mcp.tool()
        def boom() -> str:
            raise RuntimeError("plugin down")

    mcp, _, _, audit = _server(monkeypatch, [registrar])
    with pytest.raises(RuntimeError):
        mcp.tools["boom"]()
    assert audit.events[-1].ok is False and audit.events[-1].error == "RuntimeError"


# ---- (mcp, client, tools): the server's machinery


def test_a_three_argument_plugin_receives_the_tool_context(monkeypatch, tmp_path):
    seen = {}

    def registrar(mcp, client, tools):
        seen["tools"], seen["client"] = tools, client

    _, client, _, audit = _server(monkeypatch, [registrar])
    tools = seen["tools"]
    assert isinstance(tools, ToolContext) and seen["client"] is client
    assert tools.audit is audit and tools.describers == []
    assert tools.read_only is True and tools.note is note


def test_run_queries_under_the_callers_identity_and_may_lower_the_row_cap(monkeypatch):
    def registrar(mcp, client, tools):
        @tools.tool("Count")
        def count() -> str:
            tools.run("SELECT 1")
            return json.dumps(tools.run("SELECT 2", max_rows=5))

    mcp, _, trino, _ = _server(monkeypatch, [registrar])
    _as("carol_analyst", lambda: mcp.tools["count"]())
    first, second = trino.calls
    assert (first[0], first[1], first[4]) == ("SELECT 1", "carol_analyst", 0)
    assert (second[0], second[1], second[3], second[4]) == ("SELECT 2", "carol_analyst", "count", 5)


def test_a_registrar_whose_third_argument_has_a_default_receives_the_context(monkeypatch):
    seen = {}

    def registrar(mcp, client, tools=None):
        seen["tools"] = tools

    _server(monkeypatch, [registrar])
    assert isinstance(seen["tools"], ToolContext)


def test_a_callable_without_a_signature_is_called_with_two_arguments(monkeypatch):
    calls = []

    class _NoSignature:
        __signature__ = property(lambda self: (_ for _ in ()).throw(ValueError("no signature")))

        def __call__(self, *args):
            calls.append(len(args))

    _server(monkeypatch, [_NoSignature()])
    assert calls == [2]


def test_a_plugin_reaches_the_rest_of_the_server_through_the_audited_view(monkeypatch):
    seen = {}

    def registrar(mcp, client):
        seen["version"] = mcp.version

    _server(monkeypatch, [registrar])
    assert seen == {"version": "2.2"}


def test_on_the_real_sdk_a_plugin_tool_keeps_its_arguments_and_is_audited():
    """The SDK reads each tool's arguments from its signature; the audit wrapper keeps it."""

    def registrar(mcp, client):
        @mcp.tool()
        def ask(question: str, limit: int = 5) -> str:
            """Ask a question."""
            note(rows=limit)
            return json.dumps({"question": question})

        async def later(x: int) -> str:
            """Answer later."""
            return str(x)

        mcp.add_tool(later)

    audit = InMemoryAudit()
    mcp, _ = build_server(Config(**_CFG), extra_tool_registrars=[registrar], audit=audit)

    async def scenario():
        tools = {t.name: t for t in await mcp.list_tools()}
        assert tools["ask"].input_schema["required"] == ["question"]
        assert set(tools["ask"].input_schema["properties"]) == {"question", "limit"}
        assert tools["later"].input_schema["required"] == ["x"]
        assert (await mcp.call_tool("ask", {"question": "hi", "limit": 3})).is_error is False
        assert (await mcp.call_tool("later", {"x": 4})).content[0].text == "4"

    anyio.run(scenario)
    assert [(e.tool, e.ok, e.call.rows) for e in audit.events] == [
        ("ask", True, 3),
        ("later", True, None),
    ]


# ---- a plugin's tools are due within the server's call budget, like the built-in ones


def test_every_tool_of_a_plugin_is_due_within_the_servers_call_budget(monkeypatch):
    seen = []

    def registrar(mcp, client, tools):
        @mcp.tool()
        def decorated() -> str:
            seen.append(current_call().budget_seconds)
            return "{}"

        def added() -> str:
            seen.append(current_call().budget_seconds)
            return "{}"

        mcp.add_tool(added)

        @tools.tool("Through the tool context")
        def contextual() -> str:
            seen.append(current_call().budget_seconds)
            return "{}"

    mcp, _, _, _ = _server(monkeypatch, [registrar], call_timeout_seconds=7)
    for name in ("decorated", "added", "contextual", "list_catalogs"):
        _as("alice_analyst", lambda: mcp.tools[name]())
    assert seen == [7, 7, 7]


def test_a_plugin_answer_that_comes_past_the_calls_deadline_is_an_error(monkeypatch):
    """A plugin that calls its own backend (not Trino) spends the same budget: an answer that
    arrives past the deadline, whole as it may be, is refused like a partial one, and the
    audit line says why."""
    import akko_mcp_trino.audit as audit_module

    now = [0.0]
    monkeypatch.setattr(audit_module.time, "monotonic", lambda: now[0])

    def registrar(mcp, client, tools):
        @mcp.tool()
        def slow_backend() -> str:
            now[0] += 8  # the backend answers after 8 s
            return json.dumps({"answer": 42})

        @mcp.tool()
        def quick_backend() -> str:
            now[0] += 1
            return json.dumps({"answer": 42})

    mcp, _, _, audit = _server(monkeypatch, [registrar], call_timeout_seconds=7)
    late = json.loads(_as("alice_analyst", lambda: mcp.tools["slow_backend"]()))
    assert late == {
        "error": "the tool call exceeded its time budget of 7 s; nothing it gathered is "
        "returned: narrow the request (a catalog, more precise words) or try again"
    }
    assert json.loads(_as("alice_analyst", lambda: mcp.tools["quick_backend"]())) == {"answer": 42}
    assert [(e.tool, e.ok, e.error) for e in audit.events] == [
        ("slow_backend", False, "CallTimeout"),
        ("quick_backend", True, ""),
    ]
