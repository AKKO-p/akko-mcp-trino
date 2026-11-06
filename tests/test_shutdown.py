"""A stop closes the context: the real server process, a real signal, a real provider.

Kubernetes stops a pod with SIGTERM. Over HTTP, uvicorn shuts the application down
(its lifespan) and then lets the signal end the process; over stdio, the server
closes the context and exits 0. A normal end and a Ctrl+C close it too, once.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

_SERVER = os.path.join(os.path.dirname(__file__), "shutdown_server.py")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _until(condition, seconds=30.0):
    end = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < end, "condition not reached"
        time.sleep(0.05)


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
            "MCP_CONTEXT_PROVIDERS": "marker",
            "SHUTDOWN_MARKER": str(tmp_path / "closed"),
            "SHUTDOWN_READY": str(tmp_path / "ready"),
            **extra,
        }
    )
    return env


def _closings(tmp_path, line="closed") -> int:
    marker = tmp_path / "closed"
    return marker.read_text().splitlines().count(line) if marker.exists() else 0


def _healthy(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as res:
            return res.status == 200
    except OSError:
        return False


def _listening(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def _http_server(tmp_path, transport):
    port, health = _free_port(), _free_port()
    env = _env(tmp_path, transport, MCP_PORT=str(port), MCP_HEALTH_PORT=str(health))
    proc = subprocess.Popen(
        [sys.executable, _SERVER], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
    )
    try:
        _until(lambda: _healthy(health) and _listening(port))
    except AssertionError:
        proc.kill()
        raise AssertionError(proc.communicate(timeout=10)[1].decode()) from None
    return proc


def _stdio_server(tmp_path, **extra):
    proc = subprocess.Popen(
        [sys.executable, _SERVER],
        env=_env(tmp_path, "stdio", **extra),
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        _until(lambda: (tmp_path / "ready").exists())
    except AssertionError:
        proc.kill()
        raise AssertionError(proc.communicate(timeout=10)[1].decode()) from None
    time.sleep(0.5)  # past the start of the stdio loop
    return proc


def _stop(proc, sig):
    proc.send_signal(sig)
    try:
        _, err = proc.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise
    return proc.returncode, err.decode()


_HTTP = pytest.mark.parametrize("transport", ["streamable-http", "sse"])


@_HTTP
def test_sigterm_closes_the_context_over_http(tmp_path, transport):
    code, err = _stop(_http_server(tmp_path, transport), signal.SIGTERM)
    assert _closings(tmp_path) == 1, err
    assert code == -signal.SIGTERM, "uvicorn lets the signal end the process after shutdown"


@_HTTP
def test_ctrl_c_closes_the_context_over_http_once(tmp_path, transport):
    code, err = _stop(_http_server(tmp_path, transport), signal.SIGINT)
    assert _closings(tmp_path) == 1, err
    assert code == 0, err


def test_sigterm_closes_the_context_over_stdio_and_exits_0(tmp_path):
    code, err = _stop(_stdio_server(tmp_path), signal.SIGTERM)
    assert _closings(tmp_path) == 1, err
    assert code == 0, err


def test_the_end_of_stdin_closes_the_context_over_stdio(tmp_path):
    proc = _stdio_server(tmp_path)
    _, err = proc.communicate(input=b"", timeout=30)
    assert _closings(tmp_path) == 1, err.decode()
    assert proc.returncode == 0, err.decode()


def test_sigterm_during_the_close_over_stdio_starts_no_second_close(tmp_path):
    proc = _stdio_server(tmp_path, SHUTDOWN_CLOSE_SECONDS="2")
    proc.stdin.close()  # the end of stdin starts the close
    _until(lambda: _closings(tmp_path, "closing") == 1)
    proc.send_signal(signal.SIGTERM)
    try:
        code = proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise
    err = proc.stderr.read().decode()
    assert (_closings(tmp_path, "closing"), _closings(tmp_path)) == (1, 1), err
    assert code == 0, err


def test_sigterm_without_a_provider_over_stdio_ends_the_process_as_in_0_7_2(tmp_path):
    code, err = _stop(_stdio_server(tmp_path, MCP_CONTEXT_PROVIDERS=""), signal.SIGTERM)
    assert _closings(tmp_path, "closing") == 0, err
    assert code == -signal.SIGTERM, err
