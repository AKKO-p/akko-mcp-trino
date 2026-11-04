"""The chain of context sources: shared caches, deadlines, concurrency and breakers.

Every source is read in parallel on the dedicated loop. A source that is down,
or that refuses the account, is not called again before its delay; a deadline
reached on a large read is not an outage and opens nothing. The text of a
source's error never leaves the server: the caller gets one status per source.
"""

import asyncio
import functools
import logging
import time
import traceback
from collections import defaultdict
from collections.abc import Awaitable, Callable, Hashable, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from ..metrics import Metrics
from .caches import ByteCache, serialized_size
from .loop import ContextLoop
from .provider import ContextProvider
from .status import (
    ERROR,
    NOT_FOUND,
    NOT_MAPPED,
    OK,
    PARTIAL,
    REFUSED,
    UNAVAILABLE,
    status_of,
)
from .types import TableContext, TableRef, Term

log = logging.getLogger(__name__)

# The time the loop is given past the deadline to cancel the late reads and answer.
_GRACE_SECONDS = 0.25

_NOT_WELL = frozenset({PARTIAL, UNAVAILABLE, REFUSED, ERROR})

Fetch = Callable[[ContextProvider, Sequence[Any], float], Awaitable[Mapping[Any, Any]]]


def _empty() -> Mapping:
    return MappingProxyType({})


@dataclass(frozen=True)
class ContextSettings:
    """The limits of the chain, read from the server configuration."""

    timeout_seconds: float
    reserve_seconds: float
    max_concurrency: int
    retry_after_seconds: float
    refused_retry_seconds: float
    batch: int
    ttl_seconds: float
    cache_bytes: int
    terms_cache_bytes: int


@dataclass(frozen=True)
class Gathered:
    """What the chain read for one call.

    ``tables`` gives, for each table asked, its contexts in the order of the
    sources; ``terms`` the terms by (source, key); ``statuses`` one status per
    source. ``gather`` leaves ``terms`` empty: terms are read with
    ``ContextChain.terms`` once the tables are filtered.
    """

    tables: Mapping[TableRef, tuple[tuple[str, TableContext], ...]]
    terms: Mapping[tuple[str, str], Term] = field(default_factory=_empty)
    statuses: Mapping[str, str] = field(default_factory=_empty)


@dataclass
class _Breaker:
    status: str
    until: float
    stamp: str
    generation: int


async def _describe(
    provider: ContextProvider, refs: Sequence[TableRef], deadline: float
) -> Mapping[TableRef, TableContext]:
    return await provider.describe(refs, deadline=deadline)


async def _terms(provider: ContextProvider, keys: Sequence[str], deadline: float) -> Mapping:
    return await provider.terms(keys, deadline=deadline)


class ContextChain:
    """The context sources, in the configured order, behind caches and breakers."""

    def __init__(
        self,
        providers: Sequence[ContextProvider],
        settings: ContextSettings,
        *,
        loop: ContextLoop,
        clock: Callable[[], float] = time.monotonic,
        metrics: Metrics | None = None,
    ) -> None:
        """Keep the sources and build the shared caches; ``loop`` must be started."""
        self._providers = tuple(providers)
        self._settings = settings
        self._loop = loop
        self._clock = clock
        self._metrics = metrics
        self.names: tuple[str, ...] = tuple(p.name for p in self._providers)
        self._tables = ByteCache(
            settings.cache_bytes, settings.ttl_seconds, clock=clock, group=_source
        )
        self._terms = ByteCache(
            settings.terms_cache_bytes, settings.ttl_seconds, clock=clock, group=_source
        )
        self._active: defaultdict[str, int] = defaultdict(int)
        self._breakers: dict[str, _Breaker] = {}
        # Bumped from any thread (a signal handler); compared on the loop.
        self._refused_generation = 0
        if metrics is not None:
            for name in self.names:
                metrics.context_cache_bytes.labels(provider=name).set_function(
                    functools.partial(self._held, name)
                )
                metrics.context_breaker_open.labels(provider=name).set(0)

    def gather(self, refs: Sequence[TableRef], *, deadline: float) -> Gathered:
        """Read the context of ``refs`` from every source, at most until ``deadline``.

        ``deadline`` is an instant of the chain's clock.
        """
        wanted = tuple(dict.fromkeys(refs))
        return self._loop.submit(self._gather(wanted, deadline), self._timeout(deadline))

    def terms(
        self, wanted: Mapping[str, Sequence[str]], *, deadline: float
    ) -> Mapping[tuple[str, str], Term]:
        """Read glossary terms by source name and key, at most until ``deadline``.

        A source not in the chain is ignored; a term a source does not know is left out.
        """
        return self._loop.submit(self._read_terms(wanted, deadline), self._timeout(deadline))

    def reset_refused(self) -> None:
        """Let every source that refused the account be called again at once."""
        self._refused_generation += 1

    def check_lines(self) -> list[str]:
        """Return the lines the configuration check prints for the sources."""
        return [line for p in self._providers for line in p.check_lines()]

    def close(self) -> None:
        """Release what the sources hold open; a failure to close is not raised."""
        self._loop.submit(self._aclose(), self._settings.timeout_seconds)

    def _timeout(self, deadline: float) -> float:
        return max(deadline - self._clock(), 0.0) + _GRACE_SECONDS

    async def _aclose(self) -> None:
        await asyncio.gather(*(p.aclose() for p in self._providers), return_exceptions=True)

    async def _gather(self, refs: tuple[TableRef, ...], deadline: float) -> Gathered:
        jobs: dict[str, tuple[ContextProvider, list[Any]]] = {}
        statuses: dict[str, str] = {}
        for provider in self._providers:
            mine = [ref for ref in refs if provider.maps(ref.catalog)]
            if mine:
                jobs[provider.name] = (provider, mine)
            else:
                statuses[provider.name] = NOT_MAPPED
        found, read = await self._collect(jobs, self._tables, _describe, deadline)
        statuses.update(read)
        tables = {
            ref: tuple(
                (name, context)
                for name in self.names
                if (context := found.get(name, {}).get(ref)) is not None
            )
            for ref in refs
        }
        return Gathered(
            tables=MappingProxyType(tables),
            statuses=MappingProxyType({name: statuses[name] for name in self.names}),
        )

    async def _read_terms(
        self, wanted: Mapping[str, Sequence[str]], deadline: float
    ) -> Mapping[tuple[str, str], Term]:
        jobs = {
            p.name: (p, list(dict.fromkeys(wanted[p.name])))
            for p in self._providers
            if wanted.get(p.name)
        }
        found, _ = await self._collect(jobs, self._terms, _terms, deadline)
        return MappingProxyType(
            {
                (name, key): term
                for name in self.names
                for key, term in found.get(name, {}).items()
                if term is not None
            }
        )

    async def _collect(
        self,
        jobs: Mapping[str, tuple[ContextProvider, list[Any]]],
        cache: ByteCache,
        fetch: Fetch,
        deadline: float,
    ) -> tuple[dict[str, dict[Any, Any]], dict[str, str]]:
        found: dict[str, dict[Any, Any]] = {}
        statuses: dict[str, str] = {}
        tasks: dict[asyncio.Task, ContextProvider] = {}
        for name, (provider, items) in jobs.items():
            mine: dict[Any, Any] = {}
            found[name] = mine
            missing = []
            for item in items:
                hit, value = cache.get((name, item))
                self._count_lookup(name, hit)
                if hit:
                    mine[item] = value
                else:
                    missing.append(item)
            if not missing:
                statuses[name] = _settled(mine)
                continue
            if self._clock() >= deadline:
                statuses[name] = PARTIAL if mine else UNAVAILABLE
                continue
            blocked = self._blocked(provider)
            if blocked is not None:
                statuses[name] = blocked
                continue
            if self._active[name] >= self._settings.max_concurrency:
                statuses[name] = UNAVAILABLE
                continue
            self._active[name] += 1
            task = asyncio.create_task(self._read(provider, cache, fetch, missing, mine, deadline))
            task.add_done_callback(functools.partial(self._release, name))
            tasks[task] = provider
        try:
            if tasks:
                done, late = await asyncio.wait(tasks, timeout=max(deadline - self._clock(), 0.0))
                for task in late:
                    task.cancel()
                await asyncio.gather(*late, return_exceptions=True)
                for task, provider in tasks.items():
                    statuses[provider.name] = self._outcome(
                        provider, task, task in done, found[provider.name]
                    )
        finally:
            for task in tasks:
                task.cancel()
        for name, status in statuses.items():
            if status in _NOT_WELL and self._metrics is not None:
                self._metrics.context_errors.labels(provider=name, status=status).inc()
        return found, statuses

    async def _read(
        self,
        provider: ContextProvider,
        cache: ByteCache,
        fetch: Fetch,
        missing: list[Any],
        mine: dict[Any, Any],
        deadline: float,
    ) -> None:
        name = provider.name
        index: dict[Hashable, Any] = {(name, item): item for item in missing}

        async def load(keys: Sequence[Hashable]) -> Mapping[Hashable, Any]:
            start = time.perf_counter()
            if self._metrics is not None:
                self._metrics.context_calls.labels(provider=name).inc()
            try:
                got = await fetch(provider, [index[key] for key in keys], deadline)
            finally:
                if self._metrics is not None:
                    self._metrics.context_duration.labels(provider=name).observe(
                        time.perf_counter() - start
                    )
            return {(name, item): value for item, value in got.items()}

        keys = list(index)
        size = self._settings.batch
        for at in range(0, len(keys), size):
            values = await cache.load_many(keys[at : at + size], load, serialized_size)
            for key, value in values.items():
                mine[index[key]] = value

    def _outcome(
        self, provider: ContextProvider, task: asyncio.Task, in_time: bool, mine: dict
    ) -> str:
        if not in_time:
            return PARTIAL if mine else UNAVAILABLE
        exc = task.exception()
        if exc is None:
            return _settled(mine)
        status = status_of(exc)
        if status == REFUSED:
            self._open(provider, REFUSED, self._settings.refused_retry_seconds)
        elif status == UNAVAILABLE:
            self._open(provider, UNAVAILABLE, self._settings.retry_after_seconds)
        else:
            log.error(
                "context source %s failed with %s\n%s",
                provider.name,
                type(exc).__name__,
                "".join(traceback.format_tb(exc.__traceback__)).rstrip(),
            )
        return status

    def _open(self, provider: ContextProvider, status: str, delay: float) -> None:
        if self._blocked(provider) == status:
            return
        self._breakers[provider.name] = _Breaker(
            status=status,
            until=self._clock() + delay,
            stamp=provider.credentials_stamp(),
            generation=self._refused_generation,
        )
        if status == REFUSED:
            log.warning("%s refused the account", provider.name)
        if self._metrics is not None:
            self._metrics.context_breaker_open.labels(provider=provider.name).set(1)

    def _blocked(self, provider: ContextProvider) -> str | None:
        breaker = self._breakers.get(provider.name)
        if breaker is None:
            return None
        if self._clock() < breaker.until and (
            breaker.status != REFUSED
            or (
                breaker.generation == self._refused_generation
                and breaker.stamp == provider.credentials_stamp()
            )
        ):
            return breaker.status
        del self._breakers[provider.name]
        if self._metrics is not None:
            self._metrics.context_breaker_open.labels(provider=provider.name).set(0)
        return None

    def _release(self, name: str, _: asyncio.Task) -> None:
        self._active[name] -= 1

    def _held(self, name: str) -> float:
        return self._tables.bytes_of(name) + self._terms.bytes_of(name)

    def _count_lookup(self, name: str, hit: bool) -> None:
        if self._metrics is not None:
            result = "hit" if hit else "miss"
            self._metrics.context_cache.labels(provider=name, result=result).inc()


def _source(key: Any) -> Hashable:
    return key[0]


def _settled(mine: Mapping[Any, Any]) -> str:
    return OK if any(value is not None for value in mine.values()) else NOT_FOUND
