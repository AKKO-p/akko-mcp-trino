"""The dedicated loop: one thread, submitted coroutines, cancellation past the timeout."""

import asyncio
import logging
import threading
import time

import pytest

from akko_mcp_trino.context import loop as loop_module
from akko_mcp_trino.context.loop import ContextLoop


def test_a_submitted_coroutine_runs_on_the_loop_thread(loop):
    async def where():
        return threading.current_thread().name

    assert loop.submit(where(), timeout=2) == "mcp-context"


def test_starting_twice_keeps_one_thread(loop):
    before = {t.name for t in threading.enumerate()}
    loop.start()
    assert [t.name for t in threading.enumerate()].count("mcp-context") == 1
    assert {t.name for t in threading.enumerate()} == before


def test_a_coroutine_past_its_timeout_is_cancelled(loop):
    seen = threading.Event()

    async def slow():
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            seen.set()
            raise

    start = time.monotonic()
    with pytest.raises(TimeoutError):
        loop.submit(slow(), timeout=0.1)
    assert seen.wait(2)
    assert time.monotonic() - start < 1


def test_an_error_of_the_coroutine_reaches_the_caller(loop):
    async def broken():
        raise ValueError("no")

    with pytest.raises(ValueError):
        loop.submit(broken(), timeout=2)


def test_a_loop_not_started_refuses_a_coroutine_without_leaving_it_unawaited():
    async def never():
        return 1

    coro = never()
    with pytest.raises(RuntimeError, match="not running"):
        ContextLoop().submit(coro, timeout=1)
    assert coro.cr_frame is None


def test_closing_cancels_what_is_left_and_stops_the_thread():
    started = ContextLoop()
    started.start()
    seen = threading.Event()

    async def forever():
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            seen.set()
            raise

    async def spawn():
        asyncio.get_running_loop().create_task(forever())

    started.submit(spawn(), timeout=2)
    started.close()
    assert seen.is_set()
    assert "mcp-context" not in {t.name for t in threading.enumerate()}
    started.close()
    ContextLoop().close()


def test_closing_does_not_hang_on_a_task_that_ignores_cancellation():
    started = ContextLoop()
    started.start()
    asked = threading.Event()

    async def stubborn():
        while True:
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                asked.set()

    async def fails_when_cancelled():
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            raise ValueError("cleanup failed") from None

    async def spawn():
        asyncio.get_running_loop().create_task(stubborn())
        asyncio.get_running_loop().create_task(fails_when_cancelled())

    started.submit(spawn(), timeout=2)
    start = time.monotonic()
    started.close()
    assert time.monotonic() - start < 5
    assert asked.is_set()
    assert "mcp-context" not in {t.name for t in threading.enumerate()}


def test_a_loop_thread_that_does_not_end_in_time_logs_one_warning(monkeypatch, caplog):
    monkeypatch.setattr(loop_module, "_CLOSE_WAIT_SECONDS", 0.05)
    started = ContextLoop()
    started.start()
    busy = threading.Event()

    async def blocks_the_thread():
        busy.set()
        time.sleep(0.5)

    asyncio.run_coroutine_threadsafe(blocks_the_thread(), started._loop)
    assert busy.wait(2)
    with caplog.at_level(logging.WARNING, logger="akko_mcp_trino.context.loop"):
        started.close()
    mine = [r.getMessage() for r in caplog.records if r.name == "akko_mcp_trino.context.loop"]
    assert mine == ["the context loop did not stop within 0.1 s; its thread is left behind"]
    _until_gone = time.monotonic() + 5
    while "mcp-context" in {t.name for t in threading.enumerate()}:
        assert time.monotonic() < _until_gone
        time.sleep(0.01)
