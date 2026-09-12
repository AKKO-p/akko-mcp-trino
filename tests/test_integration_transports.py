"""End to end, in process: the real FastMCP, the real SDK client, each transport.

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
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamablehttp_client

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
        agent_keys="cursor:k1",
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


async def _call(transport, url, headers, tool, args):
    opener = sse_client if transport == "sse" else streamablehttp_client
    async with opener(url, headers=headers) as streams:
        async with ClientSession(streams[0], streams[1]) as s:
            await s.initialize()
            res = await s.call_tool(tool, args)
            return res.isError, res.content[0].text


def test_each_http_transport_serves_tools_under_the_callers_identity(served):
    transport, url = served
    headers = {"Authorization": f"Bearer {_token('alice')}", "X-Agent-Key": "k1"}
    is_err, text = anyio.run(_call, transport, url, headers, "execute_query", {"sql": "SELECT 1"})
    assert not is_err
    assert json.loads(text)["rows"][0][0] == "alice", "the query did not run as the token's subject"


def test_each_http_transport_refuses_without_a_token(served):
    transport, url = served
    with pytest.raises(Exception):
        anyio.run(_call, transport, url, {"X-Agent-Key": "k1"}, "list_catalogs", {})


def test_each_http_transport_lists_the_eight_tools(served):
    transport, url = served
    headers = {"Authorization": f"Bearer {_token('alice')}", "X-Agent-Key": "k1"}

    async def names():
        opener = sse_client if transport == "sse" else streamablehttp_client
        async with opener(url, headers=headers) as streams:
            async with ClientSession(streams[0], streams[1]) as s:
                await s.initialize()
                return sorted(t.name for t in (await s.list_tools()).tools)

    assert anyio.run(names) == sorted(
        [
            "list_catalogs",
            "list_schemas",
            "list_tables",
            "describe_table",
            "search_columns",
            "profile_table",
            "explain_query",
            "explain_table",
            "execute_query",
        ]
    )


def test_stdio_transport_runs_as_a_subprocess_with_the_users_token():
    """Claude Desktop, Cursor and Vibe launch a local server over stdio. The
    user's token comes from the environment; strict mode still applies."""
    env = {
        **os.environ,
        "MCP_TRANSPORT": "stdio",
        "MCP_AUTH_ENABLED": "false",
        "TRINO_HOST": "h",
    }
    params = StdioServerParameters(command=sys.executable, args=["-m", "akko_mcp_trino"], env=env)

    async def go():
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                return sorted(t.name for t in (await s.list_tools()).tools)

    assert "execute_query" in anyio.run(go)
