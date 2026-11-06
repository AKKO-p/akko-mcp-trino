"""trino-tags: every query of a call carries its request id and its tool as client tags.

Trino keeps them with the query, so that its history, its resource groups and a policy
engine see which call of the server sent it. The filter's tag (mcp-gate) and the profile's
(mcp-profile) are added to them, never in their place.
"""

import pytest

from akko_mcp_trino import trino_client as tc
from akko_mcp_trino.audit import (
    bind_call,
    reset_call,
    reset_current_request_id,
    set_current_request_id,
)
from akko_mcp_trino.extensions import load_extensions
from akko_mcp_trino.middleware import _request_id
from akko_mcp_trino.modules import trino_tags
from akko_mcp_trino.server import build_server
from tests.tool_doubles import FakeMCP, core_config, in_request


class _Cursor:
    description = [("c",)]
    query_id = "q1"

    def execute(self, sql, params=None):
        pass

    def fetchmany(self, n):
        return [(1,)]


def _server(monkeypatch, extensions="trino-tags"):
    """The server built from its settings, the Trino driver replaced: the tags of each query."""
    sent = []

    def connect(**kw):
        sent.append(kw.get("client_tags"))
        return type("Connection", (), {"cursor": lambda self: _Cursor()})()

    monkeypatch.setattr(tc.trino.dbapi, "connect", connect)
    config = core_config(extensions=extensions)
    mcp, client = build_server(
        config, mcp_factory=lambda name, **_: FakeMCP(), extensions=load_extensions(config, {})
    )
    return mcp, client, sent


def test_every_query_of_a_call_carries_request_id_and_tool(monkeypatch):
    mcp, _, sent = _server(monkeypatch)
    in_request(lambda: mcp.tools["execute_query"]("SELECT 1"))
    in_request(lambda: mcp.tools["list_schemas"]("tpch"))
    assert sent == [
        ["request_id=req-1", "tool=execute_query"],
        ["request_id=req-1", "tool=list_schemas"],
    ]


def test_gate_queries_carry_mcp_gate_besides_the_call_tags(monkeypatch):
    _, client, sent = _server(monkeypatch)
    rid = set_current_request_id("edge-7")
    _, token = bind_call("describe_table")
    try:
        tc.gate_runner(client.query)("SHOW CATALOGS")
        with tc.query_tags("mcp-profile"):
            client.query("SELECT count(*) FROM tpch.tiny.nation")
    finally:
        reset_call(token)
        reset_current_request_id(rid)
    assert sent == [
        ["request_id=edge-7", "tool=describe_table", tc.GATE_TAG],
        ["request_id=edge-7", "tool=describe_table", "mcp-profile"],
    ]


def test_without_the_module_no_call_tag_is_sent(monkeypatch):
    mcp, client, sent = _server(monkeypatch, extensions="")
    in_request(lambda: mcp.tools["execute_query"]("SELECT 1"))
    tc.gate_runner(client.query)("SHOW CATALOGS")
    assert sent == [None, [tc.GATE_TAG]]


def test_outside_a_call_or_a_request_no_tag_is_made_up():
    module = trino_tags.create(core_config(), {})
    assert module.client_tags() == []
    rid = set_current_request_id("edge-8")
    try:
        assert module.client_tags() == ["request_id=edge-8"]
    finally:
        reset_current_request_id(rid)


@pytest.mark.parametrize("sent", ["a,tool=list_catalogs", "a=b", "x y", "é", "a" * 129])
def test_a_request_id_that_could_add_a_tag_never_reaches_trino(sent):
    """The core keeps an edge's request id only when it is a plain token: no comma and no
    `=` can add a tag of its own; a fresh id replaces any other."""
    kept = _request_id(sent)
    assert kept != sent and "," not in kept and "=" not in kept


def test_the_check_line_names_the_tags():
    assert trino_tags.create(core_config(), {}).check_lines() == [
        "trino-tags: every query carries request_id=<id> and tool=<tool> as client tags"
    ]


@pytest.mark.parametrize("tool", ["plugin,admin=1", "my tool", "x=y", "outil_é", ""])
def test_a_tool_name_that_could_add_a_tag_is_said_as_other(tool):
    module = trino_tags.create(core_config(), {})
    _, token = bind_call(tool)
    try:
        assert module.client_tags() == ["tool=other"]
    finally:
        reset_call(token)


def test_a_plain_tool_name_is_kept():
    module = trino_tags.create(core_config(), {})
    _, token = bind_call("my_plugin.search-v2:x")
    try:
        assert module.client_tags() == ["tool=my_plugin.search-v2:x"]
    finally:
        reset_call(token)
