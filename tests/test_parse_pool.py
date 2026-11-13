"""akko_mcp_trino.parse_pool and parse_worker: an agent's SQL is parsed in worker
processes, within a deadline, one statement at a time per caller.

``STUCK`` is a statement whose parse outlasts every deadline here, so the deadline, not the
statement, ends each test that sends it.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import struct
import subprocess
import sys
import threading
import time

import pytest

from akko_mcp_trino import parse_pool, parse_worker
from akko_mcp_trino.metrics import Metrics
from akko_mcp_trino.parse_pool import ParsePool, ParseRefused
from akko_mcp_trino.sql_guard import Analysis, ColumnScope

STUCK = "SELECT " + "ARRAY[" * 24 + "1" + "]" * 24
_PROC = os.path.isdir("/proc/self/fd")


def _refusals(metrics, reason):
    return metrics.registry.get_sample_value("mcp_trino_sql_refusals_total", {"reason": reason})


def _analyze(pool, sql, caller="alice"):
    return pool.analyze(sql, "c", frozenset(), caller)


def _in_thread(fn):
    out = {}

    def run():
        try:
            out["value"] = fn()
        except ParseRefused as e:
            out["refused"] = e
        out["at"] = time.monotonic()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, out


def _processes(pool):
    """The process objects of the pool's workers, to see them end on any platform."""
    with pool._lock:
        return [w.process for w in pool._all]


def _until(condition, seconds=10.0):
    end = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < end, "condition not reached"
        time.sleep(0.01)


def test_a_worker_parses_and_classifies_into_plain_values(monkeypatch):
    """The server's process never parses: its sqlglot.parse fails here, the worker's does not."""
    from akko_mcp_trino import sql_guard

    def no_parse(*args, **kwargs):
        raise AssertionError("the server's process parsed an agent's statement")

    monkeypatch.setattr(sql_guard.sqlglot, "parse", no_parse)
    pool = ParsePool(1, 10.0)
    found = _analyze(pool, "SELECT x.a FROM c.s.t x")
    assert found == Analysis(True, ("c.s.t",), ColumnScope(True, ((("c", "s", "t"), "x"),)))
    assert _analyze(pool, "DROP TABLE c.s.t").read_only is False
    assert _analyze(pool, "SELECT FROM WHERE (((") == Analysis(False, (), ColumnScope(False, ()))


def test_a_parse_past_the_deadline_is_killed_replaced_and_refused():
    metrics = Metrics()
    pool = ParsePool(1, 1.0, metrics=metrics)
    _analyze(pool, "SELECT 1")  # the worker is started and ready
    (first,) = pool.pids()
    (old,) = _processes(pool)
    started = time.monotonic()
    with pytest.raises(ParseRefused) as refused:
        _analyze(pool, STUCK)
    assert time.monotonic() - started < 1.0 + 0.5
    assert (refused.value.kind, refused.value.code) == ("time_budget", "parse_deadline")
    assert "ARRAY" not in str(refused.value) and "1 s" in str(refused.value)
    _until(lambda: pool.pids() and pool.pids() != [first])
    assert old.poll() is not None, "the worker past the deadline was killed"
    assert _analyze(pool, "SELECT 1").read_only is True, "the replacement serves"
    assert _refusals(metrics, "deadline") == 1


def test_a_worker_that_dies_refuses_the_call_and_never_allows_it():
    metrics = Metrics()
    pool = ParsePool(1, 30.0, metrics=metrics)
    _analyze(pool, "SELECT 1")
    (process,) = _processes(pool)
    thread, out = _in_thread(lambda: _analyze(pool, STUCK))
    time.sleep(0.3)  # the worker is parsing
    process.kill()
    thread.join(10)
    assert "value" not in out
    assert (out["refused"].kind, out["refused"].code) == ("unavailable", "parse_failed")
    assert _refusals(metrics, "worker_failed") == 1
    assert _analyze(pool, "SELECT 1").read_only is True


def test_one_statement_at_a_time_per_caller_and_other_callers_still_served():
    """alice holds a worker with a statement that never ends: her second call is refused
    at once, and bob's statement is served on the other worker meanwhile."""
    metrics = Metrics()
    pool = ParsePool(2, 3.0, metrics=metrics)
    _analyze(pool, "SELECT 1", "bob")  # the workers are started
    held, out = _in_thread(lambda: _analyze(pool, STUCK, "alice"))
    _until(lambda: "alice" in pool._callers)
    with pytest.raises(ParseRefused) as again:
        _analyze(pool, "SELECT 1", "alice")
    assert (again.value.kind, again.value.code) == ("busy", "parse_busy")
    assert _analyze(pool, "SELECT b FROM c.s.t", "bob").tables == ("c.s.t",)
    assert held.is_alive(), "bob was served while alice's statement still held its worker"
    held.join(10)
    assert out["refused"].code == "parse_deadline"
    assert _refusals(metrics, "caller_busy") == 1
    assert _analyze(pool, "SELECT 1", "alice").read_only, "her slot is free again"


def test_a_call_that_finds_no_worker_free_within_the_deadline_is_refused():
    metrics = Metrics()
    pool = ParsePool(1, 1.0, metrics=metrics)
    _analyze(pool, "SELECT 1")
    held, _ = _in_thread(lambda: _analyze(pool, STUCK, "alice"))
    _until(lambda: "alice" in pool._callers)
    started = time.monotonic()
    with pytest.raises(ParseRefused) as refused:
        _analyze(pool, "SELECT 1", "bob")
    assert (refused.value.kind, refused.value.code) == ("time_budget", "parse_no_worker")
    assert time.monotonic() - started < 1.0 + 0.5
    held.join(10)
    assert _refusals(metrics, "no_worker") == 1


def test_close_kills_every_worker_and_refuses_what_comes_after():
    pool = ParsePool(2, 5.0)
    _analyze(pool, "SELECT 1")
    processes = _processes(pool)
    assert len(processes) == 2
    pool.close()
    pool.close()  # twice is harmless
    assert all(process.poll() is not None for process in processes)
    with pytest.raises(ParseRefused) as refused:
        _analyze(pool, "SELECT 1")
    assert refused.value.code == "parse_no_worker"
    assert pool.pids() == []


def test_close_during_a_parse_refuses_it_and_starts_no_worker():
    pool = ParsePool(1, 30.0)
    _analyze(pool, "SELECT 1")
    thread, out = _in_thread(lambda: _analyze(pool, STUCK))
    _until(lambda: pool._callers)
    pool.close()
    thread.join(10)
    assert out["refused"].code == "parse_failed" and pool.pids() == []


def test_close_all_closes_every_pool_and_the_shared_one_is_made_again():
    shared = parse_pool.shared_pool(1, 9.5)
    assert parse_pool.shared_pool(1, 9.5) is shared
    other = ParsePool(1, 5.0)
    parse_pool.close_all()
    assert shared._closed and other._closed
    with pytest.raises(ParseRefused):  # closed before it ever started: nothing starts
        _analyze(other, "SELECT 1")
    assert other.pids() == []
    assert parse_pool.shared_pool(1, 9.5) is not shared


@pytest.mark.skipif(not _PROC, reason="reads /proc")
def test_a_worker_writes_nowhere_and_holds_no_setting_of_the_server(monkeypatch):
    """Its standard streams are the null device: over stdio, the server's standard output
    is the protocol channel. Its environment holds no secret of the server."""
    monkeypatch.setenv("TRINO_PASSWORD", "not-for-a-worker")
    pool = ParsePool(1, 5.0)
    _analyze(pool, "SELECT 1")
    (pid,) = pool.pids()
    for fd in (0, 1, 2):
        assert os.readlink(f"/proc/{pid}/fd/{fd}") == "/dev/null"
    with open(f"/proc/{pid}/environ", "rb") as env:
        names = {item.split(b"=", 1)[0] for item in env.read().split(b"\0") if item}
    assert names == {b"PYTHONPATH"}


def test_a_worker_that_cannot_start_leaves_the_call_refused():
    def broken():
        raise OSError("no process")

    pool = ParsePool(2, 0.2, launch=broken)
    with pytest.raises(ParseRefused) as refused:
        _analyze(pool, "SELECT 1")
    assert refused.value.code == "parse_no_worker" and pool.pids() == []


def test_a_worker_ends_as_soon_as_its_server_is_gone_even_mid_parse():
    """The server gone, its end of the request pipe is closed: the worker ends at once,
    though its parse would last minutes."""
    pool = ParsePool(1, 60.0)
    _analyze(pool, "SELECT 1")
    (process,) = _processes(pool)
    thread, out = _in_thread(lambda: _analyze(pool, STUCK))
    time.sleep(0.3)  # the worker is parsing
    process.stdin.close()  # what the end of the server does
    assert process.wait(5) == 0
    thread.join(10)
    assert out["refused"].code == "parse_failed"


def test_nothing_a_worker_prints_reaches_the_protocol_or_the_terminal():
    """The worker's first instructions: whatever is printed after them, by ``print``, on
    descriptor 1 or on descriptor 2, goes to the null device; only frames reach the pipe."""
    noisy = (
        "import os, sys\n"
        "from akko_mcp_trino import parse_worker as w\n"
        "requests, answers = w.isolate_streams()\n"
        "print('noise'); sys.stdout.flush(); print('noise', file=sys.stderr)\n"
        "os.write(1, b'noise'); os.write(2, b'noise')\n"
        "w.write_frame(answers, w.read_frame(requests))\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", noisy],
        input=struct.pack(">I", 2) + b"hi",
        capture_output=True,
        timeout=60,
        env={**os.environ, "PYTHONPATH": parse_pool._package_root()},
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout == struct.pack(">I", 2) + b"hi"
    assert done.stderr == b""


def _frame(payload):
    return struct.pack(">I", len(payload)) + payload


READY = _frame(b'["ready", 4242]')


class _Process:
    """A worker that never runs: what it answers is written in advance to its stdout pipe;
    ``before_read`` runs as the server waits for it."""

    pid = 4242

    def __init__(self, written=b"", end=False):
        read_fd, self._write_fd = os.pipe()
        self._ending = threading.Lock()
        self.stdout = os.fdopen(read_fd, "rb", buffering=0)
        self.stdin = io.BytesIO()
        self.killed = False
        # Written by a thread: a pipe holds a few kilobytes until its reader takes them.
        self._writer = threading.Thread(target=self._write, args=(written, end), daemon=True)
        self._writer.start()

    def _write(self, written, end):
        if written:
            with contextlib.suppress(OSError):
                os.write(self._write_fd, written)
        if end:
            self.end()

    def end(self):
        with self._ending:
            if self._write_fd is not None:
                os.close(self._write_fd)
                self._write_fd = None

    def poll(self):
        return 0 if self.killed else None

    def kill(self):
        self.killed = True
        self.end()

    def wait(self, timeout=None):
        return 0


def _fake(*processes):
    made = iter(processes)
    return lambda: next(made)


def test_a_worker_not_ready_within_the_deadline_is_kept_and_the_call_refused():
    pool = ParsePool(1, 0.1, launch=_fake(_Process()))
    with pytest.raises(ParseRefused) as refused:
        _analyze(pool, "SELECT 1")
    assert refused.value.code == "parse_no_worker"
    assert len(pool._idle) == 1, "the worker still starting is kept for the next call"


@pytest.mark.parametrize("first", [b"", _frame(b'["hello"]'), _frame(b"{}")])
def test_a_worker_that_ends_or_says_anything_but_ready_is_replaced(first):
    pool = ParsePool(1, 1.0, launch=_fake(_Process(first, end=True), _Process()))
    with pytest.raises(ParseRefused) as refused:
        _analyze(pool, "SELECT 1")
    assert refused.value.code == "parse_failed"
    assert len(pool._all) == 1, "a new worker took its place"


@pytest.mark.parametrize(
    "written",
    [
        _frame(b"not json"),
        _frame(b'["ok", ["yes", [], [true, []]]]'),
        _frame(b'["ok", [true, [], ["yes", []]]]'),
        _frame(b'["ok", [true, [], [true, [[["a"]]]]]]'),
        _frame(b'["ok", 7]'),
        _frame(b'["maybe", null]'),
        struct.pack(">I", parse_pool.MAX_FRAME_BYTES + 1) + b"x",  # too long: not read
        b"\x00\x00",  # a short frame, then the end
    ],
)
def test_a_malformed_or_oversized_answer_refuses_the_call_and_replaces_the_worker(written):
    metrics = Metrics()
    broken = _Process(READY + written, end=written == b"\x00\x00")
    pool = ParsePool(1, 1.0, launch=_fake(broken, _Process()), metrics=metrics)
    with pytest.raises(ParseRefused) as refused:
        _analyze(pool, "SELECT 1")
    assert (refused.value.kind, refused.value.code) == ("unavailable", "parse_failed")
    assert broken.killed and broken not in [w.process for w in pool._all]
    assert _refusals(metrics, "worker_failed") == 1


def test_an_analysis_that_failed_in_the_worker_is_no_read_and_keeps_the_worker():
    worker = _Process(READY + _frame(b'["error", null]'))
    pool = ParsePool(1, 1.0, launch=_fake(worker))
    assert _analyze(pool, "SELECT 1") == Analysis(False, (), ColumnScope(False, ()))
    assert [w.process for w in pool._idle] == [worker]


def test_a_request_too_long_for_a_frame_is_refused_before_any_worker(monkeypatch):
    monkeypatch.setattr(parse_pool, "MAX_FRAME_BYTES", 100)
    pool = ParsePool(1, 1.0, launch=_fake())
    with pytest.raises(ParseRefused) as refused:
        _analyze(pool, "SELECT '" + "x" * 200 + "'")
    assert (refused.value.kind, refused.value.code) == ("invalid_argument", "sql_too_long")
    assert pool._all == set()


def test_a_worker_that_cannot_be_written_to_is_replaced():
    class _Closed(io.BytesIO):
        def write(self, data):
            raise BrokenPipeError

    worker = _Process(READY)
    worker.stdin = _Closed()
    pool = ParsePool(1, 1.0, launch=_fake(worker, _Process()))
    with pytest.raises(ParseRefused) as refused:
        _analyze(pool, "SELECT 1")
    assert refused.value.code == "parse_failed"


def test_an_answer_after_close_stops_the_worker_instead_of_keeping_it():
    good = _frame(json.dumps(["ok", [True, ["c.s.t"], [True, []]]]).encode())
    holder = {}

    class _Late(_Process):
        def __init__(self):
            super().__init__(READY)

    late = _Late()
    pool = holder["pool"] = ParsePool(1, 1.0, launch=_fake(late))
    _analyze_started = []

    class _Stdin(io.BytesIO):
        def write(self, data):  # the request leaves; the pool closes; then the answer comes
            _analyze_started.append(True)
            holder["pool"]._closed = True
            os.write(late._write_fd, good)
            return len(data)

    late.stdin = _Stdin()
    assert _analyze(pool, "SELECT 1").tables == ("c.s.t",)
    assert late.killed and pool._idle == []


@pytest.mark.parametrize(
    "raw",
    [
        b"not json",
        b'["error", 1]',
        b'["ok", ["yes", [], [true, []]]]',
        b'["ok", [true, [], ["yes", []]]]',
        b'["ok", [true, [], [true, [[["a"]]]]]]',
        b'["ok", 7]',
    ],
)
def test_anything_but_a_well_formed_answer_is_none(raw):
    assert parse_pool._analysis(raw) is None
    assert parse_pool._analysis(b'["error", null]') == Analysis(False, (), ColumnScope(False, ()))


def test_the_worker_answers_on_its_pipes_and_ends_with_them():
    request_r, request_w = os.pipe()
    answer_r, answer_w = os.pipe()
    requests = os.fdopen(request_r, "rb", buffering=0)
    answers, replies = os.fdopen(answer_w, "wb", buffering=0), os.fdopen(answer_r, "rb")
    ended = []
    thread = threading.Thread(
        target=parse_worker.serve, args=(requests, answers, ended.append), daemon=True
    )
    thread.start()
    assert json.loads(parse_worker.read_frame(replies)) == ["ready", os.getpid()]
    os.write(request_w, _frame(json.dumps(["SELECT a FROM c.s.t", "c", []]).encode()))
    assert json.loads(parse_worker.read_frame(replies)) == [
        "ok",
        [True, ["c.s.t"], [True, [[["c", "s", "t"], ""]]]],
    ]
    os.write(request_w, _frame(b"[1, 2]"))  # a request it cannot read: an error, no crash
    assert json.loads(parse_worker.read_frame(replies)) == ["error", None]
    os.close(request_w)  # the server is gone
    thread.join(5)
    assert not thread.is_alive() and ended == [0]
    answers.close()
    assert parse_worker.read_frame(replies) is None


def test_a_worker_whose_answer_pipe_is_closed_ends_quietly():
    class _Gone(io.BytesIO):
        def write(self, data):
            raise BrokenPipeError

    ended = []
    parse_worker.serve(io.BytesIO(), _Gone(), ended.append)
    _until(lambda: ended == [0])


def test_the_metric_names_a_reason_never_a_statement():
    metrics = Metrics()
    ParsePool(1, 1.0, metrics=metrics).count("too_long")
    body = metrics.render()[0].decode()
    assert 'mcp_trino_sql_refusals_total{reason="too_long"} 1.0' in body
    assert set(parse_pool.REASONS) == {
        "too_long",
        "too_nested",
        "caller_busy",
        "no_worker",
        "deadline",
        "worker_failed",
    }


def test_a_launch_that_failed_is_tried_again_by_the_next_call():
    """A passing failure to start a worker (memory, a process limit) never leaves the pool
    short for good: the next call starts the missing workers."""
    attempts = []

    def flaky():
        attempts.append(1)
        if len(attempts) == 1:
            raise OSError("no process for now")
        return _Process(READY + _frame(json.dumps(["ok", [True, [], [True, []]]]).encode()))

    pool = ParsePool(1, 0.2, launch=flaky)
    with pytest.raises(ParseRefused) as refused:
        _analyze(pool, "SELECT 1")
    assert refused.value.code == "parse_no_worker" and pool._all == set()
    assert _analyze(pool, "SELECT 1").read_only is True
    assert len(pool._all) == 1 and len(attempts) == 2


def test_an_idle_worker_that_died_is_replaced_before_any_call_uses_it():
    good = _frame(json.dumps(["ok", [True, ["c.s.t"], [True, []]]]).encode())
    dead, fresh = _Process(READY), _Process(READY + good)
    pool = ParsePool(1, 1.0, launch=_fake(dead, fresh))
    with pool._lock:
        pool._start()
        pool._refill()
    dead.kill()  # it ended while it waited: its process gone, its pipe closed
    assert _analyze(pool, "SELECT 1").tables == ("c.s.t",)
    assert [w.process for w in pool._all] == [fresh]


def test_an_answer_that_cannot_even_be_read_replaces_the_worker():
    deep = b"[" * 200_000 + b"]" * 200_000  # json.loads raises RecursionError
    worker = _Process(READY + _frame(deep))
    pool = ParsePool(1, 1.0, launch=_fake(worker, _Process()))
    with pytest.raises(ParseRefused) as refused:
        _analyze(pool, "SELECT 1")
    assert refused.value.code == "parse_failed"
    assert worker.killed and worker not in [w.process for w in pool._all]


def test_an_unexpected_exception_while_asking_replaces_the_worker(monkeypatch):
    class _Stop(BaseException):
        pass

    def stop(raw):
        raise _Stop

    worker = _Process(READY + _frame(b'["error", null]'))
    pool = ParsePool(1, 1.0, launch=_fake(worker, _Process()))
    monkeypatch.setattr(parse_pool, "_analysis", stop)
    with pytest.raises(_Stop):
        _analyze(pool, "SELECT 1")
    assert worker.killed and worker not in [w.process for w in pool._all]
    assert len(pool._all) == 1 and pool._callers == set()


def test_a_frame_is_written_whole_even_when_the_stream_takes_a_few_bytes_at_a_time():
    class _Trickle(io.BytesIO):
        def write(self, data):
            return super().write(bytes(data[:3]))

    out = _Trickle()
    parse_worker.write_frame(out, b"a longer payload")
    assert out.getvalue() == _frame(b"a longer payload")

    class _WouldBlock(io.BytesIO):
        def write(self, data):
            return None

    with pytest.raises(BlockingIOError):
        parse_worker.write_frame(_WouldBlock(), b"x")


def test_a_worker_starts_without_a_console_window_on_windows(monkeypatch):
    seen = {}

    def popen(args, **kwargs):
        seen.update(kwargs)
        raise OSError("not started")

    monkeypatch.setattr(parse_pool.subprocess, "Popen", popen)
    monkeypatch.setattr(parse_pool.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    with pytest.raises(OSError):
        parse_pool.launch_worker()
    assert seen["creationflags"] == 0x08000000
    assert seen["stdin"] == seen["stdout"] == parse_pool.subprocess.PIPE
    assert seen["stderr"] == parse_pool.subprocess.DEVNULL and seen["close_fds"] is True
