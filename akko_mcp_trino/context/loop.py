"""The event loop the context sources run on: one, in its own thread, per process.

The tools are synchronous. They submit their reads to this loop and wait for the
result, at most until their deadline; a read past it is cancelled.
"""

import asyncio
import threading
from collections.abc import Coroutine
from typing import Any


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
        """Cancel what is left on the loop, stop it and join its thread."""
        with self._lock:
            loop, thread = self._loop, self._thread
            self._loop = self._thread = None
        if loop is None or thread is None:
            return
        loop.call_soon_threadsafe(loop.stop)
        thread.join()

    @staticmethod
    def _run(loop: asyncio.AbstractEventLoop) -> None:
        asyncio.set_event_loop(loop)
        try:
            loop.run_forever()
            left = asyncio.all_tasks(loop)
            for task in left:
                task.cancel()
            loop.run_until_complete(asyncio.gather(*left, return_exceptions=True))
            loop.run_until_complete(loop.shutdown_asyncgens())
        finally:
            loop.close()
