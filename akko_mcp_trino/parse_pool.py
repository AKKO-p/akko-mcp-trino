"""Parse workers: an agent's SQL is parsed in other processes, within a deadline.

The parse time grows with some nesting and repetition, so no bound on the text alone bounds
it. A thread cannot be stopped; a process can. Each statement an agent sends is parsed, and
classified, in one of a few persistent worker processes (see
``akko_mcp_trino.parse_worker``), which answer plain values: the server's own process never
parses an agent's SQL.

* A worker is a fresh interpreter started by exec, never a fork of the server (which runs
  threads and an event loop). The workers start on first use. The protocol runs on the
  worker's standard input and output, two pipes of the server's; the worker takes them for
  itself at once and points its standard streams at the null device, so nothing it prints
  reaches the protocol or, over stdio, the server's own channel, which it never inherits.
  It gets no setting of the server's environment. Each message is a length-prefixed JSON
  frame of ``MAX_FRAME_BYTES`` at most, read by one thread per worker, so a deadline holds
  on Linux, macOS and Windows alike.
* A worker that has not answered within ``seconds`` is killed and replaced, and the call
  is refused (``time_budget``). A worker that died, or answered a frame too long or not a
  well-formed answer, is replaced and refuses the call too: never allowed.
* One statement at a time per caller, so one caller holds one worker at most: a second call
  of the same caller (an agent sends several tool calls at once; without a verified caller,
  every user shares one turn) waits until the first is answered. A call that has not had
  its turn within ``seconds`` is refused (``busy``).
* A call that has not had its turn and a free worker within ``seconds`` is refused
  (``time_budget``), so a call waits at most twice ``seconds``: for its turn and a worker,
  then for its answer.
* ``close`` (at exit, at the server's shutdown, and by ``close_all``) kills every worker. A
  worker whose server is gone sees its request pipe close and ends at once, mid-parse
  included.

Neither a refusal nor a metric carries the statement or a name.
"""

from __future__ import annotations

import atexit
import json
import os
import queue
import subprocess
import sys
import threading
import time
import weakref
from contextlib import suppress
from typing import Any, Callable, Optional

from .errors import BUSY, INVALID_ARGUMENT, TIME_BUDGET, UNAVAILABLE
from .parse_worker import MAX_FRAME_BYTES, read_frame, write_frame
from .sql_guard import Analysis, ColumnScope

DEFAULT_PARSE_SECONDS = 2.0
DEFAULT_PARSE_WORKERS = 2
# How long a worker is given to end once told to, before it is killed.
_STOP_SECONDS = 1.0
# The reasons a statement is refused before Trino, as the metric counts them.
REASONS = ("too_long", "too_nested", "caller_busy", "no_worker", "deadline", "worker_failed")
_NOTHING = Analysis(False, (), ColumnScope(False, ()))
# What a Windows interpreter needs from the environment to start at all.
_WINDOWS_ENV = ("SYSTEMROOT",)

Launcher = Callable[[], Any]


class ParseRefused(Exception):
    """The statement could not be checked: ``code`` for the audit, ``kind`` and the
    message for the agent, ``reason`` for the metric."""

    def __init__(self, reason: str, code: str, kind: str, message: str) -> None:
        """Keep the reason, the code and the kind beside the message."""
        super().__init__(message)
        self.reason, self.code, self.kind = reason, code, kind


def _package_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def launch_worker() -> subprocess.Popen:
    """Start one worker, its standard input and output on pipes of this process, its
    standard error on the null device, no other descriptor inherited. Its environment holds
    the path to this package, and on Windows ``SYSTEMROOT``, nothing else."""
    root = _package_root()
    env = {"PYTHONPATH": root}
    env.update({name: os.environ[name] for name in _WINDOWS_ENV if name in os.environ})
    return subprocess.Popen(
        [sys.executable, "-m", "akko_mcp_trino.parse_worker"],
        # On Windows, a worker of a server started without a console opens no window.
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        bufsize=0,
        cwd=root,
        env=env,
    )


class _Worker:
    """One worker process, and the thread that reads its frames into ``inbox``; None ends
    them: the worker ended, or wrote what is not a frame."""

    def __init__(self, launch: Launcher) -> None:
        self.process = launch()
        self.ready = False
        self.inbox: queue.Queue[Optional[bytes]] = queue.Queue()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self) -> None:
        with suppress(OSError, ValueError):
            while (frame := read_frame(self.process.stdout, MAX_FRAME_BYTES)) is not None:
                self.inbox.put(frame)
        self.inbox.put(None)

    def send(self, payload: bytes) -> None:
        """Send one frame; raises OSError or ValueError once the worker is gone."""
        write_frame(self.process.stdin, payload)

    def get(self, timeout: float) -> Optional[bytes]:
        """The next frame within ``timeout`` (raises ``queue.Empty`` past it), or None once
        the worker is broken; a broken worker stays broken."""
        frame = self.inbox.get(timeout=max(timeout, 0))
        if frame is None:
            self.inbox.put(None)
        return frame

    def stop(self) -> None:
        """Kill the process and close both pipes; once is enough, twice is harmless."""
        with suppress(OSError, ValueError):
            self.process.stdin.close()
        if self.process.poll() is None:
            with suppress(OSError):
                self.process.kill()
        try:
            self.process.wait(_STOP_SECONDS)
        except subprocess.TimeoutExpired:  # pragma: no cover - a killed process ends
            pass
        self.reader.join(_STOP_SECONDS)
        with suppress(OSError, ValueError):
            self.process.stdout.close()


def _analysis(raw: bytes) -> Optional[Analysis]:
    """The worker's answer as an ``Analysis``, every field checked: ``["error", null]`` is
    the analysis of a statement that names nothing and is no read; anything else that is
    not a well-formed answer is None, and breaks the worker."""
    try:
        status, value = json.loads(raw)
        if status == "error" and value is None:
            return _NOTHING
        if status != "ok":
            return None
        read_only, tables, (bare, scoped) = value
        if not (isinstance(read_only, bool) and isinstance(bare, bool)):
            return None
        names = tuple(str(t) for t in tables)
        scope = tuple((tuple(str(p) for p in parts), str(alias)) for parts, alias in scoped)
        return Analysis(read_only, names, ColumnScope(bare, scope))
    except Exception:  # noqa: BLE001 - any answer that cannot be read breaks the worker
        return None


def _is_ready(frame: Optional[bytes]) -> bool:
    """Whether ``frame`` is a worker's ``["ready", pid]``."""
    try:
        return frame is not None and json.loads(frame)[0] == "ready"
    except (ValueError, TypeError, IndexError, KeyError):
        return False


class ParsePool:
    """``workers`` parse workers, each statement within ``seconds`` (see the module)."""

    def __init__(
        self,
        workers: int = DEFAULT_PARSE_WORKERS,
        seconds: float = DEFAULT_PARSE_SECONDS,
        *,
        metrics: Any = None,
        launch: Launcher = launch_worker,
    ) -> None:
        """Nothing starts until the first statement."""
        self.workers, self.seconds = workers, seconds
        self._metrics, self._launch = metrics, launch
        # One lock, two conditions: a worker comes free (``_lock``), a caller's turn comes
        # (``_turn``).
        mutex = threading.RLock()
        self._lock = threading.Condition(mutex)
        self._turn = threading.Condition(mutex)
        self._idle: list[_Worker] = []
        self._all: set[_Worker] = set()
        self._callers: set[str] = set()
        self._started = self._closed = False
        _POOLS.add(self)

    def count(self, reason: str) -> None:
        """Count one refusal by its reason (``REASONS``)."""
        if self._metrics is not None:
            self._metrics.sql_refusals.labels(reason=reason).inc()

    def pids(self) -> list[int]:
        """The process ids of the workers running now."""
        with self._lock:
            return sorted(w.process.pid for w in self._all)

    def _refuse(self, reason: str, code: str, kind: str, message: str) -> ParseRefused:
        self.count(reason)
        return ParseRefused(reason, code, kind, message)

    def analyze(
        self,
        sql: str,
        session_catalog: Optional[str],
        extra_reads: frozenset[str],
        caller: str,
    ) -> Analysis:
        """The ``Analysis`` of ``sql``, from a worker, for ``caller`` (a verified subject, or
        "" for the configured account); raises ``ParseRefused``. The wait for the caller's
        turn and for a worker share one deadline."""
        deadline = time.monotonic() + self.seconds
        with self._lock:
            # An exception while waiting (none is expected in a worker thread) leaves the
            # turns as they were: this call has not taken one yet.
            while caller in self._callers:
                left = deadline - time.monotonic()
                if self._closed:
                    raise self._no_worker()
                if left <= 0:
                    raise self._refuse(
                        "caller_busy",
                        "parse_busy",
                        BUSY,
                        f"A statement of yours was still being checked after {self.seconds:g} "
                        "s: wait for its answer, then call again.",
                    )
                self._turn.wait(left)
            self._callers.add(caller)
        try:
            request = json.dumps([sql, session_catalog, sorted(extra_reads)]).encode()
            if len(request) > MAX_FRAME_BYTES:
                raise self._refuse(
                    "too_long",
                    "sql_too_long",
                    INVALID_ARGUMENT,
                    "The statement is too long for this server to check. Write a shorter "
                    "statement.",
                )
            return self._ask(self._take(deadline), request)
        finally:
            with self._lock:
                self._callers.discard(caller)
                self._turn.notify_all()

    def _no_worker(self) -> ParseRefused:
        return self._refuse(
            "no_worker",
            "parse_no_worker",
            TIME_BUDGET,
            f"No worker could check the statement within {self.seconds:g} s; the server is "
            "busy. Call again in a few seconds.",
        )

    def _failed(self) -> ParseRefused:
        return self._refuse(
            "worker_failed",
            "parse_failed",
            UNAVAILABLE,
            "The statement could not be checked; it was not run. Call again.",
        )

    def _start(self) -> None:
        """Mark the pool started, the lock held; ``_refill`` starts the workers."""
        self._started = True
        atexit.register(self.close)

    def _refill(self) -> None:
        """Start workers, the lock held, until there are ``workers`` of them or one cannot
        start: a launch that failed is tried again by the next call, so a passing failure
        (memory, a process limit) never leaves the pool short for good."""
        while not self._closed and len(self._all) < self.workers:
            if not self._add():
                return

    def _drop_dead(self) -> None:
        """Take out of the pool, the lock held, every idle worker whose process has ended,
        so that no call is refused for a worker that died while it waited."""
        for worker in [w for w in self._idle if w.process.poll() is not None]:
            self._idle.remove(worker)
            self._all.discard(worker)
            worker.stop()

    def _add(self) -> bool:
        try:
            worker = _Worker(self._launch)
        except Exception:  # noqa: BLE001 - fewer workers: the next call tries again
            return False
        self._all.add(worker)
        self._idle.append(worker)
        self._lock.notify()
        return True

    def _take(self, deadline: float) -> _Worker:
        """A free worker, ready, before ``deadline``; raises ``ParseRefused``."""
        with self._lock:
            if not self._started and not self._closed:
                self._start()
            self._drop_dead()
            self._refill()
            while not self._idle:
                left = deadline - time.monotonic()
                if self._closed or left <= 0:
                    raise self._no_worker()
                self._lock.wait(left)
            ready = [w for w in self._idle if w.ready]
            worker = ready[0] if ready else self._idle[0]
            self._idle.remove(worker)
        if not worker.ready:
            try:
                hello = worker.get(deadline - time.monotonic())
            except queue.Empty:
                self._release(worker)  # still starting: kept for the next call
                raise self._no_worker() from None
            except BaseException:
                self._release(worker)  # never lost to the pool
                raise
            if not _is_ready(hello):
                self._replace(worker)
                raise self._failed()
            worker.ready = True
        return worker

    def _ask(self, worker: _Worker, request: bytes) -> Analysis:
        """The worker's analysis of ``request``. On any way out but a well-formed answer,
        an unexpected exception included, the worker is replaced: it never goes back to the
        pool, nor stays counted in it."""
        try:
            try:
                worker.send(request)
                raw = worker.get(self.seconds)
            except queue.Empty:
                raise self._refuse(
                    "deadline",
                    "parse_deadline",
                    TIME_BUDGET,
                    f"The statement took more than {self.seconds:g} s to check and was not "
                    "run. Write a simpler statement: fewer nested or repeated expressions.",
                ) from None
            except (OSError, ValueError):
                raw = None
            found = _analysis(raw) if raw is not None else None
            if found is None:
                raise self._failed()
        except BaseException:
            self._replace(worker)
            raise
        self._release(worker)
        return found

    def _release(self, worker: _Worker) -> None:
        with self._lock:
            if not self._closed:
                self._idle.append(worker)
                self._lock.notify()
                return
        worker.stop()

    def _replace(self, worker: _Worker) -> None:
        worker.stop()
        with self._lock:
            self._all.discard(worker)
            if not self._closed:
                self._add()

    def close(self) -> None:
        """Kill every worker; a later statement is refused. Idempotent."""
        with self._lock:
            self._closed = True
            workers = list(self._all)
            self._all.clear()
            self._idle.clear()
            self._lock.notify_all()
            self._turn.notify_all()
        for worker in workers:
            worker.stop()


_POOLS: "weakref.WeakSet[ParsePool]" = weakref.WeakSet()
_SHARED: dict[tuple[int, float], ParsePool] = {}
_SHARED_LOCK = threading.Lock()


def shared_pool(
    workers: int = DEFAULT_PARSE_WORKERS, seconds: float = DEFAULT_PARSE_SECONDS
) -> ParsePool:
    """The pool of the process for these settings, made on first use: what the tools use
    when they are given none."""
    with _SHARED_LOCK:
        pool = _SHARED.get((workers, seconds))
        if pool is None or pool._closed:
            pool = _SHARED[(workers, seconds)] = ParsePool(workers, seconds)
        return pool


def close_all() -> None:
    """Close every pool of the process: the server's shutdown."""
    for pool in list(_POOLS):
        pool.close()
