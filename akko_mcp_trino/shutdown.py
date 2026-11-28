"""A clean stop over HTTP: no call cut short, no query left running in Trino.

Kubernetes stops a pod with SIGTERM, removes it from the Service's endpoints at the same
moment, and kills it once the grace period (``terminationGracePeriodSeconds``) is over. The
endpoints take a few seconds to leave every load balancer, and a client keeps the connection
it has. So, on SIGTERM:

1. For ``MCP_SHUTDOWN_DELAY_SECONDS`` the server serves as before, but every answer closes
   its connection (``Connection: close``): each client opens a new one for its next request,
   which the load balancer sends to another replica. No connection is closed under a client
   that is sending a request on it.
2. Then it stops accepting connections, closes the idle ones, and gives the calls in flight
   ``MCP_SHUTDOWN_TIMEOUT_SECONDS`` to end (uvicorn's graceful shutdown).
3. Past that, the calls still running are stopped and their Trino queries are cancelled in
   Trino (`TrinoClient.stop_queries`), before the process ends: none is left to run until
   Trino's own limit. The grace period must cover the three: the chart computes it.

A second SIGTERM, or SIGINT (Ctrl+C), stops at once, as uvicorn does.

The streamable HTTP transport answers in Server-Sent Events through sse-starlette, which
replaces uvicorn's signal handler to end every event stream at the first SIGTERM: a call in
flight would lose its answer at once, whatever time it had left. ``serve`` turns that off, and
the streams still open when the timeout runs out are stopped with the calls (step 3).
"""

from __future__ import annotations

import logging
import signal
import threading
from types import FrameType
from typing import Any, Awaitable, Callable, MutableMapping

import uvicorn

_Message = MutableMapping[str, Any]
_Receive = Callable[[], Awaitable[_Message]]
_Send = Callable[[_Message], Awaitable[None]]

log = logging.getLogger(__name__)


class Drain:
    """Whether the server is stopping: once ``begin`` is called, every answer closes its
    connection."""

    def __init__(self) -> None:
        """A server serving as usual."""
        self._closing = threading.Event()

    def begin(self) -> None:
        """From now on, every answer closes its connection."""
        self._closing.set()

    def closing(self) -> bool:
        """Whether ``begin`` was called."""
        return self._closing.is_set()


class CloseConnections:
    """ASGI middleware: once the drain has begun, an answer carries ``Connection: close``,
    and the HTTP server closes the connection after it. Checked when the answer starts, so a
    long call that began before the stop closes its connection too."""

    def __init__(self, app: Any, drain: Drain) -> None:
        """Wrap ``app``; ``drain`` says when the stop began."""
        self._app, self._drain = app, drain

    async def __call__(self, scope: _Message, receive: _Receive, send: _Send) -> None:
        """Serve ``scope``; past the start of the drain, the answer closes its connection."""
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        async def send_closing(message: _Message) -> None:
            if message["type"] == "http.response.start" and self._drain.closing():
                headers = [
                    (name, value)
                    for name, value in message.get("headers", [])
                    if name.lower() != b"connection"
                ]
                message = {**message, "headers": [*headers, (b"connection", b"close")]}
            await send(message)

        await self._app(scope, receive, send_closing)


class _Server(uvicorn.Server):
    """uvicorn's server, whose first SIGTERM starts the drain and stops ``delay`` seconds
    later."""

    def __init__(self, config: uvicorn.Config, drain: Drain, delay: float) -> None:
        super().__init__(config)
        self._drain, self._delay = drain, delay

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        if sig != signal.SIGTERM or self._drain.closing():
            super().handle_exit(sig, frame)
            return
        self._drain.begin()
        log.info(
            "SIGTERM: serving %g s more, each answer closing its connection, then stopping",
            self._delay,
        )
        if self._delay <= 0:
            super().handle_exit(sig, frame)
            return
        stop = threading.Timer(self._delay, super().handle_exit, (sig, frame))
        stop.daemon = True
        stop.start()


def _keep_event_streams_on_sigterm() -> None:
    """Stop sse-starlette from ending every event stream at the first SIGTERM."""
    try:
        from sse_starlette.sse import AppStatus
    except ImportError:  # pragma: no cover - an SDK without sse-starlette ends no stream
        return
    AppStatus.disable_automatic_graceful_drain()


def serve(  # pragma: no cover - process glue, proven by tests/test_instances.py
    app: Any,
    *,
    host: str,
    port: int,
    drain: Drain,
    delay: float,
    timeout: float,
    log_level: str = "warning",
) -> None:
    """Serve ``app`` until a signal stops it: on SIGTERM, ``delay`` seconds of answers that
    close their connection, then at most ``timeout`` seconds for the calls in flight; the
    app's shutdown (its lifespan) runs last. Returns once the server has stopped."""
    _keep_event_streams_on_sigterm()
    config = uvicorn.Config(
        CloseConnections(app, drain),
        host=host,
        port=port,
        log_level=log_level,
        timeout_graceful_shutdown=timeout,  # type: ignore[arg-type]  # uvicorn types it int
    )
    try:
        _Server(config, drain, delay).run()
    except KeyboardInterrupt:
        pass  # Ctrl+C: uvicorn raises SIGINT again once it has stopped, as uvicorn.run does
