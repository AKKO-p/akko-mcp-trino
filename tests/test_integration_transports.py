"""End to end, in process: the real MCPServer, the real SDK client, each transport.

Unit tests prove each piece; this proves the pieces fit the SDK we pin. The
server runs in a thread on a free port with a fake Trino connection and a real
RSA-signed JWT verified against an in-memory JWKS. Every transport the server
offers is exercised with the official client the hosts use: `sse`,
`streamable-http`, and `stdio` (a subprocess, the way Claude Desktop or Vibe
would launch it).
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time

import anyio
import jwt as pyjwt
import pytest
import uvicorn
from cryptography.hazmat.primitives.asymmetric import rsa
from mcp import ClientSession, MCPError
from mcp.client.sse import sse_client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
from mcp.types import INVALID_REQUEST

import akko_mcp_trino
from akko_mcp_trino import auth
from akko_mcp_trino.app import build_asgi_app
from akko_mcp_trino.config import Config
from akko_mcp_trino.server import build_server

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_ISS, _AUD = "https://idp.test/realms/t", "mcp"


class _FakeJwks:
    def get_signing_key_from_jwt(self, token):
        return type("K", (), {"key": _KEY.public_key()})()


def _token(user: str) -> str:
    return pyjwt.encode(
        {
            "iss": _ISS,
            "aud": _AUD,
            "exp": int(time.time()) + 600,
            "preferred_username": user,
            "jti": f"j-{user}",
        },
        _KEY,
        algorithm="RS256",
    )


class _FakeCursor:
    """Answers any SQL with one row carrying the user Trino saw."""

    def __init__(self, user):
        self.description, self._user = [("who",), ("sql",)], user
        self._sql = ""

    def execute(self, sql, params=None):
        self._sql = sql

    def fetchmany(self, n):
        return [(self._user, self._sql)]


def _fake_connect(**kw):
    return type("C", (), {"cursor": lambda self: _FakeCursor(kw.get("user"))})()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _config(transport: str, port: int) -> Config:
    return Config(
        trino_host="h",
        trino_identity_mode="service",
        query_timeout_seconds=0.0,
        call_timeout_seconds=0.0,
        trino_port=8080,
        trino_user="svc",
        trino_catalog="c",
        max_rows=10,
        read_only=True,
        auth_enabled=True,
        server_name="t",
        health_port=port + 1,
        jwks_url="https://idp.test/jwks",
        oidc_issuer=_ISS,
        oidc_audience=_AUD,
        transport=transport,
        mcp_port=port,
        auth_required=True,
        trino_http_scheme="https",
    )


@pytest.fixture(params=["sse", "streamable-http"])
def served(request, monkeypatch):
    import akko_mcp_trino.trino_client as tc

    monkeypatch.setattr(tc.trino.dbapi, "connect", _fake_connect)
    port = _free_port()
    cfg = _config(request.param, port)
    mcp, _ = build_server(cfg)
    provider = auth.JwksJwtAuth(
        cfg.jwks_url, cfg.oidc_issuer, cfg.oidc_audience, jwks_client=_FakeJwks()
    )
    app = build_asgi_app(cfg, mcp, auth_provider=provider)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    path = "/sse" if request.param == "sse" else "/mcp"
    yield request.param, f"http://127.0.0.1:{port}{path}"
    server.should_exit = True


def _open(transport, url, headers):
    """The SDK 2.x streamable client takes a configured httpx2 client, the SSE
    client still takes headers: one opener for the tests, whatever the transport."""
    if transport == "sse":
        return sse_client(url, headers=headers)
    return streamable_http_client(url, http_client=create_mcp_http_client(headers=headers))


async def _call(transport, url, headers, tool, args):
    async with _open(transport, url, headers) as streams:
        async with ClientSession(streams[0], streams[1]) as s:
            await s.initialize()
            res = await s.call_tool(tool, args)
            return res.is_error, res.content[0].text


def test_each_http_transport_admits_a_verified_caller_and_queries_as_the_configured_identity(
    served,
):
    transport, url = served
    headers = {"Authorization": f"Bearer {_token('alice')}"}
    is_err, text = anyio.run(_call, transport, url, headers, "execute_query", {"sql": "SELECT 1"})
    assert not is_err
    assert json.loads(text)["rows"][0][0] == "svc", "the query did not run as TRINO_USER"


def test_each_http_transport_refuses_without_a_token(served):
    transport, url = served
    with pytest.raises(Exception):
        anyio.run(_call, transport, url, {}, "list_catalogs", {})


def test_each_http_transport_announces_the_package_version_in_initialize(served):
    transport, url = served
    headers = {"Authorization": f"Bearer {_token('alice')}"}

    async def info():
        async with _open(transport, url, headers) as streams:
            async with ClientSession(streams[0], streams[1]) as s:
                return (await s.initialize()).server_info

    server_info = anyio.run(info)
    assert server_info.version == akko_mcp_trino.__version__
    assert server_info.name == "t", "the configured server name"


def test_each_http_transport_lists_the_eight_tools(served):
    transport, url = served
    headers = {"Authorization": f"Bearer {_token('alice')}"}

    async def names():
        async with _open(transport, url, headers) as streams:
            async with ClientSession(streams[0], streams[1]) as s:
                await s.initialize()
                return sorted(t.name for t in (await s.list_tools()).tools)

    assert anyio.run(names) == sorted(
        [
            "list_catalogs",
            "list_schemas",
            "list_tables",
            "describe_schema",
            "describe_table",
            "explain_query",
            "execute_query",
            "search_columns",
        ]
    )


@pytest.mark.parametrize("identity", ["service", "oauth2"])
def test_stdio_transport_runs_as_a_subprocess_with_the_users_token(identity):
    """Claude Desktop, Cursor and Vibe launch a local server over stdio. The
    user's token comes from the environment; strict mode still applies. Service mode
    without a door, chosen explicitly here, is announced to the host in the instructions
    of the initialization, by the real SDK."""
    env = {
        **os.environ,
        "MCP_TRANSPORT": "stdio",
        "MCP_AUTH_ENABLED": "false",
        "MCP_AUTH_REQUIRED": "false",
        "TRINO_IDENTITY_MODE": identity,
        "TRINO_HTTP_SCHEME": "https",
        "TRINO_HOST": "h",
    }
    params = StdioServerParameters(command=sys.executable, args=["-m", "akko_mcp_trino"], env=env)

    async def go():
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                init = await s.initialize()
                tools = sorted(t.name for t in (await s.list_tools()).tools)
                return init.instructions, tools, init.server_info

    instructions, tools, server_info = anyio.run(go)
    assert server_info.version == akko_mcp_trino.__version__
    assert server_info.name == "trino-mcp"
    assert "execute_query" in tools
    assert instructions.startswith("WARNING")
    if identity == "service":
        assert "TRINO_IDENTITY_MODE=service" in instructions
    assert "MCP_AUTH_ENABLED=false" in instructions


_OAUTH2_SERVER = os.path.join(os.path.dirname(__file__), "oauth2_stdio_server.py")


def _oauth2_stdio(tmp_path, client_calls, refuse=False):
    """Run ``client_calls(session)`` against the ``oauth2`` stdio server of
    ``oauth2_stdio_server.py`` once discovery is done; return what it answered, what
    discovery recorded and every event of the run."""
    events = tmp_path / "events.jsonl"
    events.touch()
    env = {
        **os.environ,
        "MCP_TRANSPORT": "stdio",
        "MCP_AUTH_ENABLED": "false",
        "MCP_AUTH_REQUIRED": "false",
        "TRINO_IDENTITY_MODE": "oauth2",
        "TRINO_HTTP_SCHEME": "https",
        "TRINO_HOST": "h",
        "OAUTH2_EVENTS": str(events),
        "OAUTH2_REFUSE": "1" if refuse else "",
    }
    params = StdioServerParameters(command=sys.executable, args=[_OAUTH2_SERVER], env=env)

    def read():
        return [json.loads(line) for line in events.read_text().splitlines()]

    async def go():
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                await s.list_tools()
                await s.list_resources()
                discovery = read()
                return await client_calls(s), discovery

    answered, discovery = anyio.run(go)
    return answered, discovery, read()


def test_oauth2_stdio_on_the_real_sdk_runs_each_call_as_the_user_trino_named(tmp_path):
    """The real SDK, over stdio, with Trino faked at the driver. Discovery signs in to
    nothing. Three concurrent tool calls and a resource read sign in once, and each runs
    as the user Trino named, with the binding of its own admission: a synchronous tool
    in a worker thread, an asynchronous one, a query and a resource an extension adds.
    It fails if the middleware stops binding the call's context, or stops admitting a
    method that is not discovery."""

    async def calls(s):
        answers = {}

        async def call(name, args=None):
            result = await s.call_tool(name, args or {})
            answers[name] = json.loads(result.content[0].text)

        async with anyio.create_task_group() as group:
            group.start_soon(call, "seen_sync")
            group.start_soon(call, "seen_async")
            group.start_soon(call, "execute_query", {"sql": "SELECT 1"})
        read = await s.read_resource("test://seen")
        answers["resource"] = json.loads(read.contents[0].text)
        return answers

    answers, discovery, events = _oauth2_stdio(tmp_path, calls)
    assert discovery == [], "discovery reached Trino or a guard"
    queries = [e for e in events if "trino" in e]
    assert queries[0] == {
        "trino": "SELECT current_user",
        "user": None,
        "subject": None,
        "bound": "unbound",
    }
    assert [e["trino"] for e in queries].count("SELECT current_user") == 1
    guards = [e for e in events if "guard" in e]
    assert len(guards) == 4
    assert all(g["subject"] == "alice" and g["stdio"] for g in guards)
    bindings = {f"call-{g['guard']}" for g in guards}
    seen = [answers["seen_sync"], answers["seen_async"], answers["resource"]]
    assert all(a["subject"] == "alice" for a in seen)
    (query,) = [e for e in queries if e["trino"] != "SELECT current_user"]
    assert query["subject"] == "alice" and query["user"] is None
    assert answers["execute_query"]["rows"] == [[1]]
    assert sorted([a["bound"] for a in seen] + [query["bound"]]) == sorted(bindings)
    audits = [e for e in events if "audit" in e]
    assert sorted(e["audit"] for e in audits) == ["execute_query", "seen_async", "seen_sync"]
    assert all(e["subject"] == "alice" and e["ok"] for e in audits)


def test_oauth2_stdio_on_the_real_sdk_refused_admission_runs_nothing(tmp_path):
    """A guard that refuses answers each call an ``INVALID_REQUEST`` error with its reason
    and what to do about it; no tool runs, no query reaches Trino past the identity
    check, and the audit records nothing."""

    async def calls(s):
        refused = []
        for request in (
            s.call_tool("execute_query", {"sql": "SELECT 1"}),
            s.call_tool("seen_sync", {}),
            s.read_resource("test://seen"),
        ):
            with pytest.raises(MCPError) as raised:
                await request
            refused.append((raised.value.code, raised.value.message))
        return refused

    refused, discovery, events = _oauth2_stdio(tmp_path, calls, refuse=True)
    assert discovery == []
    message = "admission refused: test_refused: ask an administrator"
    assert refused == [(INVALID_REQUEST, message)] * 3
    assert [e["trino"] for e in events if "trino" in e] == ["SELECT current_user"]
    assert len([e for e in events if "guard" in e]) == 3
    assert [e for e in events if "audit" in e] == []


def test_a_foreign_host_header_is_served_not_refused_with_421(served):
    """In a cluster the pod is reached as a service name, on the internet as a
    public name: neither is localhost, both must be served."""
    transport, url = served
    headers = {
        "Authorization": f"Bearer {_token('alice')}",
        "Host": "akko-mcp-trino.akko.svc:3000",
    }
    is_err, text = anyio.run(_call, transport, url, headers, "execute_query", {"sql": "SELECT 1"})
    assert not is_err
    assert json.loads(text)["rows"][0][0] == "svc"


def _serve(cfg, provider):
    mcp, _ = build_server(cfg)
    app = build_asgi_app(cfg, mcp, auth_provider=provider)
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=cfg.mcp_port, log_level="error")
    )
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    return server


def test_jwt_mode_on_the_real_sdk_sends_trino_the_exchanged_token_not_the_callers(monkeypatch):
    """The whole chain on the real SDK: the caller's token is verified at the door,
    exchanged at the identity provider (a stand-in here), and Trino receives the token
    issued for it, never the caller's. The live proof against Keycloak and Trino is in
    ``integration/``."""
    import akko_mcp_trino.token_exchange as te
    import akko_mcp_trino.trino_client as tc

    trino_saw, idp_saw = [], []

    def connect(**kw):
        trino_saw.append((kw["user"], kw["auth"].token))
        return _fake_connect(**kw)

    def exchange(url, **kw):
        idp_saw.append(kw["data"]["subject_token"])
        body = {"access_token": "issued-for-trino", "expires_in": 300}
        return type("R", (), {"status_code": 200, "json": lambda self: body})()

    monkeypatch.setattr(tc.trino.dbapi, "connect", connect)
    monkeypatch.setattr(te.httpx, "post", exchange)
    port = _free_port()
    cfg = Config(
        **{
            **_config("streamable-http", port).__dict__,
            "trino_identity_mode": "jwt",
            "token_exchange_url": "https://idp.test/token",
            "token_exchange_client_id": "mcp",
            "token_exchange_client_secret": "s",
            "token_exchange_audience": "trino",
        }
    )
    provider = auth.build_auth(cfg)
    provider._client = _FakeJwks()
    server = _serve(cfg, provider)
    try:
        caller = _token("alice")
        headers = {"Authorization": f"Bearer {caller}"}
        url = f"http://127.0.0.1:{port}/mcp"
        is_err, text = anyio.run(
            _call, "streamable-http", url, headers, "execute_query", {"sql": "SELECT 1"}
        )
    finally:
        server.should_exit = True
    assert not is_err, text
    assert trino_saw == [("alice", "issued-for-trino")]
    assert idp_saw == [caller], "the caller's token went to the identity provider only"


@pytest.mark.parametrize("read_only", [True, False])
def test_the_real_sdk_shows_hosts_the_annotation_the_read_only_setting_implies(
    monkeypatch, read_only
):
    import akko_mcp_trino.trino_client as tc

    monkeypatch.setattr(tc.trino.dbapi, "connect", _fake_connect)
    port = _free_port()
    cfg = Config(**{**_config("streamable-http", port).__dict__, "read_only": read_only})
    provider = auth.JwksJwtAuth(
        cfg.jwks_url, cfg.oidc_issuer, cfg.oidc_audience, jwks_client=_FakeJwks()
    )
    server = _serve(cfg, provider)
    headers = {"Authorization": f"Bearer {_token('alice')}"}

    async def annotations():
        url = f"http://127.0.0.1:{port}/mcp"
        async with _open("streamable-http", url, headers) as streams:
            async with ClientSession(streams[0], streams[1]) as s:
                await s.initialize()
                return {t.name: t.annotations for t in (await s.list_tools()).tools}

    try:
        found = anyio.run(annotations)
    finally:
        server.should_exit = True
    assert found["execute_query"].read_only_hint is read_only
    assert found["execute_query"].destructive_hint is (not read_only)
    assert found["list_tables"].read_only_hint is True
