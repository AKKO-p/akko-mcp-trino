"""Caches bounded in bytes, with spread expiry and one read in flight per key.

A table of two thousand columns weighs megabytes, so an entry counts the length
of its JSON form, not one unit. Expiry is drawn within ten percent of the TTL, so
entries loaded together do not expire together.
"""

import asyncio
import dataclasses
import json
import random
import time
from collections import OrderedDict, defaultdict
from collections.abc import Awaitable, Callable, Hashable, Mapping, Sequence
from typing import Any

_SPREAD = 0.1


def _plain(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: getattr(value, f.name) for f in dataclasses.fields(value)}
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError(f"{type(value).__name__} has no JSON form")


def serialized_size(value: Any) -> int:
    """Return the length of the JSON form of ``value``; an absence (``None``) counts four."""
    return len(json.dumps(value, default=_plain, separators=(",", ":")))


def _no_group(key: Hashable) -> Hashable:
    return None


def _being_cancelled() -> bool:
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0


def _fail(future: asyncio.Future, exc: BaseException) -> None:
    if future.done():
        # Abandoned: its waiters were already sent to read again.
        return
    if isinstance(exc, asyncio.CancelledError):
        future.cancel()
        return
    future.set_exception(exc)
    # Marks the exception as retrieved: a key nobody waits for must not warn.
    future.exception()


class ByteCache:
    """Entries kept until their expiry or until the sum of their sizes passes ``max_bytes``.

    ``group`` names the group a key belongs to, so that ``bytes_of`` can tell what
    one context source holds in a shared cache. The cache is used from one event
    loop only.
    """

    def __init__(
        self,
        max_bytes: int,
        ttl_seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        rng: Callable[[], float] = random.random,
        group: Callable[[Hashable], Hashable] = _no_group,
    ) -> None:
        """Bound the cache to ``max_bytes`` and ``ttl_seconds``, spread by ten percent."""
        self._max_bytes = max_bytes
        self._ttl = ttl_seconds
        self._clock = clock
        self._rng = rng
        self._group = group
        self._entries: OrderedDict[Hashable, tuple[float, int, Any]] = OrderedDict()
        self._bytes = 0
        self._group_bytes: defaultdict[Hashable, int] = defaultdict(int)
        self._inflight: dict[Hashable, asyncio.Future] = {}
        self._leaders: dict[Hashable, asyncio.Task | None] = {}

    @property
    def bytes(self) -> int:
        """Return the sum of the sizes of the entries held."""
        return self._bytes

    def bytes_of(self, group: Hashable) -> int:
        """Return the sum of the sizes of the entries of one group."""
        return self._group_bytes.get(group, 0)

    def get(self, key: Hashable) -> tuple[bool, Any]:
        """Return (found, value); a known absence is found with the value ``None``."""
        entry = self._entries.get(key)
        if entry is None:
            return False, None
        expires, _, value = entry
        if self._clock() >= expires:
            self._drop(key)
            return False, None
        return True, value

    def put(self, key: Hashable, value: Any, size: int) -> None:
        """Keep ``value`` under ``key``, evicting the oldest entries past ``max_bytes``."""
        if key in self._entries:
            self._drop(key)
        if size > self._max_bytes:
            return
        spread = 1 - _SPREAD + 2 * _SPREAD * self._rng()
        self._entries[key] = (self._clock() + self._ttl * spread, size, value)
        self._bytes += size
        self._group_bytes[self._group(key)] += size
        while self._bytes > self._max_bytes:
            self._drop(next(iter(self._entries)))

    def abandon(self, owner: asyncio.Task) -> None:
        """Stop treating the reads led by ``owner`` as in flight.

        Their waiters read again, and so do later callers; what ``owner`` returns
        if it ever ends is still kept.
        """
        for key in [key for key, leader in self._leaders.items() if leader is owner]:
            del self._leaders[key]
            self._inflight.pop(key).cancel()

    async def load(
        self,
        key: Hashable,
        loader: Callable[[], Awaitable[Any]],
        size_of: Callable[[Any], int],
    ) -> Any:
        """Return the cached value of ``key``, or read it once for every caller that waits."""

        async def one(keys: Sequence[Hashable]) -> Mapping[Hashable, Any]:
            return {key: await loader()}

        return (await self.load_many([key], one, size_of))[key]

    async def load_many(
        self,
        keys: Sequence[Hashable],
        loader: Callable[[Sequence[Hashable]], Awaitable[Mapping[Hashable, Any]]],
        size_of: Callable[[Any], int],
    ) -> dict[Hashable, Any]:
        """Return a value for each key, reading in one call the keys nobody is reading.

        A key the loader leaves out is kept as a known absence (``None``); a key it
        adds is ignored. A key another caller is reading is awaited, and read again
        if that read was cancelled.
        """
        found: dict[Hashable, Any] = {}
        todo = list(dict.fromkeys(keys))
        while todo:
            lead: list[Hashable] = []
            wait: dict[Hashable, asyncio.Future] = {}
            for key in todo:
                hit, value = self.get(key)
                if hit:
                    found[key] = value
                elif key in self._inflight:
                    wait[key] = self._inflight[key]
                else:
                    lead.append(key)
            if lead:
                found.update(await self._lead(lead, loader, size_of))
            todo = []
            for key, pending in wait.items():
                try:
                    found[key] = await asyncio.shield(pending)
                except asyncio.CancelledError:
                    if _being_cancelled():
                        raise
                    todo.append(key)
        return found

    async def _lead(
        self,
        keys: list[Hashable],
        loader: Callable[[Sequence[Hashable]], Awaitable[Mapping[Hashable, Any]]],
        size_of: Callable[[Any], int],
    ) -> dict[Hashable, Any]:
        loop = asyncio.get_running_loop()
        futures = {key: loop.create_future() for key in keys}
        self._inflight.update(futures)
        owner = asyncio.current_task()
        self._leaders.update(dict.fromkeys(keys, owner))
        try:
            got = await loader(keys)
            values = {key: got.get(key) for key in keys}
            sizes = {key: size_of(value) for key, value in values.items()}
        except BaseException as exc:
            for future in futures.values():
                _fail(future, exc)
            raise
        finally:
            for key in keys:
                if self._inflight.get(key) is futures[key]:
                    del self._inflight[key]
                    del self._leaders[key]
        for key, value in values.items():
            self.put(key, value, sizes[key])
            if not futures[key].done():
                futures[key].set_result(value)
        return values

    def _drop(self, key: Hashable) -> None:
        _, size, _ = self._entries.pop(key)
        self._bytes -= size
        self._group_bytes[self._group(key)] -= size
