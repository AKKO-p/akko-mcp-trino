"""akko_mcp_trino.parse_pool and parse_worker: an agent's SQL is parsed in worker
processes, within a deadline, one statement at a time per caller.

The slow statements are real: an ARRAY nested 24 deep keeps sqlglot busy for minutes, so
the deadline, not the statement, ends each test that sends one.
"""

from __future__ import annotations

import json
import os
import signal
import threading
import time
from multiprocessing import Pipe

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
    started = time.monotonic()
    with pytest.raises(ParseRefused) as refused:
        _analyze(pool, STUCK)
    assert time.monotonic() - started < 1.0 + 0.5
    assert (refused.value.kind, refused.value.code) == ("time_budget", "parse_deadline")
    assert "ARRAY" not in str(refused.value) and "1 s" in str(refused.value)
    _until(lambda: pool.pids() and pool.pids() != [first])
    with pytest.raises(ProcessLookupError):
        os.kill(first, 0)
    assert _analyze(pool, "SELECT 1").read_only is True, "the replacement serves"
    assert _refusals(metrics, "deadline") == 1


def test_a_worker_that_dies_refuses_the_call_and_never_allows_it():
    metrics = Metrics()
    pool = ParsePool(1, 30.0, metrics=metrics)
    _analyze(pool, "SELECT 1")
    (pid,) = pool.pids()
    thread, out = _in_thread(lambda: _analyze(pool, STUCK))
    time.sleep(0.3)  # the worker is parsing
    os.kill(pid, signal.SIGKILL)
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
    pids = pool.pids()
    assert len(pids) == 2
    pool.close()
    pool.close()  # twice is harmless
    for pid in pids:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
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


def test_launch_worker_closes_its_pipes_when_the_process_cannot_start(monkeypatch):
    def refuse(*args, **kwargs):
        raise OSError("no exec")

    monkeypatch.setattr(parse_pool.subprocess, "Popen", refuse)
    before = len(os.listdir("/dev/fd"))
    with pytest.raises(OSError):
        parse_pool.launch_worker()
    assert len(os.listdir("/dev/fd")) == before


class _Process:
    pid = 4242

    def __init__(self):
        self.killed = False

    def poll(self):
        return 0 if self.killed else None

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return 0


class _Requests:
    def send_bytes(self, data):
        pass

    def close(self):
        pass


class _Answers:
    """Answers ``replies`` in turn; ``before`` runs as a poll starts."""

    def __init__(self, replies, ready=True, before=None):
        self.replies, self.before = list(replies), before
        if ready:
            self.replies.insert(0, b'["ready", 4242]')

    def poll(self, timeout):
        if self.before:
            self.before()
        return bool(self.replies)

    def recv_bytes(self):
        reply = self.replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply

    def close(self):
        pass


def _fake(answers):
    return lambda: (_Process(), _Requests(), answers)


def test_a_worker_not_ready_within_the_deadline_is_kept_and_the_call_refused():
    pool = ParsePool(1, 0.1, launch=_fake(_Answers([], ready=False)))
    with pytest.raises(ParseRefused) as refused:
        _analyze(pool, "SELECT 1")
    assert refused.value.code == "parse_no_worker"
    assert len(pool._idle) == 1, "the worker still starting is kept for the next call"


def test_a_worker_that_dies_while_starting_is_replaced():
    pool = ParsePool(1, 1.0, launch=_fake(_Answers([EOFError()], ready=False)))
    with pytest.raises(ParseRefused) as refused:
        _analyze(pool, "SELECT 1")
    assert refused.value.code == "parse_failed"


def test_an_answer_after_close_stops_the_worker_instead_of_keeping_it():
    holder = {}
    good = json.dumps(["ok", [True, ["c.s.t"], [True, []]]]).encode()
    answers = _Answers([good], before=lambda: holder["pool"].close())
    pool = holder["pool"] = ParsePool(1, 1.0, launch=_fake(answers))
    with pool._lock:
        pool._start()
    pool._idle[0].ready = True
    answers.replies.pop(0)  # the ready line, already read
    assert _analyze(pool, "SELECT 1").tables == ("c.s.t",)
    assert pool._idle == []


@pytest.mark.parametrize(
    "raw",
    [
        b"not json",
        b'["error", null]',
        b'["ok", ["yes", [], [true, []]]]',
        b'["ok", [true, [], ["yes", []]]]',
        b'["ok", [true, [], [true, [[["a"]]]]]]',
        b'["ok", 7]',
    ],
)
def test_anything_but_a_well_formed_answer_is_no_read(raw):
    assert parse_pool._analysis(raw) == Analysis(False, (), ColumnScope(False, ()))


def test_the_worker_answers_on_its_pipes_and_ends_with_them():
    requests_r, requests_w = Pipe(duplex=False)
    answers_r, answers_w = Pipe(duplex=False)
    thread = threading.Thread(
        target=parse_worker.serve, args=(requests_r, answers_w, os.getppid()), daemon=True
    )
    thread.start()
    assert json.loads(answers_r.recv_bytes()) == ["ready", os.getpid()]
    requests_w.send_bytes(json.dumps(["SELECT a FROM c.s.t", "c", []]).encode())
    assert json.loads(answers_r.recv_bytes()) == [
        "ok",
        [True, ["c.s.t"], [True, [[["c", "s", "t"], ""]]]],
    ]
    requests_w.send_bytes(b"[1, 2]")  # a request it cannot read: an error, no crash
    assert json.loads(answers_r.recv_bytes()) == ["error", None]
    requests_w.close()
    thread.join(5)
    assert not thread.is_alive()


def test_a_worker_ends_once_its_server_is_gone():
    stop, ended = threading.Event(), []
    parents = iter([10, 10, 1])
    parse_worker.watch_parent(
        10, stop, exit=ended.append, every=0.001, getppid=lambda: next(parents)
    )
    assert ended == [0]
    stop.set()
    parse_worker.watch_parent(10, stop, exit=ended.append, every=0.001, getppid=lambda: 10)
    assert ended == [0], "a stopped watch returns without ending the process"


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
