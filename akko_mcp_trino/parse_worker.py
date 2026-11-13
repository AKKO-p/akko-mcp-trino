"""A parse worker: the process that parses an agent's SQL, so the server never does.

The server starts each worker as a fresh interpreter (``python -m
akko_mcp_trino.parse_worker <request fd> <answer fd> <server pid>``), never as a fork of
itself. Its standard input, output and error are the null device: over stdio, the server's
own standard output is the protocol channel, and a single stray byte would break it. The
requests and the answers go through two pipes of their own.

A request is the JSON ``[sql, session_catalog, extra_reads]``; the answer is the JSON
``["ok", [read_only, tables, [bare, [[parts, alias], ...]]]]`` (an ``Analysis``), or
``["error", null]`` when the analysis itself failed. JSON, not pickle, both ways: the server
reads nothing from a worker but plain values. The worker first says ``["ready", pid]`` once
its imports are done. It ends when the request pipe closes,
and on its own when the server's process is gone, even in the middle of a parse.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from multiprocessing.connection import Connection
from typing import Any, Callable

from .sql_guard import analyze

# How often a worker checks that the server that started it still runs.
PARENT_CHECK_SECONDS = 0.5


def answer(request: bytes) -> bytes:
    """The answer to one request: the analysis, or an error that names nothing."""
    try:
        sql, session_catalog, extra_reads = json.loads(request)
        found = analyze(sql, session_catalog, frozenset(extra_reads))
        scope = [found.scope.bare, [[list(parts), alias] for parts, alias in found.scope.tables]]
        return json.dumps(["ok", [found.read_only, list(found.tables), scope]]).encode()
    except Exception:  # noqa: BLE001 - the server refuses the call: fail closed
        return b'["error", null]'


def watch_parent(
    parent: int,
    stop: threading.Event,
    exit: Callable[[int], Any] = os._exit,
    every: float = PARENT_CHECK_SECONDS,
    getppid: Callable[[], int] = os.getppid,
) -> None:
    """End the process once ``parent`` is no longer its parent: the server is gone, so
    nobody waits for the parse under way. Returns once ``stop`` is set."""
    while getppid() == parent:
        if stop.wait(every):
            return
    exit(0)


def serve(requests: Connection, answers: Connection, parent: int, **watch: Any) -> None:
    """Answer each request in turn until the request pipe closes."""
    stop = threading.Event()
    watcher = threading.Thread(target=watch_parent, args=(parent, stop), kwargs=watch, daemon=True)
    watcher.start()
    try:
        answers.send_bytes(json.dumps(["ready", os.getpid()]).encode())
        while True:
            try:
                request = requests.recv_bytes()
            except (EOFError, OSError):
                return
            answers.send_bytes(answer(request))
    finally:
        stop.set()


def main(argv: list[str]) -> None:  # pragma: no cover - process glue, proven by running it
    """Point the standard streams at the null device, then serve the two pipes."""
    null = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(null, fd)
    request_fd, answer_fd, parent = (int(a) for a in argv)
    serve(
        Connection(request_fd, writable=False),
        Connection(answer_fd, readable=False),
        parent,
    )


if __name__ == "__main__":  # pragma: no cover
    main(sys.argv[1:])
