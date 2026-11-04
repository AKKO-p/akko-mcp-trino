"""The dedicated loop: one thread, submitted coroutines, cancellation past the timeout."""

import asyncio
import threading
import time

import pytest

from akko_mcp_trino.context.loop import ContextLoop


@pytest.fixture
def loop():
    started = ContextLoop()
    started.start()
    yield started
    started.close()


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
