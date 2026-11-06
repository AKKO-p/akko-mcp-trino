"""The event loop the context sources run on: one, in its own thread, per process.

The tools are synchronous. They submit their reads to this loop and wait for the
result, at most until their deadline; a read past it is cancelled.
"""

import asyncio
import logging
import threading
from collections.abc import Coroutine
from typing import Any

_log = logging.getLogger(__name__)

# An internal margin, not an operating setting: on close, the tasks left on the loop
# are given this long to end once cancelled; one that ignores its cancellation is
# dropped, and the thread is waited for at most twice this long.
_CLOSE_WAIT_SECONDS = 1.0


class ContextLoop:
    """A daemon thread running one event loop, started once and closed once."""

    def __init__(self) -> None:
        """Prepare the loop; nothing runs before ``start``."""
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start the thread and its loop; a second call does nothing."""
        with self._lock:
            if self._thread is not None:
                return
            loop = asyncio.new_event_loop()
            thread = threading.Thread(
                target=self._run, args=(loop,), name="mcp-context", daemon=True
            )
            self._loop, self._thread = loop, thread
            thread.start()

    @property
    def running(self) -> bool:
        """Say whether the loop is started and not closed."""
        return self._loop is not None

    def submit(self, coro: Coroutine[Any, Any, Any], timeout: float) -> Any:
        """Run ``coro`` on the loop and return its result; cancel it past ``timeout`` seconds.

        Raises ``TimeoutError`` past the timeout, and ``RuntimeError`` when the loop
        is not running.
        """
        loop = self._loop
        if loop is None:
            coro.close()
            raise RuntimeError("the context loop is not running")
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        try:
            return future.result(timeout)
        except TimeoutError:
            future.cancel()
            raise

    def close(self) -> None:
        """Cancel what is left on the loop, stop it and join its thread, in bounded time.

        A task that ignores its cancellation does not hold the close past an internal
        margin; it is dropped with the closed loop, and asyncio may log, once, that a
        pending task was destroyed. A thread that does not end in time is left behind
        with a warning.
        """
        with self._lock:
            loop, thread = self._loop, self._thread
            self._loop = self._thread = None
        if loop is None or thread is None:
            return
        loop.call_soon_threadsafe(loop.stop)
        thread.join(2 * _CLOSE_WAIT_SECONDS)
        if thread.is_alive():
            _log.warning(
                "the context loop did not stop within %g s; its thread is left behind",
                2 * _CLOSE_WAIT_SECONDS,
            )

    @staticmethod
    def _run(loop: asyncio.AbstractEventLoop) -> None:
        asyncio.set_event_loop(loop)
        try:
            loop.run_forever()
            left = asyncio.all_tasks(loop)
            for task in left:
                task.cancel()
            if left:
                done, _ = loop.run_until_complete(asyncio.wait(left, timeout=_CLOSE_WAIT_SECONDS))
                # A task that failed while it was cancelled is read, so that it does not warn.
                for task in done:
                    if not task.cancelled():
                        task.exception()
            loop.run_until_complete(loop.shutdown_asyncgens())
        finally:
            loop.close()
