"""Several instances and a clean stop: real server processes, real HTTP, a real signal.

Two replicas are two processes behind no load balancer: a client that opens with one and
sends its next request to the other is what a Service or a Route does when it spreads
connections. The stop is the one Kubernetes makes: SIGTERM, then a grace period. Trino is
a small HTTP server that speaks Trino's statement protocol, holds a query running as long
as the test wants, and records each cancel (``DELETE``) it receives.
"""

from __future__ import annotations

import asyncio
import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
import uvicorn
from starlette.applications import Starlette

from akko_mcp_trino.__main__ import check_config
from akko_mcp_trino.app import build_asgi_app
from akko_mcp_trino.config import DEFAULT_SHUTDOWN_TIMEOUT_SECONDS, Config
from akko_mcp_trino.shutdown import (
    CloseConnections,
    Drain,
    _keep_event_streams_on_sigterm,
    _Server,
)
from akko_mcp_trino.trino_client import Cancellation

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")

_BIGINT = {"rawType": "bigint", "arguments": []}

# The revision of the initialize handshake that a client of 2025 negotiates.
_LEGACY = "2025-11-25"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _until(condition, seconds=30.0):
    end = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < end, "condition not reached"
        time.sleep(0.05)


def _listening(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


class FakeTrino:
    """Trino's statement protocol over HTTP: a statement that names ``lineitem`` runs for
    ``hold`` seconds (forever when None), any other answers one row at once. A ``DELETE``
    on a query cancels it and is recorded in ``cancelled``."""

    def __init__(self, hold: float | None = None) -> None:
        self.hold = hold
        self.cancelled: list[str] = []
        self.started: list[str] = []
        self._lock = threading.Lock()
        self._queries: dict[str, dict[str, Any]] = {}
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def _reply(self, body: dict | None, status: int = 200) -> None:
                data = json.dumps(body).encode() if body is not None else b""
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self) -> None:  # noqa: N802 - the http.server interface
                sql = self.rfile.read(int(self.headers["Content-Length"])).decode()
                self._reply(fake.start(sql, self.server.server_address[1]))

            def do_GET(self) -> None:  # noqa: N802
                self._reply(fake.poll(self.path.split("/")[-2], self.server.server_address[1]))

            def do_DELETE(self) -> None:  # noqa: N802
                fake.cancel(self.path.split("/")[-2])
                self._reply(None, 204)

            def log_message(self, *_: Any) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def _next(self, qid: str, port: int, state: str) -> dict:
        return {
            "id": qid,
            "infoUri": f"http://127.0.0.1:{port}/ui/query.html?{qid}",
            "nextUri": f"http://127.0.0.1:{port}/v1/statement/executing/{qid}/1",
            "stats": {"state": state, "queued": False, "scheduled": True, "nodes": 1},
        }

    def start(self, sql: str, port: int) -> dict:
        with self._lock:
            qid = f"q{len(self._queries) + 1}"
            self._queries[qid] = {"sql": sql, "since": time.monotonic(), "cancelled": False}
            if "lineitem" in sql:
                self.started.append(qid)
        return self._next(qid, port, "QUEUED")

    def poll(self, qid: str, port: int) -> dict:
        query = self._queries[qid]
        if query["cancelled"]:
            return {
                "id": qid,
                "infoUri": f"http://127.0.0.1:{port}/ui/query.html?{qid}",
                "stats": {"state": "FAILED"},
                "error": {
                    "message": "Query was canceled",
                    "errorCode": 3,
                    "errorName": "USER_CANCELED",
                    "errorType": "USER_ERROR",
                },
            }
        long = "lineitem" in query["sql"]
        if long and (self.hold is None or time.monotonic() - query["since"] < self.hold):
            time.sleep(0.2)
            return self._next(qid, port, "RUNNING")
        return {
            "id": qid,
            "infoUri": f"http://127.0.0.1:{port}/ui/query.html?{qid}",
            "columns": [{"name": "n", "type": "bigint", "typeSignature": _BIGINT}],
            "data": [[42]],
            "stats": {"state": "FINISHED"},
        }

    def cancel(self, qid: str) -> None:
        with self._lock:
            self._queries[qid]["cancelled"] = True
            self.cancelled.append(qid)

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def _server(trino_port: int, **settings: str) -> tuple[subprocess.Popen, int]:
    port, health = _free_port(), _free_port()
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("MCP_", "TRINO_", "OIDC_"))
    }
    env.update(
        {
            "PYTHONPATH": _ROOT,
            "MCP_TRANSPORT": "streamable-http",
            "MCP_PORT": str(port),
            "MCP_HEALTH_PORT": str(health),
            "MCP_AUTH_ENABLED": "false",
            "MCP_AUTH_REQUIRED": "false",
            "TRINO_HOST": "127.0.0.1",
            "TRINO_PORT": str(trino_port),
            "TRINO_HTTP_SCHEME": "http",
            "TRINO_CATALOG": "tpch",
            "TRINO_IDENTITY_MODE": "service",
            **settings,
        }
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "akko_mcp_trino"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        _until(lambda: _listening(port) and _listening(health))
    except AssertionError:
        proc.kill()
        raise AssertionError(proc.communicate(timeout=10)[1].decode()) from None
    return proc, port


def _end(proc: subprocess.Popen, seconds: float = 30) -> str:
    try:
        _, err = proc.communicate(timeout=seconds)
    except subprocess.TimeoutExpired:
        proc.kill()
        _, err = proc.communicate(timeout=10)
    return err.decode()


def _rpc(
    port: int, method: str, params: dict, *, session: str | None = None, rid: int = 1
) -> tuple[int, dict[str, str], dict | None]:
    """One JSON-RPC request on a new connection: status, headers, the JSON-RPC answer."""
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": _LEGACY,
    }
    if session:
        headers["Mcp-Session-Id"] = session
    body = json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    try:
        conn.request("POST", "/mcp", body, headers)
        res = conn.getresponse()
        text = res.read().decode()
        answer = None
        for line in text.splitlines():
            if line.startswith("data:") and line[5:].strip():
                answer = json.loads(line[5:])
        if answer is None and text.strip().startswith("{"):
            answer = json.loads(text)
        return res.status, {k.lower(): v for k, v in res.getheaders()}, answer
    finally:
        conn.close()


_INITIALIZE = {
    "protocolVersion": _LEGACY,
    "capabilities": {},
    "clientInfo": {"name": "test", "version": "1"},
}


@pytest.fixture
def trino():
    fake = FakeTrino()
    yield fake
    fake.close()


def _two_replicas(trino: FakeTrino, stateless: str):
    a, port_a = _server(trino.port, MCP_STATELESS_HTTP=stateless)
    b, port_b = _server(trino.port, MCP_STATELESS_HTTP=stateless)
    return (a, port_a), (b, port_b)


def test_without_a_session_a_2025_client_is_served_by_any_replica(trino):
    (a, port_a), (b, port_b) = _two_replicas(trino, "true")
    try:
        status, headers, _ = _rpc(port_a, "initialize", _INITIALIZE)
        assert status == 200 and "mcp-session-id" not in headers
        for rid, port in enumerate((port_b, port_a, port_b), start=2):
            status, _, answer = _rpc(port, "tools/list", {}, rid=rid)
            assert status == 200, answer
            assert "execute_query" in {t["name"] for t in answer["result"]["tools"]}
    finally:
        for proc in (a, b):
            proc.terminate()
            _end(proc)


def test_with_sessions_a_2025_client_is_refused_by_the_other_replica(trino):
    """The failure the setting removes: the session lives in one process only."""
    (a, port_a), (b, port_b) = _two_replicas(trino, "false")
    try:
        status, headers, _ = _rpc(port_a, "initialize", _INITIALIZE)
        session = headers.get("mcp-session-id")
        assert status == 200 and session
        assert _rpc(port_a, "tools/list", {}, session=session, rid=2)[0] == 200
        assert _rpc(port_b, "tools/list", {}, session=session, rid=3)[0] == 404
    finally:
        for proc in (a, b):
            proc.terminate()
            _end(proc)


def _call_in_thread(port: int, sql: str) -> tuple[threading.Thread, list]:
    out: list = []

    def call() -> None:
        try:
            params = {"name": "execute_query", "arguments": {"sql": sql}}
            out.append(_rpc(port, "tools/call", params))
        except OSError as exc:
            out.append(exc)

    thread = threading.Thread(target=call, daemon=True)
    thread.start()
    return thread, out


_LONG = "SELECT count(*) FROM tpch.sf100.lineitem"


def test_sigterm_cancels_in_trino_the_query_of_a_call_past_the_shutdown_timeout(trino):
    proc, port = _server(trino.port, MCP_SHUTDOWN_TIMEOUT_SECONDS="1")
    thread, _ = _call_in_thread(port, _LONG)
    _until(lambda: trino.started)
    proc.send_signal(signal.SIGTERM)
    err = _end(proc)
    assert trino.started and set(trino.cancelled) == set(trino.started), err  # none left
    assert proc.returncode == -signal.SIGTERM, err
    thread.join(10)


def test_a_call_in_flight_at_sigterm_ends_and_answers_within_the_shutdown_timeout(trino):
    trino.hold = 2.0
    proc, port = _server(trino.port, MCP_SHUTDOWN_TIMEOUT_SECONDS="30")
    thread, out = _call_in_thread(port, _LONG)
    _until(lambda: trino.started)
    proc.send_signal(signal.SIGTERM)
    thread.join(30)
    err = _end(proc)
    status, _, answer = out[0]
    assert status == 200, err
    reply = json.loads(answer["result"]["content"][0]["text"])
    assert reply.get("rows") == [[42]], reply
    assert trino.cancelled == [], err


def test_during_the_shutdown_delay_answers_close_their_connection_then_none_is_accepted(
    trino,
):
    proc, port = _server(trino.port, MCP_SHUTDOWN_DELAY_SECONDS="3")
    try:
        assert _rpc(port, "tools/list", {})[1].get("connection") != "close"
        started = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        status, headers, answer = _rpc(port, "tools/list", {}, rid=2)
        assert status == 200 and answer["result"]["tools"]
        assert headers.get("connection") == "close"
        _until(lambda: not _listening(port), seconds=15)
        assert time.monotonic() - started >= 2.5, "the delay is kept before the port closes"
    finally:
        proc.terminate()
        _end(proc)


# The pieces of the stop and of the setting, in process.

_SETTINGS = (
    "MCP_STATELESS_HTTP",
    "MCP_SHUTDOWN_DELAY_SECONDS",
    "MCP_SHUTDOWN_TIMEOUT_SECONDS",
    "MCP_CALL_TIMEOUT_SECONDS",
    "TRINO_QUERY_TIMEOUT_SECONDS",
)


@pytest.fixture
def env(monkeypatch):
    for name in _SETTINGS:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_streamable_http_serves_without_a_session_by_default(env):
    config = Config.from_env()
    assert config.stateless_http() is True
    assert config.problems() == [] or not any("MCP_STATELESS" in p for p in config.problems())


@pytest.mark.parametrize(
    ("text", "stateless"), [("false", False), (" FALSE ", False), ("True", True)]
)
def test_the_stateless_setting_reads_true_or_false(env, text, stateless):
    env.setenv("MCP_STATELESS_HTTP", text)
    config = Config.from_env()
    assert config.stateless_http() is stateless
    assert not any("MCP_STATELESS_HTTP" in p for p in config.problems())


@pytest.mark.parametrize("text", ["yes", "0", "on"])
def test_a_stateless_setting_that_is_no_boolean_refuses_to_start(env, text):
    env.setenv("MCP_STATELESS_HTTP", text)
    assert f"MCP_STATELESS_HTTP={text!r} is not a boolean: set true or false" in " ".join(
        Config.from_env().problems()
    )


def test_the_shutdown_timeout_is_the_longest_a_call_may_run_unless_set(env):
    assert Config.from_env().shutdown_timeout() == 120.0  # the call budget's default
    env.setenv("MCP_CALL_TIMEOUT_SECONDS", "300")
    assert Config.from_env().shutdown_timeout() == 300.0
    env.setenv("MCP_CALL_TIMEOUT_SECONDS", "0")
    assert Config.from_env().shutdown_timeout() == 60.0  # then the query budget
    env.setenv("TRINO_QUERY_TIMEOUT_SECONDS", "0")
    assert Config.from_env().shutdown_timeout() == 120.0  # no budget at all
    env.setenv("MCP_SHUTDOWN_TIMEOUT_SECONDS", "45")
    assert Config.from_env().shutdown_timeout() == 45.0
    env.setenv("MCP_SHUTDOWN_TIMEOUT_SECONDS", " ")
    assert Config.from_env().shutdown_timeout_seconds is None


def test_the_shutdown_delay_defaults_to_none_and_is_read(env):
    assert Config.from_env().shutdown_delay_seconds == 0.0
    env.setenv("MCP_SHUTDOWN_DELAY_SECONDS", "5")
    assert Config.from_env().shutdown_delay_seconds == 5.0


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("MCP_SHUTDOWN_DELAY_SECONDS", "-1"),
        ("MCP_SHUTDOWN_TIMEOUT_SECONDS", "-1"),
        ("MCP_SHUTDOWN_TIMEOUT_SECONDS", "nan"),
        ("MCP_SHUTDOWN_DELAY_SECONDS", "inf"),
    ],
)
def test_shutdown_seconds_below_0_or_not_finite_refuse_to_start(env, name, value):
    env.setenv(name, value)
    assert any(p.startswith(f"{name}=") for p in Config.from_env().problems())


def test_check_says_how_the_server_runs_as_several_instances_and_stops(env, monkeypatch, capsys):
    for name in ("TRINO_IDENTITY_MODE", "MCP_AUTH_ENABLED", "MCP_AUTH_REQUIRED"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TRINO_IDENTITY_MODE", "service")
    monkeypatch.setenv("MCP_AUTH_ENABLED", "false")
    monkeypatch.setenv("MCP_AUTH_REQUIRED", "false")
    env.setenv("MCP_SHUTDOWN_DELAY_SECONDS", "5")
    check_config(Config.from_env())
    out = capsys.readouterr().out
    assert "instances: streamable HTTP without a session (MCP_STATELESS_HTTP)" in out
    assert "on SIGTERM 5 s of answers that close their connection, then 120 s" in out
    env.setenv("MCP_STATELESS_HTTP", "false")
    check_config(Config.from_env())
    assert "clients of a 2025 revision need an affinity" in capsys.readouterr().out


class _MCP:
    def __init__(self, lifespan=None):
        self.stateless = None
        self._lifespan = lifespan

    def streamable_http_app(self, *, transport_security=None, stateless_http=False):
        self.stateless = stateless_http
        return Starlette(lifespan=self._lifespan)


@pytest.mark.parametrize(("text", "stateless"), [("", True), ("false", False)])
def test_the_setting_reaches_the_sdk_transport(env, text, stateless):
    env.setenv("MCP_STATELESS_HTTP", text)
    mcp = _MCP()
    build_asgi_app(Config.from_env(), mcp, auth_provider=None)
    assert mcp.stateless is stateless


def test_the_stop_runs_before_the_transport_ends_and_the_close_after(env):
    events = []

    @asynccontextmanager
    async def transport(app):
        events.append("start")
        yield
        events.append("transport ended")

    app = build_asgi_app(
        Config.from_env(),
        _MCP(transport),
        auth_provider=None,
        on_stop=lambda: events.append("stop"),
    )

    async def cycle():
        async with app.router.lifespan_context(app):
            events.append("serving")

    asyncio.run(cycle())
    assert events == ["start", "serving", "stop", "transport ended"]


async def _answer(app, scope_type="http"):
    sent = []

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(message):
        sent.append(message)

    await app({"type": scope_type}, receive, send)
    return sent


async def _inner(scope, receive, send):
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"connection", b"keep-alive"), (b"x", b"1")],
        }
    )
    await send({"type": "http.response.body", "body": b"ok"})


def test_an_answer_closes_its_connection_once_the_drain_has_begun():
    drain = Drain()
    app = CloseConnections(_inner, drain)
    before = asyncio.run(_answer(app))[0]["headers"]
    assert (b"connection", b"keep-alive") in before
    drain.begin()
    start, body = asyncio.run(_answer(app))
    assert start["headers"] == [(b"x", b"1"), (b"connection", b"close")]
    assert body == {"type": "http.response.body", "body": b"ok"}


def test_a_lifespan_or_websocket_scope_passes_unchanged():
    drain = Drain()
    drain.begin()
    seen = []

    async def inner(scope, receive, send):
        seen.append(scope["type"])

    asyncio.run(_answer(CloseConnections(inner, drain), "lifespan"))
    assert seen == ["lifespan"]


@pytest.fixture(autouse=True)
def _event_streams_untouched(monkeypatch):
    """uvicorn's handle_exit, as sse-starlette replaces it, marks every event stream of the
    process to end: a test that calls it must not end the streams of the tests after it."""
    from sse_starlette.sse import AppStatus

    monkeypatch.setattr(AppStatus, "should_exit", False)
    monkeypatch.setattr(AppStatus, "enable_automatic_graceful_drain", True)


def _server_object(delay):
    return _Server(uvicorn.Config(_inner), Drain(), delay)


def test_ctrl_c_stops_at_once_without_the_delay():
    server = _server_object(30)
    server.handle_exit(signal.SIGINT, None)
    assert server.should_exit and not server._drain.closing()


def test_sigterm_without_a_delay_closes_connections_and_stops_at_once():
    server = _server_object(0)
    server.handle_exit(signal.SIGTERM, None)
    assert server.should_exit and server._drain.closing()


def test_sigterm_serves_the_delay_then_stops_and_a_second_one_stops_at_once():
    server = _server_object(0.3)
    server.handle_exit(signal.SIGTERM, None)
    assert server._drain.closing() and not server.should_exit
    _until(lambda: server.should_exit, seconds=5)
    later = _server_object(30)
    later.handle_exit(signal.SIGTERM, None)
    later.handle_exit(signal.SIGTERM, None)
    assert later.should_exit


def test_event_streams_are_kept_on_sigterm():
    from sse_starlette.sse import AppStatus

    _keep_event_streams_on_sigterm()
    assert AppStatus.enable_automatic_graceful_drain is False


def test_waiting_for_the_cancels_stops_at_its_timeout():
    gate = threading.Event()
    cancel = Cancellation()
    cancel.watch(gate.wait)
    cancel.cancel()
    started = time.monotonic()
    cancel.wait(0.2)
    assert 0.15 <= time.monotonic() - started < 2
    gate.set()
    cancel.wait()


def test_the_chart_computes_the_grace_period_with_the_server_s_own_defaults():
    """The chart writes MCP_SHUTDOWN_TIMEOUT_SECONDS and sizes the grace period from the
    server's defaults when the values leave them null: they must not drift apart."""
    import re
    from pathlib import Path

    helpers = (Path(_ROOT) / "charts/akko-mcp-trino/templates/_helpers.tpl").read_text()
    line = re.search(r'define "mcp-trino.serverDefaults".*?\n(.*?)\n', helpers, re.S).group(1)
    chart = {k: float(v) for k, v in re.findall(r'"(\w+)" (\d+)', line)}
    config = Config.from_env()
    for name in _SETTINGS + ("TRINO_REQUEST_TIMEOUT_SECONDS",):
        assert name not in os.environ, f"{name} is set in the environment of the tests"
    assert chart == {
        "callTimeoutSeconds": config.call_timeout_seconds,
        "queryTimeoutSeconds": config.query_timeout_seconds,
        "requestTimeoutSeconds": config.trino_request_timeout,
        "shutdownTimeoutSeconds": DEFAULT_SHUTDOWN_TIMEOUT_SECONDS,
    }
