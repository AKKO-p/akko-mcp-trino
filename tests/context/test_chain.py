"""The chain: cache first, batches, deadlines, concurrency, breakers, statuses, metrics."""

import asyncio
import inspect
import logging
import pathlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from akko_mcp_trino import config as config_module
from akko_mcp_trino.context import (
    ContextChain,
    ContextSettings,
    Gathered,
    ProviderRefused,
    ProviderUnavailable,
    TermsResult,
)
from akko_mcp_trino.context import chain as chain_module
from akko_mcp_trino.context.types import TableContext, TableRef, Term
from akko_mcp_trino.metrics import Metrics

ORDERS = TableRef.of("demo", "crm", "orders")
CLIENTS = TableRef.of("demo", "crm", "clients")
LEDGER = TableRef.of("finance", "gl", "ledger")


def _settings(**changes):
    values = {
        "timeout_seconds": 5.0,
        "reserve_seconds": 1.0,
        "max_concurrency": 4,
        "retry_after_seconds": 30.0,
        "refused_retry_seconds": 3600.0,
        "batch": 50,
        "ttl_seconds": 300.0,
        "cache_bytes": 1_000_000,
        "terms_cache_bytes": 1_000_000,
    }
    values.update(changes)
    return ContextSettings(**values)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Fake:
    """A context source held in memory, with a gate, a delay and a failure to set."""

    context_api = 1

    def __init__(self, name, *, catalogs=("demo",), known=None, terms=None):
        self.name = name
        self.catalogs = set(catalogs)
        self.known = dict(known or {})
        self.glossary = dict(terms or {})
        self.calls = []
        self.term_calls = []
        self.fail = None
        self.delay = 0.0
        self.slow_after = None
        self.cancelled = 0
        self.gate = None
        self.gates = {}
        self.fails = {}
        self.hold = None
        self.stamp = ""
        self.stamp_during_call = None
        self.maps_error = None
        self.stamp_error = None
        self.closed = False
        self.sets = {}

    def maps(self, catalog):
        if self.maps_error is not None:
            raise self.maps_error
        return catalog in self.catalogs

    def served_sets(self):
        if isinstance(self.sets, Exception):
            raise self.sets
        return self.sets

    async def describe(self, refs, *, deadline, on_behalf_of=None):
        self.calls.append(tuple(refs))
        if self.gate is not None:
            await self.gate.wait()
        if refs[0] in self.gates:
            await self.gates[refs[0]].wait()
        if self.stamp_during_call is not None:
            self.stamp = self.stamp_during_call
        slow = self.delay and (self.slow_after is None or len(self.calls) > self.slow_after)
        if slow:
            try:
                await asyncio.sleep(self.delay)
            except asyncio.CancelledError:
                self.cancelled += 1
                if self.hold is None:
                    raise
                # Ignores every cancellation until the test lets it go.
                while not self.hold.is_set():
                    try:
                        await self.hold.wait()
                    except asyncio.CancelledError:
                        self.cancelled += 1
                return {}
        if refs[0] in self.fails:
            raise self.fails[refs[0]]
        if self.fail is not None:
            raise self.fail
        return {ref: self.known[ref] for ref in refs if ref in self.known}

    async def terms(self, keys, *, deadline):
        self.term_calls.append(tuple(keys))
        if self.fail is not None:
            raise self.fail
        return {key: self.glossary[key] for key in keys if key in self.glossary}

    def check_lines(self):
        return [f"{self.name}: configured"]

    async def aclose(self):
        self.closed = True
        raise RuntimeError("already closed")

    def credentials_stamp(self):
        if self.stamp_error is not None:
            raise self.stamp_error
        return self.stamp


def _chain(loop, *providers, clock=None, metrics=None, **settings):
    return ContextChain(
        providers, _settings(**settings), loop=loop, clock=clock or Clock(), metrics=metrics
    )


def _until(condition, seconds=2.0):
    end = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < end, "condition not reached"
        time.sleep(0.005)


def _release(loop, fake):
    loop._loop.call_soon_threadsafe(fake.gate.set)


def _gate(loop, fake):
    async def make():
        fake.gate = asyncio.Event()

    loop.submit(make(), timeout=2)


def _set(loop, event):
    loop._loop.call_soon_threadsafe(event.set)


def test_an_unmapped_catalog_calls_nothing(loop):
    fake = Fake("one", catalogs=("finance",))
    chain = _chain(loop, fake)
    got = chain.gather([ORDERS], deadline=chain._clock() + 5)
    assert got == Gathered(tables={ORDERS: ()}, statuses={"one": "not_mapped"})
    assert fake.calls == []


def test_contexts_come_in_the_order_of_the_sources_with_one_status_each(loop):
    first = Fake("first", known={ORDERS: TableContext(description="A")})
    second = Fake("second", catalogs=("demo", "finance"), known={LEDGER: TableContext(owner="o")})
    chain = _chain(loop, first, second)
    got = chain.gather([ORDERS, LEDGER, ORDERS], deadline=chain._clock() + 5)
    assert got.tables[ORDERS] == (("first", TableContext(description="A")),)
    assert got.tables[LEDGER] == (("second", TableContext(owner="o")),)
    assert dict(got.statuses) == {"first": "ok", "second": "ok"}
    assert got.terms == {}
    assert first.calls == [(ORDERS,)] and second.calls == [(ORDERS, LEDGER)]
    assert chain.names == ("first", "second")


def test_a_source_that_knows_none_of_the_tables_says_not_found(loop):
    fake = Fake("one")
    chain = _chain(loop, fake)
    got = chain.gather([ORDERS], deadline=chain._clock() + 5)
    assert got.statuses["one"] == "not_found"


def test_the_cache_comes_first_and_an_absence_is_kept(loop):
    fake = Fake("one", known={ORDERS: TableContext(description="A")})
    chain = _chain(loop, fake)
    chain.gather([ORDERS, CLIENTS], deadline=chain._clock() + 5)
    again = chain.gather([ORDERS, CLIENTS], deadline=chain._clock() + 5)
    assert fake.calls == [(ORDERS, CLIENTS)]
    assert again.statuses["one"] == "ok" and again.tables[CLIENTS] == ()


def test_missing_tables_are_read_by_batches(loop):
    refs = [TableRef.of("demo", "crm", f"t{i}") for i in range(5)]
    fake = Fake("one", known={ref: TableContext() for ref in refs})
    chain = _chain(loop, fake, batch=2)
    chain.gather(refs, deadline=chain._clock() + 5)
    assert [len(call) for call in fake.calls] == [2, 2, 1]


def test_a_slow_provider_is_cancelled_at_the_deadline_and_says_unavailable(loop):
    slow = Fake("slow", known={ORDERS: TableContext()})
    slow.delay = 10.0
    chain = _chain(loop, slow, clock=time.monotonic)
    deadline = time.monotonic() + 0.2
    got = chain.gather([ORDERS], deadline=deadline)
    assert time.monotonic() < deadline + 0.5
    assert got.statuses["slow"] == "unavailable"
    assert slow.cancelled == 1


def test_a_deadline_reached_after_one_batch_says_partial(loop):
    refs = [ORDERS, CLIENTS]
    fake = Fake("one", known={ref: TableContext() for ref in refs})
    fake.delay, fake.slow_after = 10.0, 1
    chain = _chain(loop, fake, clock=time.monotonic, batch=1)
    got = chain.gather(refs, deadline=time.monotonic() + 0.2)
    assert got.statuses["one"] == "partial"
    assert got.tables[ORDERS] == (("one", TableContext()),) and got.tables[CLIENTS] == ()


def test_the_deadline_reached_on_a_large_page_opens_no_breaker(loop):
    fake = Fake("one", known={ORDERS: TableContext()})
    fake.delay = 10.0
    chain = _chain(loop, fake, clock=time.monotonic)
    assert chain.gather([ORDERS], deadline=time.monotonic() + 0.1).statuses["one"] == (
        "unavailable"
    )
    fake.delay = 0.0
    assert chain.gather([ORDERS], deadline=time.monotonic() + 2).statuses["one"] == "ok"
    assert len(fake.calls) == 2


def test_a_deadline_already_passed_still_serves_the_cache(loop):
    """Within the grace past the deadline: past it, nothing is waited for."""
    fake = Fake("one", known={ORDERS: TableContext()})
    clock = Clock()
    chain = _chain(loop, fake, clock=clock)
    chain.gather([ORDERS], deadline=clock.now + 5)
    got = chain.gather([ORDERS, CLIENTS], deadline=clock.now - 0.1)
    assert got.statuses["one"] == "partial" and got.tables[ORDERS]
    assert len(fake.calls) == 1


def test_one_provider_down_does_not_hide_the_others(loop):
    down = Fake("down", known={ORDERS: TableContext()})
    down.fail = ProviderUnavailable("connection refused")
    up = Fake("up", known={ORDERS: TableContext(description="B")})
    clock = Clock()
    chain = _chain(loop, down, up, clock=clock)
    got = chain.gather([ORDERS], deadline=clock.now + 5)
    assert dict(got.statuses) == {"down": "unavailable", "up": "ok"}
    assert got.tables[ORDERS] == (("up", TableContext(description="B")),)
    clock.now += 29
    assert chain.gather([CLIENTS], deadline=clock.now + 5).statuses["down"] == "unavailable"
    assert len(down.calls) == 1
    clock.now += 2
    down.fail = None
    assert chain.gather([CLIENTS], deadline=clock.now + 5).statuses["down"] == "not_found"
    assert len(down.calls) == 2


def test_a_refused_account_is_not_retried_before_the_long_delay(loop, caplog):
    fake = Fake("atlas")
    fake.fail = ProviderRefused("401")
    clock = Clock()
    chain = _chain(loop, fake, clock=clock)
    with caplog.at_level(logging.WARNING, logger="akko_mcp_trino.context.chain"):
        assert chain.gather([ORDERS], deadline=clock.now + 5).statuses["atlas"] == "refused"
        clock.now += 31
        assert chain.gather([ORDERS], deadline=clock.now + 5).statuses["atlas"] == "refused"
        assert len(fake.calls) == 1
        clock.now += 3570
        assert chain.gather([ORDERS], deadline=clock.now + 5).statuses["atlas"] == "refused"
        assert len(fake.calls) == 2
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == ["atlas refused the account"] * 2


def test_a_changed_secret_file_or_reset_retries_a_refused_provider_at_once(loop):
    fake = Fake("one")
    fake.fail = ProviderRefused("403")
    clock = Clock()
    chain = _chain(loop, fake, clock=clock)
    chain.gather([ORDERS], deadline=clock.now + 5)
    fake.stamp = "secret changed"
    chain.gather([ORDERS], deadline=clock.now + 5)
    assert len(fake.calls) == 2
    chain.gather([ORDERS], deadline=clock.now + 5)
    assert len(fake.calls) == 2
    chain.reset_refused()
    fake.fail = None
    assert chain.gather([ORDERS], deadline=clock.now + 5).statuses["one"] == "not_found"
    assert len(fake.calls) == 3


def test_a_reset_tells_every_provider_that_has_a_reset_hook(loop, caplog):
    listening, silent, broken = Fake("one"), Fake("two"), Fake("three")
    resets = []
    listening.reset = lambda: resets.append("one")

    def fails():
        resets.append("three")
        raise RuntimeError("cannot forget")

    broken.reset = fails
    chain = _chain(loop, listening, silent, broken)
    with caplog.at_level(logging.WARNING, logger="akko_mcp_trino.context.chain"):
        chain.reset_refused()  # a hook that raises never escapes the signal handler
    assert resets == ["one", "three"]
    # Said once, by the provider's name and the type of the error, never its text.
    assert [r.getMessage() for r in caplog.records] == [
        "context provider three: reset failed (RuntimeError)"
    ]
    assert "cannot forget" not in caplog.text


def test_a_reset_does_not_close_a_breaker_opened_by_an_outage(loop):
    fake = Fake("one")
    fake.fail = ProviderUnavailable("down")
    clock = Clock()
    chain = _chain(loop, fake, clock=clock)
    chain.gather([ORDERS], deadline=clock.now + 5)
    chain.reset_refused()
    fake.stamp = "other"
    assert chain.gather([ORDERS], deadline=clock.now + 5).statuses["one"] == "unavailable"
    assert len(fake.calls) == 1


def test_a_refusal_seen_twice_at_once_warns_once(loop, caplog):
    fake = Fake("one")
    fake.fail = ProviderRefused("401")
    _gate(loop, fake)
    clock = Clock()
    chain = _chain(loop, fake, clock=clock, batch=1)
    with ThreadPoolExecutor(2) as pool, caplog.at_level(logging.WARNING):
        first = pool.submit(chain.gather, [ORDERS], deadline=clock.now + 5)
        _until(lambda: len(fake.calls) == 1)
        second = pool.submit(chain.gather, [CLIENTS], deadline=clock.now + 5)
        _until(lambda: len(fake.calls) == 2)
        _release(loop, fake)
        statuses = [first.result().statuses["one"], second.result().statuses["one"]]
    assert statuses == ["refused", "refused"]
    assert [r.getMessage() for r in caplog.records] == ["one refused the account"]


def test_an_unexpected_error_says_error_and_logs_a_trace_without_its_message(loop, caplog):
    fake = Fake("one")
    fake.fail = KeyError("password=s3cr3t")
    chain = _chain(loop, fake)
    with caplog.at_level(logging.ERROR, logger="akko_mcp_trino.context.chain"):
        got = chain.gather([ORDERS], deadline=chain._clock() + 5)
        again = chain.gather([ORDERS], deadline=chain._clock() + 5)
    assert got.statuses["one"] == "error" and again.statuses["one"] == "error"
    assert len(fake.calls) == 2
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "KeyError" in text and "describe" in text
    assert "s3cr3t" not in text and "password" not in text


def test_an_outage_raised_by_the_source_at_the_deadline_opens_no_breaker(loop):
    fake = Fake("one", known={ORDERS: TableContext()})
    clock = Clock()
    chain = _chain(loop, fake, clock=clock)

    async def keeps_the_deadline(refs, *, deadline, on_behalf_of=None):
        fake.calls.append(tuple(refs))
        if len(fake.calls) == 1:
            clock.now = deadline
            raise ProviderUnavailable("read timed out")
        return {ORDERS: TableContext()}

    fake.describe = keeps_the_deadline
    assert chain.gather([ORDERS], deadline=clock.now + 5).statuses["one"] == "unavailable"
    assert chain.gather([ORDERS], deadline=clock.now + 5).statuses["one"] == "ok"
    assert len(fake.calls) == 2


def test_a_source_whose_mapping_fails_says_error_without_hiding_the_others(loop, caplog):
    broken = Fake("broken")
    broken.maps_error = OSError("/run/secrets/token: No such file")
    up = Fake("up", known={ORDERS: TableContext(description="B")})
    chain = _chain(loop, broken, up)
    with caplog.at_level(logging.ERROR, logger="akko_mcp_trino.context.chain"):
        got = chain.gather([ORDERS], deadline=chain._clock() + 5)
    assert dict(got.statuses) == {"broken": "error", "up": "ok"}
    assert got.tables[ORDERS] == (("up", TableContext(description="B")),)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "OSError" in text and "/run/secrets" not in text


def test_a_source_whose_secret_stamp_fails_says_error_without_hiding_the_others(loop, caplog):
    broken = Fake("broken")
    broken.stamp_error = OSError("/run/secrets/token: No such file")
    up = Fake("up", known={ORDERS: TableContext(description="B")})
    chain = _chain(loop, broken, up)
    with caplog.at_level(logging.ERROR, logger="akko_mcp_trino.context.chain"):
        got = chain.gather([ORDERS], deadline=chain._clock() + 5)
    assert dict(got.statuses) == {"broken": "error", "up": "ok"}
    assert broken.calls == []
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "OSError" in text and "/run/secrets" not in text


def _tasks_left(loop):
    async def count():
        return len(asyncio.all_tasks()) - 1

    return loop.submit(count(), timeout=2)


@pytest.fixture
def stubborn(loop):
    fake = Fake("stubborn", known={ORDERS: TableContext()})
    fake.delay, fake.hold = 60.0, asyncio.Event()
    yield fake
    # Let it go even when the test failed, so that the loop can close.
    _set(loop, fake.hold)


def test_a_read_left_behind_no_longer_holds_its_table(loop, stubborn):
    up = Fake("up", known={ORDERS: TableContext()})
    chain = _chain(loop, stubborn, up, clock=time.monotonic, max_concurrency=2)
    deadline = time.monotonic() + 0.2
    got = chain.gather([ORDERS], deadline=deadline)
    assert time.monotonic() < deadline + 0.5
    assert dict(got.statuses) == {"stubborn": "unavailable", "up": "ok"}
    stubborn.delay = 0.0
    start = time.monotonic()
    again = chain.gather([ORDERS], deadline=start + 5)
    assert time.monotonic() - start < 1
    assert again.statuses["stubborn"] == "ok" and len(stubborn.calls) == 2
    _set(loop, stubborn.hold)
    _until(lambda: _tasks_left(loop) == 0)


def test_reads_left_behind_count_against_the_cap_until_they_end(loop, stubborn):
    chain = _chain(loop, stubborn, clock=time.monotonic, max_concurrency=1)
    assert chain.gather([ORDERS], deadline=time.monotonic() + 0.2).statuses["stubborn"] == (
        "unavailable"
    )
    stubborn.delay = 0.0
    start = time.monotonic()
    capped = chain.gather([ORDERS], deadline=start + 5)
    assert time.monotonic() - start < 1
    assert capped.statuses["stubborn"] == "unavailable" and len(stubborn.calls) == 1
    _set(loop, stubborn.hold)
    _until(lambda: _tasks_left(loop) == 0)
    assert chain.gather([CLIENTS], deadline=time.monotonic() + 5).statuses["stubborn"] == (
        "not_found"
    )
    assert len(stubborn.calls) == 2


def test_a_secret_changed_during_a_refused_call_is_tried_at_once(loop):
    fake = Fake("one")
    fake.fail = ProviderRefused("401")
    fake.stamp, fake.stamp_during_call = "old", "new"
    clock = Clock()
    chain = _chain(loop, fake, clock=clock)
    assert chain.gather([ORDERS], deadline=clock.now + 5).statuses["one"] == "refused"
    fake.fail = None
    assert chain.gather([ORDERS], deadline=clock.now + 5).statuses["one"] == "not_found"
    assert len(fake.calls) == 2


def test_an_outage_never_shortens_an_open_refusal(loop):
    fake = Fake("one")
    fake.fails = {ORDERS: ProviderRefused("401"), CLIENTS: ProviderUnavailable("down")}
    fake.gates = {ORDERS: asyncio.Event(), CLIENTS: asyncio.Event()}
    clock = Clock()
    chain = _chain(loop, fake, clock=clock)
    with ThreadPoolExecutor(2) as pool:
        refused = pool.submit(chain.gather, [ORDERS], deadline=clock.now + 5)
        down = pool.submit(chain.gather, [CLIENTS], deadline=clock.now + 5)
        _until(lambda: len(fake.calls) == 2)
        _set(loop, fake.gates[ORDERS])
        assert refused.result().statuses["one"] == "refused"
        _set(loop, fake.gates[CLIENTS])
        assert down.result().statuses["one"] == "unavailable"
    clock.now += 31
    assert chain.gather([ORDERS], deadline=clock.now + 5).statuses["one"] == "refused"
    assert len(fake.calls) == 2


def test_a_source_that_cancels_itself_says_unavailable_without_hiding_the_others(loop):
    quitter = Fake("quitter")
    quitter.fail = asyncio.CancelledError()
    up = Fake("up", known={ORDERS: TableContext()})
    chain = _chain(loop, quitter, up)
    got = chain.gather([ORDERS], deadline=chain._clock() + 5)
    assert dict(got.statuses) == {"quitter": "unavailable", "up": "ok"}


def test_two_callers_of_the_same_table_share_one_read(loop):
    fake = Fake("one", known={ORDERS: TableContext(description="A")})
    _gate(loop, fake)
    clock = Clock()
    chain = _chain(loop, fake, clock=clock)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(chain.gather, [ORDERS], deadline=clock.now + 5)
        _until(lambda: len(fake.calls) == 1)
        second = pool.submit(chain.gather, [ORDERS], deadline=clock.now + 5)
        _until(lambda: chain._active["one"] == 2)
        _release(loop, fake)
        results = [first.result(), second.result()]
    assert fake.calls == [(ORDERS,)]
    assert [r.statuses["one"] for r in results] == ["ok", "ok"]


def test_more_calls_in_flight_than_the_limit_say_unavailable_at_once(loop):
    fake = Fake("one", known={ORDERS: TableContext()})
    _gate(loop, fake)
    clock = Clock()
    chain = _chain(loop, fake, clock=clock, max_concurrency=1)
    with ThreadPoolExecutor(1) as pool:
        first = pool.submit(chain.gather, [ORDERS], deadline=clock.now + 5)
        _until(lambda: len(fake.calls) == 1)
        assert chain.gather([CLIENTS], deadline=clock.now + 5).statuses["one"] == "unavailable"
        _release(loop, fake)
        assert first.result().statuses["one"] == "ok"
    assert len(fake.calls) == 1
    assert chain.gather([CLIENTS], deadline=clock.now + 5).statuses["one"] == "not_found"


def test_terms_are_read_once_by_source_and_key(loop):
    term = Term(key="k1", name="Customer", glossary="g")
    first = Fake("first", terms={"k1": term})
    second = Fake("second")
    chain = _chain(loop, first, second, batch=1)
    wanted = {"first": ["k1", "k2", "k1"], "second": [], "elsewhere": ["k1"]}
    got = chain.terms(wanted, deadline=chain._clock() + 5)
    assert dict(got.terms) == {("first", "k1"): term}
    assert chain.terms(wanted, deadline=chain._clock() + 5).terms == got.terms
    assert first.term_calls == [("k1",), ("k2",)] and second.term_calls == []


def test_terms_of_a_refused_source_are_not_asked_again(loop):
    fake = Fake("one")
    fake.fail = ProviderRefused("401")
    chain = _chain(loop, fake)
    assert chain.terms({"one": ["k"]}, deadline=chain._clock() + 5).terms == {}
    assert chain.terms({"one": ["k"]}, deadline=chain._clock() + 5).terms == {}
    assert len(fake.term_calls) == 1


def test_check_lines_and_close_reach_every_source(loop):
    first, second = Fake("first"), Fake("second")
    chain = _chain(loop, first, second)
    assert chain.check_lines() == ["first: configured", "second: configured"]
    chain.close()
    assert first.closed and second.closed


def test_no_metric_label_names_an_object(loop):
    metrics = Metrics()
    up = Fake("up", known={ORDERS: TableContext(description="A")})
    down = Fake("down")
    down.fail = ProviderUnavailable("down")
    clock = Clock()
    chain = _chain(loop, up, down, clock=clock, metrics=metrics)
    chain.gather([ORDERS, CLIENTS], deadline=clock.now + 5)
    clock.now += 31
    down.fail = None
    chain.gather([ORDERS], deadline=clock.now + 5)
    labels = set()
    for family in metrics.registry.collect():
        if family.name.startswith("mcp_trino_context"):
            for sample in family.samples:
                labels.update(v for k, v in sample.labels.items() if k != "le")
    assert labels == {"up", "down", "hit", "miss", "unavailable"}
    value = metrics.registry.get_sample_value
    assert value("mcp_trino_context_calls_total", {"provider": "up"}) == 1
    assert value("mcp_trino_context_calls_total", {"provider": "down"}) == 2
    assert value("mcp_trino_context_call_duration_seconds_count", {"provider": "down"}) == 2
    assert value("mcp_trino_context_cache_lookups_total", {"provider": "up", "result": "hit"}) == 1
    assert value("mcp_trino_context_cache_lookups_total", {"provider": "up", "result": "miss"}) == 2
    assert value("mcp_trino_context_cache_bytes", {"provider": "up"}) > 0
    assert value("mcp_trino_context_breaker_open", {"provider": "down"}) == 0
    errors = {"provider": "down", "status": "unavailable"}
    assert value("mcp_trino_context_errors_total", errors) == 1


def test_an_open_breaker_shows_in_its_gauge(loop):
    metrics = Metrics()
    down = Fake("down")
    down.fail = ProviderRefused("401")
    chain = _chain(loop, down, metrics=metrics)
    chain.gather([ORDERS], deadline=chain._clock() + 5)
    value = metrics.registry.get_sample_value
    assert value("mcp_trino_context_breaker_open", {"provider": "down"}) == 1
    assert value("mcp_trino_context_errors_total", {"provider": "down", "status": "refused"}) == 1


def test_a_chain_in_use_runs_on_exactly_one_context_thread(loop):
    chain = _chain(loop, Fake("up", known={ORDERS: TableContext()}))
    chain.gather([ORDERS], deadline=chain._clock() + 5)
    assert [t.name for t in threading.enumerate()].count("mcp-context") == 1


def test_terms_returns_the_statuses_it_computed(loop):
    term = Term(key="k1", name="Customer", glossary="g")
    first = Fake("first", terms={"k1": term})
    empty = Fake("empty")
    refused = Fake("refused")
    refused.fail = ProviderRefused("401")
    unasked = Fake("unasked")
    chain = _chain(loop, first, empty, refused, unasked)
    wanted = {"first": ["k1"], "empty": ["k9"], "refused": ["k1"]}
    got = chain.terms(wanted, deadline=chain._clock() + 5)
    assert isinstance(got, TermsResult)
    assert dict(got.terms) == {("first", "k1"): term}
    assert dict(got.statuses) == {"first": "ok", "empty": "not_found", "refused": "refused"}
    with pytest.raises(TypeError):
        got.statuses["first"] = "error"  # type: ignore[index]


def test_served_sets_of_a_failing_provider_is_an_error_not_a_crash(loop, caplog):
    metrics = Metrics()
    good = Fake("good")
    good.sets = {"glossary": frozenset({"Finance"})}
    broken = Fake("broken")
    broken.sets = RuntimeError("token s3cr3t rejected")
    chain = _chain(loop, good, broken, metrics=metrics)
    with caplog.at_level(logging.DEBUG, logger="akko_mcp_trino.context.chain"):
        assert dict(chain.served_sets("good")) == {"glossary": frozenset({"Finance"})}
        assert chain.served_sets("broken") is None, "an error is not an empty declaration"
        assert dict(chain.served_sets("elsewhere")) == {}
        good.sets = {}
        assert dict(chain.served_sets("good")) == {}
    assert "RuntimeError" in caplog.text and "s3cr3t" not in caplog.text
    errors = {"provider": "broken", "status": "error"}
    assert metrics.registry.get_sample_value("mcp_trino_context_errors_total", errors) == 1
    good.sets = {"glossary": frozenset({"Finance"})}
    with pytest.raises(TypeError):
        chain.served_sets("good")["glossary"] = frozenset()  # type: ignore[index]


def test_an_outage_logs_one_warning_per_opening(loop, caplog):
    down = Fake("down")
    down.fail = ProviderUnavailable("connection refused by 10.0.0.1")
    clock = Clock()
    chain = _chain(loop, down, clock=clock)
    with caplog.at_level(logging.WARNING, logger="akko_mcp_trino.context.chain"):
        assert chain.gather([ORDERS], deadline=clock.now + 5).statuses["down"] == "unavailable"
        assert chain.gather([ORDERS], deadline=clock.now + 5).statuses["down"] == "unavailable"
        clock.now += 31
        assert chain.gather([ORDERS], deadline=clock.now + 5).statuses["down"] == "unavailable"
    assert len(down.calls) == 2
    assert [r.getMessage() for r in caplog.records] == ["down is unavailable"] * 2


def test_sighup_resets_the_breaker_gauge(loop):
    metrics = Metrics()
    refused = Fake("refused")
    refused.fail = ProviderRefused("401")
    down = Fake("down")
    down.fail = ProviderUnavailable("down")
    chain = _chain(loop, refused, down, metrics=metrics)
    chain.gather([ORDERS], deadline=chain._clock() + 5)
    value = metrics.registry.get_sample_value
    assert value("mcp_trino_context_breaker_open", {"provider": "refused"}) == 1
    assert value("mcp_trino_context_breaker_open", {"provider": "down"}) == 1
    chain.reset_refused()
    assert value("mcp_trino_context_breaker_open", {"provider": "refused"}) == 0
    assert value("mcp_trino_context_breaker_open", {"provider": "down"}) == 1, "an outage stays"


def test_a_close_past_the_timeout_is_logged_by_type_and_not_raised(loop, caplog):
    class Slow(Fake):
        async def aclose(self):
            await asyncio.sleep(10)

    chain = _chain(loop, Slow("slow"), timeout_seconds=0.05)
    with caplog.at_level(logging.WARNING, logger="akko_mcp_trino.context.chain"):
        chain.close()
    assert [r.getMessage() for r in caplog.records] == [
        "the context sources were not all closed (TimeoutError)"
    ]


class _SpendingLoop:
    """A loop that never runs its coroutine: each read takes its whole wait, on the clock."""

    running = True

    def __init__(self, clock):
        self.clock = clock
        self.waits = []

    def submit(self, coro, timeout):
        coro.close()
        self.waits.append(timeout)
        self.clock.now += timeout
        raise TimeoutError


def test_gather_then_terms_end_by_the_deadline_and_one_grace():
    clock = Clock()
    spending = _SpendingLoop(clock)
    chain = ContextChain([Fake("a")], _settings(), loop=spending, clock=clock)
    start, deadline = clock.now, clock.now + 1.0
    with pytest.raises(TimeoutError):
        chain.gather([ORDERS], deadline=deadline)
    with pytest.raises(TimeoutError):
        chain.terms({"a": ["k"]}, deadline=deadline)
    assert clock.now <= deadline + chain_module._GRACE_SECONDS
    assert spending.waits == [deadline - start + chain_module._GRACE_SECONDS, 0.0]


def test_the_grace_written_in_the_docs_is_the_chains():
    """The 0.25 s the CHANGELOG and the code's own words give is the chain's grace."""
    written = f"{chain_module._GRACE_SECONDS:g} s"
    root = pathlib.Path(__file__).resolve().parents[2]
    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    assert f"may wait up to {written} past its deadline" in changelog
    assert f"waits up to {written} for a provider" in inspect.getsource(config_module)
    assert f"a grace of {written}" in " ".join(ContextChain.gather.__doc__.split())
    assert config_module.MIN_CONTEXT_RESERVE_SECONDS > chain_module._GRACE_SECONDS
