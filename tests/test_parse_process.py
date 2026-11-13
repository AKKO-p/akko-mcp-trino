"""The parse workers with the real server process: over stdio a statement that would keep
the parser busy for minutes leaves the session valid, and no worker outlives its server.

Each line the server writes on stdout is read as JSON-RPC: a single stray byte from a
worker would fail the test. Linux only: a worker's state is read in /proc.
"""

from __future__ import annotations

import json
import os
import queue
import signal
import socket
import subprocess
import sys
import threading
import time

import pytest

_SERVER = os.path.join(os.path.dirname(__file__), "parse_server.py")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STUCK = "SELECT " + "ARRAY[" * 24 + "1" + "]" * 24

pytestmark = pytest.mark.skipif(not os.path.isdir("/proc/self"), reason="reads /proc")


def _env(tmp_path, transport, **extra):
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("MCP_", "TRINO_", "OIDC_"))
    }
    env.update(
        {
            "PYTHONPATH": _ROOT,
            "MCP_TRANSPORT": transport,
            "MCP_AUTH_ENABLED": "false",
            "MCP_AUTH_REQUIRED": "false",
            "TRINO_HOST": "trino.invalid",
            "TRINO_IDENTITY_MODE": "service",
            "MCP_SQL_PARSE_SECONDS": "2",
            "MCP_MAX_SQL_NESTING": "64",
            "PARSE_PIDS": str(tmp_path / "pids"),
            **extra,
        }
    )
    return env


def _pids(tmp_path):
    path = tmp_path / "pids"
    return [int(p) for p in path.read_text().split()] if path.exists() else []


def _gone(pid):
    """Ended: no such process, or a zombie nobody reaped yet."""
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as stat:
            return stat.read().rsplit(")", 1)[1].split()[0] == "Z"
    except FileNotFoundError:
        return True


def _until(condition, seconds=30.0):
    end = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < end, "condition not reached"
        time.sleep(0.05)


class _Stdio:
    """The server over stdio, spoken to line by line; every line it writes must be JSON."""

    def __init__(self, env):
        self.proc = subprocess.Popen(
            [sys.executable, _SERVER],
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self.lines: queue.Queue = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.proc.stdout:
            self.lines.put(line)
        self.lines.put(None)

    def send(self, message):
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", **message}).encode() + b"\n")
        self.proc.stdin.flush()

    def answer(self, request_id):
        while True:
            line = self.lines.get(timeout=60)
            assert line is not None, "the server closed stdout"
            message = json.loads(line)  # a stray byte fails here
            assert message["jsonrpc"] == "2.0"
            if message.get("id") == request_id:
                return message

    def call(self, request_id, sql):
        self.send(
            {
                "id": request_id,
                "method": "tools/call",
                "params": {"name": "execute_query", "arguments": {"sql": sql}},
            }
        )
        result = self.answer(request_id)["result"]
        return json.loads(result["content"][0]["text"])


def test_a_statement_past_the_deadline_over_stdio_leaves_the_session_valid(tmp_path):
    from mcp.types import LATEST_PROTOCOL_VERSION

    server = _Stdio(_env(tmp_path, "stdio"))
    server.send(
        {
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": LATEST_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0"},
            },
        }
    )
    assert "result" in server.answer(1)
    server.send({"method": "notifications/initialized"})
    refused = server.call(2, STUCK)
    assert refused["kind"] == "time_budget"
    assert server.call(3, "SELECT 1")["rows"] == [[1]]
    server.send({"id": 4, "method": "tools/list"})
    assert "execute_query" in {t["name"] for t in server.answer(4)["result"]["tools"]}
    pids = _pids(tmp_path)
    assert pids, "the workers ran"
    server.proc.stdin.close()  # the end of stdin ends the server normally
    assert server.proc.wait(30) == 0
    while (line := server.lines.get(timeout=10)) is not None:
        json.loads(line)
    _until(lambda: all(_gone(pid) for pid in pids), 5)


def test_sigterm_over_stdio_leaves_no_worker(tmp_path):
    proc = subprocess.Popen(
        [sys.executable, _SERVER],
        env=_env(tmp_path, "stdio", PARSE_WARM="1"),
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _until(lambda: len(_pids(tmp_path)) == 2)
    pids = _pids(tmp_path)
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(30) == -signal.SIGTERM
    _until(lambda: all(_gone(pid) for pid in pids), 5)


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_sigterm_over_http_closes_the_workers_at_shutdown(tmp_path):
    port, health = _free_port(), _free_port()
    proc = subprocess.Popen(
        [sys.executable, _SERVER],
        env=_env(
            tmp_path,
            "streamable-http",
            PARSE_WARM="1",
            MCP_PORT=str(port),
            MCP_HEALTH_PORT=str(health),
        ),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _until(lambda: len(_pids(tmp_path)) == 2)
        pids = _pids(tmp_path)
        proc.send_signal(signal.SIGTERM)
        proc.wait(30)
    finally:
        if proc.poll() is None:
            proc.kill()
    _until(lambda: all(_gone(pid) for pid in pids), 5)
