"""Byte-bounded caches: size of the JSON form, spread expiry, one read in flight per key."""

import asyncio
import json

import pytest

from akko_mcp_trino.context.caches import ByteCache, serialized_size
from akko_mcp_trino.context.types import ColumnContext, Labelled, TableContext, TableRef


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_the_size_is_the_length_of_the_json_form_read_only_mappings_included():
    context = TableContext(
        description="Orders",
        tags=(Labelled("pii", "classification"),),
        columns={"id": ColumnContext(description="Key")},
    )
    size = serialized_size(context)
    assert size == len(
        json.dumps(
            {
                "description": "Orders",
                "owner": "",
                "tier": "",
                "tags": [{"name": "pii", "set": "classification"}],
                "terms": [],
                "properties": {},
                "grain": [],
                "joins": [],
                "columns": {
                    "id": {
                        "description": "Key",
                        "classifications": [],
                        "terms": [],
                        "properties": {},
                    }
                },
            },
            separators=(",", ":"),
        )
    )
    assert serialized_size(None) == 4


def test_a_value_without_a_json_form_is_refused():
    with pytest.raises(TypeError, match="object has no JSON form"):
        serialized_size(object())


def test_a_known_absence_is_found_with_none():
    cache = ByteCache(100, 60)
    assert cache.get("t") == (False, None)
    cache.put("t", None, 4)
    assert cache.get("t") == (True, None)


def test_a_cache_bounded_in_bytes_evicts_the_oldest_entry():
    cache = ByteCache(10, 60)
    cache.put("a", "A", 4)
    cache.put("b", "B", 4)
    cache.put("c", "C", 4)
    assert cache.get("a") == (False, None)
    assert cache.get("b") == (True, "B") and cache.get("c") == (True, "C")
    assert cache.bytes == 8


def test_putting_a_key_again_replaces_its_size():
    cache = ByteCache(10, 60)
    cache.put("a", "A", 4)
    cache.put("a", "AA", 6)
    assert cache.get("a") == (True, "AA") and cache.bytes == 6


def test_an_entry_larger_than_the_cache_is_not_kept():
    cache = ByteCache(10, 60)
    cache.put("a", "A", 4)
    cache.put("big", "B", 11)
    assert cache.get("big") == (False, None)
    assert cache.get("a") == (True, "A")


def test_ttls_are_spread_by_ten_percent():
    clock = Clock()
    draws = iter([0.0, 0.999999])
    cache = ByteCache(100, 100, clock=clock, rng=lambda: next(draws))
    cache.put("short", 1, 1)
    cache.put("long", 2, 1)
    clock.now = 89.9
    assert cache.get("short") == (True, 1)
    clock.now = 90.0
    assert cache.get("short") == (False, None)
    assert cache.get("long") == (True, 2)
    clock.now = 109.9
    assert cache.get("long") == (True, 2)
    clock.now = 110.0
    assert cache.get("long") == (False, None)
    assert cache.bytes == 0


def test_bytes_are_counted_by_group():
    cache = ByteCache(100, 60, group=lambda key: key[0])
    cache.put(("one", 1), "x", 5)
    cache.put(("two", 1), "y", 7)
    assert cache.bytes_of("one") == 5 and cache.bytes_of("two") == 7
    assert cache.bytes_of("three") == 0 and cache.bytes == 12


def test_two_loads_of_the_same_key_share_one_read():
    cache = ByteCache(100, 60)
    calls = []

    async def run():
        gate = asyncio.Event()

        async def loader():
            calls.append(1)
            await gate.wait()
            return "value"

        first = asyncio.create_task(cache.load("k", loader, serialized_size))
        second = asyncio.create_task(cache.load("k", loader, serialized_size))
        await asyncio.sleep(0)
        gate.set()
        return await first, await second, await cache.load("k", loader, serialized_size)

    assert asyncio.run(run()) == ("value", "value", "value")
    assert calls == [1]


def test_load_many_reads_the_missing_keys_once_keeps_absences_and_ignores_extras():
    cache = ByteCache(100, 60)
    cache.put(TableRef.of("c", "s", "cached"), "C", 1)
    asked = []

    async def loader(keys):
        asked.append(list(keys))
        return {keys[0]: "A", TableRef.of("c", "s", "extra"): "E"}

    keys = [TableRef.of("c", "s", n) for n in ("cached", "a", "b", "a")]
    got = asyncio.run(cache.load_many(keys, loader, serialized_size))
    assert got == {keys[0]: "C", keys[1]: "A", keys[2]: None}
    assert asked == [[keys[1], keys[2]]]
    assert cache.get(keys[2]) == (True, None)
    assert cache.get(TableRef.of("c", "s", "extra")) == (False, None)


def test_a_failed_read_reaches_every_waiter_and_is_not_kept():
    cache = ByteCache(100, 60)

    async def run():
        gate = asyncio.Event()

        async def loader():
            await gate.wait()
            raise ValueError("down")

        first = asyncio.create_task(cache.load("k", loader, serialized_size))
        second = asyncio.create_task(cache.load("k", loader, serialized_size))
        await asyncio.sleep(0)
        gate.set()
        return await asyncio.gather(first, second, return_exceptions=True)

    results = asyncio.run(run())
    assert [type(r) for r in results] == [ValueError, ValueError]
    assert cache.get("k") == (False, None)


def test_a_failed_read_nobody_waits_for_leaves_no_warning(recwarn):
    cache = ByteCache(100, 60)

    async def loader():
        raise ValueError("down")

    with pytest.raises(ValueError):
        asyncio.run(cache.load("k", loader, serialized_size))
    assert not recwarn.list


def test_a_cancelled_read_lets_a_waiter_read_again():
    cache = ByteCache(100, 60)
    calls = []

    async def run():
        gate = asyncio.Event()

        async def loader():
            calls.append(1)
            if len(calls) == 1:
                await gate.wait()
            return "second"

        first = asyncio.create_task(cache.load("k", loader, serialized_size))
        await asyncio.sleep(0)
        second = asyncio.create_task(cache.load("k", loader, serialized_size))
        await asyncio.sleep(0)
        first.cancel()
        return await asyncio.gather(first, second, return_exceptions=True)

    first, second = asyncio.run(run())
    assert isinstance(first, asyncio.CancelledError) and second == "second"
    assert calls == [1, 1]


def test_a_waiter_cancelled_itself_leaves_the_read_running():
    cache = ByteCache(100, 60)

    async def run():
        gate = asyncio.Event()

        async def loader():
            await gate.wait()
            return "value"

        first = asyncio.create_task(cache.load("k", loader, serialized_size))
        await asyncio.sleep(0)
        second = asyncio.create_task(cache.load("k", loader, serialized_size))
        await asyncio.sleep(0)
        second.cancel()
        await asyncio.sleep(0)
        gate.set()
        return await asyncio.gather(first, second, return_exceptions=True)

    first, second = asyncio.run(run())
    assert first == "value" and isinstance(second, asyncio.CancelledError)
    assert cache.get("k") == (True, "value")


def test_an_abandoned_read_no_longer_holds_its_key():
    cache = ByteCache(100, 60)
    calls = []

    async def run():
        gate = asyncio.Event()

        async def loader():
            calls.append(1)
            if len(calls) == 1:
                await gate.wait()
                return "late"
            return "fresh"

        leader = asyncio.create_task(cache.load("k", loader, serialized_size))
        await asyncio.sleep(0)
        waiter = asyncio.create_task(cache.load("k", loader, serialized_size))
        await asyncio.sleep(0)
        cache.abandon(leader)
        cache.abandon(waiter)
        fresh = await waiter
        later = await cache.load("k", loader, serialized_size)
        gate.set()
        return fresh, later, await leader

    assert asyncio.run(run()) == ("fresh", "fresh", "late")
    assert calls == [1, 1]
    assert cache.get("k") == (True, "late")


def test_an_abandoned_read_that_fails_later_warns_nobody(recwarn):
    cache = ByteCache(100, 60)

    async def run():
        gate = asyncio.Event()

        async def loader():
            await gate.wait()
            raise ValueError("down")

        leader = asyncio.create_task(cache.load("k", loader, serialized_size))
        await asyncio.sleep(0)
        cache.abandon(leader)
        gate.set()
        return await asyncio.gather(leader, return_exceptions=True)

    assert [type(r) for r in asyncio.run(run())] == [ValueError]
    assert cache.get("k") == (False, None)
    assert not recwarn.list
