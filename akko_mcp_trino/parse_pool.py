"""Parse workers: an agent's SQL is parsed in other processes, within a deadline.

The parser's time grows exponentially with some nesting, and with the repetition of what
it allows, so no bound on the text alone bounds it. A thread cannot be stopped; a process
can. Each statement an agent sends is parsed, and classified, in one of a few persistent
worker processes (see ``akko_mcp_trino.parse_worker``), which answer plain values: the
server's own process never parses an agent's SQL.

* A worker is a fresh interpreter started by exec, never a fork of the server (which runs
  threads and an event loop). The workers start on first use. Their standard input, output
  and error are the null device, and they get no setting of the server's environment.
* A worker that has not answered within ``seconds`` is killed and replaced, and the call
  is refused (``time_budget``). A worker that died or answered nonsense refuses the call
  too: never allowed.
* One statement at a time per caller: a second call of the same caller while the first is
  checked is refused at once (``busy``), so one caller holds one worker at most.
* A call that finds no worker free within ``seconds`` is refused (``time_budget``).
* ``close`` (at exit, at the server's shutdown, and by ``close_all``) kills every worker. A
  worker whose server is gone ends on its own, within half a second, mid-parse included.

Neither a refusal nor a metric carries the statement or a name.
"""

from __future__ import annotations

import atexit
import json
import os
import subprocess
import sys
import threading
import time
import weakref
from multiprocessing.connection import Connection
from typing import Any, Callable, Optional

from .errors import BUSY, TIME_BUDGET, UNAVAILABLE
from .sql_guard import Analysis, ColumnScope

DEFAULT_PARSE_SECONDS = 2.0
DEFAULT_PARSE_WORKERS = 2
# How long a worker is given to end once told to, before it is killed.
_STOP_SECONDS = 1.0
# The reasons a statement is refused before Trino, as the metric counts them.
REASONS = ("too_long", "too_nested", "caller_busy", "no_worker", "deadline", "worker_failed")
_NOTHING = Analysis(False, (), ColumnScope(False, ()))

Launcher = Callable[[], tuple[Any, Connection, Connection]]


class ParseRefused(Exception):
    """The statement could not be checked: ``code`` for the audit, ``kind`` and the
    message for the agent, ``reason`` for the metric."""

    def __init__(self, reason: str, code: str, kind: str, message: str) -> None:
        """Keep the reason, the code and the kind beside the message."""
        super().__init__(message)
        self.reason, self.code, self.kind = reason, code, kind


def _package_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def launch_worker() -> tuple[Any, Connection, Connection]:
    """Start one worker: its process, the pipe its requests go into, the pipe its answers
    come from. Its environment holds the path to this package, nothing else."""
    request_r, request_w = os.pipe()
    answer_r, answer_w = os.pipe()
    root = _package_root()
    try:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "akko_mcp_trino.parse_worker",
                str(request_r),
                str(answer_w),
                str(os.getpid()),
            ],
            pass_fds=(request_r, answer_w),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=root,
            env={"PYTHONPATH": root},
        )
    except BaseException:
        os.close(request_w)
        os.close(answer_r)
        raise
    finally:
        os.close(request_r)
        os.close(answer_w)
    return process, Connection(request_w, readable=False), Connection(answer_r, writable=False)


class _Worker:
    def __init__(self, launch: Launcher) -> None:
        self.process, self.requests, self.answers = launch()
        self.ready = False

    def stop(self) -> None:
        """Kill the process and close both pipes; once is enough, twice is harmless."""
        for conn in (self.requests, self.answers):
            conn.close()
        if self.process.poll() is None:
            self.process.kill()
        try:
            self.process.wait(_STOP_SECONDS)
        except subprocess.TimeoutExpired:  # pragma: no cover - a killed process ends
            pass


def _analysis(raw: bytes) -> Analysis:
    """The worker's answer as an ``Analysis``, every field checked; anything else is the
    analysis of a statement that names nothing and is no read."""
    try:
        status, value = json.loads(raw)
        if status != "ok":
            return _NOTHING
        read_only, tables, (bare, scoped) = value
        if not (isinstance(read_only, bool) and isinstance(bare, bool)):
            return _NOTHING
        names = tuple(str(t) for t in tables)
        scope = tuple((tuple(str(p) for p in parts), str(alias)) for parts, alias in scoped)
        return Analysis(read_only, names, ColumnScope(bare, scope))
    except (TypeError, ValueError):
        return _NOTHING


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
        self._lock = threading.Condition()
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
        "" for the configured account); raises ``ParseRefused``."""
        with self._lock:
            if caller in self._callers:
                raise self._refuse(
                    "caller_busy",
                    "parse_busy",
                    BUSY,
                    "A statement of yours is still being checked: wait for its answer, "
                    "then call again.",
                )
            self._callers.add(caller)
        try:
            worker = self._take(time.monotonic() + self.seconds)
            request = json.dumps([sql, session_catalog, sorted(extra_reads)]).encode()
            return self._ask(worker, request)
        finally:
            with self._lock:
                self._callers.discard(caller)

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
        """Start the workers, the lock held; one that cannot start is left out."""
        self._started = True
        atexit.register(self.close)
        for _ in range(self.workers):
            self._add()

    def _add(self) -> None:
        try:
            worker = _Worker(self._launch)
        except Exception:  # noqa: BLE001 - fewer workers: calls wait, or are refused
            return
        self._all.add(worker)
        self._idle.append(worker)
        self._lock.notify()

    def _take(self, deadline: float) -> _Worker:
        """A free worker, ready, before ``deadline``; raises ``ParseRefused``."""
        with self._lock:
            if not self._started and not self._closed:
                self._start()
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
                if not worker.answers.poll(max(deadline - time.monotonic(), 0)):
                    self._release(worker)
                    raise self._no_worker()
                worker.answers.recv_bytes()
            except (EOFError, OSError):
                self._replace(worker)
                raise self._failed() from None
            worker.ready = True
        return worker

    def _ask(self, worker: _Worker, request: bytes) -> Analysis:
        try:
            worker.requests.send_bytes(request)
            if not worker.answers.poll(self.seconds):
                self._replace(worker)
                raise self._refuse(
                    "deadline",
                    "parse_deadline",
                    TIME_BUDGET,
                    f"The statement took more than {self.seconds:g} s to check and was not "
                    "run. Write a simpler statement: fewer nested or repeated expressions.",
                )
            raw = worker.answers.recv_bytes()
        except (EOFError, OSError):
            self._replace(worker)
            raise self._failed() from None
        self._release(worker)
        return _analysis(raw)

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
