"""A parse worker: the process that parses an agent's SQL, so the server never does.

The server starts each worker as a fresh interpreter (``python -m
akko_mcp_trino.parse_worker``), never as a fork of itself, with its standard input and
output on two pipes of the server's and its standard error on the null device. Its first
instructions take those two pipes for the protocol, on descriptors of their own, and point
descriptors 0, 1 and 2, and ``sys.stdin``, ``sys.stdout`` and ``sys.stderr``, at the null
device: whatever a library prints can reach neither the protocol nor, over stdio, the
server's own channel. The same code runs on Linux, macOS and Windows.

Each message is a frame: its length on four bytes (big-endian), then that many bytes of
JSON, ``MAX_FRAME_BYTES`` at most. A request is ``[sql, session_catalog, extra_reads]``;
the answer is ``["ok", [read_only, tables, [bare, [[parts, alias], ...]]]]`` (an
``Analysis``), or ``["error", null]`` when the analysis itself failed. JSON, not pickle:
the server reads nothing from a worker but plain values. The worker first says
``["ready", pid]`` once its imports are done, the parser's included. It ends as soon as its
request pipe closes, or carries something that is not a frame, in the middle of a parse
included: a server that is gone has closed it.
"""

from __future__ import annotations

import json
import os
import queue
import struct
import sys
import threading
from typing import Any, BinaryIO, Callable, Optional

# The largest frame either side reads or writes; a longer one breaks the worker.
MAX_FRAME_BYTES = 16 * 1024 * 1024
_HEADER = struct.Struct(">I")


def _read_exactly(stream: BinaryIO, size: int) -> Optional[bytes]:
    chunks, left = [], size
    while left:
        chunk = stream.read(left)
        if not chunk:
            return None
        chunks.append(chunk)
        left -= len(chunk)
    return b"".join(chunks)


def read_frame(stream: BinaryIO, limit: int = MAX_FRAME_BYTES) -> Optional[bytes]:
    """The next frame of ``stream``; None at its end, on a short frame, or on a frame
    longer than ``limit``."""
    head = _read_exactly(stream, _HEADER.size)
    if head is None:
        return None
    (size,) = _HEADER.unpack(head)
    if size > limit:
        return None
    return _read_exactly(stream, size)


def write_frame(stream: BinaryIO, payload: bytes) -> None:
    """Write ``payload`` to ``stream`` as one frame, at once."""
    stream.write(_HEADER.pack(len(payload)) + payload)
    stream.flush()


def answer(request: bytes) -> bytes:
    """The answer to one request: the analysis, or an error that names nothing."""
    try:
        from .sql_guard import analyze  # imported once the streams are the null device

        sql, session_catalog, extra_reads = json.loads(request)
        found = analyze(sql, session_catalog, frozenset(extra_reads))
        scope = [found.scope.bare, [[list(parts), alias] for parts, alias in found.scope.tables]]
        return json.dumps(["ok", [found.read_only, list(found.tables), scope]]).encode()
    except Exception:  # noqa: BLE001 - the server refuses the call: fail closed
        return b'["error", null]'


def serve(requests: BinaryIO, answers: BinaryIO, exit: Callable[[int], Any] = os._exit) -> None:
    """Answer each request in turn. A thread reads the requests: at the end of the request
    pipe, or on what is not a frame, it calls ``exit`` at once, a parse under way or not."""
    inbox: queue.Queue[Optional[bytes]] = queue.Queue()

    def read() -> None:
        while (frame := read_frame(requests)) is not None:
            inbox.put(frame)
        inbox.put(None)
        exit(0)

    threading.Thread(target=read, daemon=True).start()
    try:
        write_frame(answers, json.dumps(["ready", os.getpid()]).encode())
        while (request := inbox.get()) is not None:
            write_frame(answers, answer(request))
    except (OSError, ValueError):  # the server closed the answer pipe
        return


def isolate_streams() -> tuple[BinaryIO, BinaryIO]:  # pragma: no cover - run in a worker
    """Take standard input and output for the protocol, on descriptors of their own, then
    point descriptors 0, 1 and 2 and the ``sys`` streams at the null device; returns the
    request and answer streams. Proven by running it (``test_parse_pool``)."""
    request_fd, answer_fd = os.dup(0), os.dup(1)
    if sys.platform == "win32":
        import msvcrt

        for fd in (request_fd, answer_fd):
            msvcrt.setmode(fd, os.O_BINARY)
    null = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(null, fd)
    os.close(null)
    sys.stdin = open(os.devnull, encoding="utf-8")
    sys.stdout = sys.stderr = open(os.devnull, "w", encoding="utf-8")
    return os.fdopen(request_fd, "rb", buffering=0), os.fdopen(answer_fd, "wb", buffering=0)


if __name__ == "__main__":  # pragma: no cover - process glue, proven by running it
    streams = isolate_streams()
    import akko_mcp_trino.sql_guard  # noqa: F401 - the parser, loaded before "ready"

    serve(*streams)
