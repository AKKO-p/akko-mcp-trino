"""The contract every context provider must pass, published for adapter authors.

An adapter's tests subclass :class:`ContextContract` and give it a ``harness`` pytest
fixture that returns a :class:`ContractHarness`: how to build the provider from its
settings and an HTTP transport, the catalogue's recorded answers, the settings with a
secret the contract can look for, and which recorded objects play which role
(:class:`ContractNames`). The contract needs no network, no catalogue and no Trino:
every request the provider sends goes to a transport that answers from the recordings,
or, on purpose, slowly, with a 401, a 503, a body that is not JSON, or a 500 whose body
holds the secret. The transport counts what is sent and what is still in flight. Four
checks send the requests over a real socket instead, to a server on 127.0.0.1 that accepts
connections and never answers, for a describe and for terms, each with the provider alone
and behind the core's chain: the contract needs no network beyond the loopback interface.
A suite run under a socket blocker must allow 127.0.0.1.

A provider that sends no HTTP request, such as ``file``, skips the checks that need one,
each with ``pytest.skip("no HTTP")``. Each check fails with ``pytest.fail`` and a message
that never holds the secret.

The secret is looked for as written, JSON-escaped, URL-encoded, as Python writes it and in
base64: give a secret with a quote, a backslash and a non-ASCII character, so that escaping
matters. Logs are read from the root logger: a logger of the provider that does not
propagate escapes the check. The import check imports the module of the provider's class:
keep the entry point's factory in that module, or in one that imports no more.
"""

import asyncio
import base64
import inspect
import json
import logging
import re
import socket
import subprocess
import sys
import time
import traceback
from collections.abc import Callable, Coroutine, Iterable, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, fields, replace
from typing import Any
from urllib.parse import quote, quote_plus

import httpx
import pytest

from ..config import Config
from ..context.chain import ContextChain, ContextSettings
from ..context.errors import ProviderError, ProviderRefused, ProviderUnavailable
from ..context.loop import ContextLoop
from ..context.provider import ContextProvider
from ..context.status import (
    ERROR,
    NOT_CHECKED,
    NOT_FOUND,
    NOT_MAPPED,
    OK,
    PARTIAL,
    REFUSED,
    UNAVAILABLE,
)
from ..context.types import (
    CLASSIFICATION,
    GLOSSARY,
    OWNER,
    PROPERTY,
    ColumnContext,
    Join,
    Labelled,
    TableContext,
    TableRef,
    Term,
    TermRef,
)

_KINDS = frozenset({GLOSSARY, CLASSIFICATION, PROPERTY, OWNER})
_REPLAY, _SLOW, _REFUSED, _DOWN, _BAD_JSON, _LEAK = (
    "replay",
    "slow",
    "refused",
    "down",
    "bad-json",
    "leak",
)
# Longer than any deadline the contract gives: a slow answer never arrives in time.
_SLOW_SECONDS = 30.0
# How long the contract waits for one call before it gives up on the provider.
_SUBMIT_SECONDS = 120.0
_TRICKY = "'\". "
# The core never touches these; an adapter must not either.
_FORBIDDEN_MODULES = ("akko_mcp_trino.trino_client", "akko_mcp_trino.identity")


@dataclass(frozen=True)
class ContractNames:
    """Which recorded objects play which role, by the names Trino lists (lower case).

    Every table belongs to ``catalog``. Pick names that appear nowhere else in the
    provider's logs or ``check_lines()`` (a schema named like a glossary would be
    reported as a leak).
    """

    catalog: str  # a Trino catalog the provider maps
    unmapped_catalog: str  # a Trino catalog it does not map
    described: TableRef  # a known table with a described column
    partitioned: TableRef  # a known table with a partition column
    partition_column: str
    dropped: TableRef  # an entity the catalogue marks deleted (record a real one)
    unknown: TableRef  # a table the catalogue does not hold
    trap: TableRef  # a known table whose name holds a quote, dot, space, accent or non-Latin
    term_column: str  # a column of ``described`` with a term of a served glossary attached
    foreign_glossary: str  # a glossary the catalogue holds outside the allow list


@dataclass
class ContractHarness:
    """What the contract needs to run a provider without its catalogue.

    ``build(env, transport)`` returns the provider; every HTTP request it sends must go
    through ``transport``. ``replay(request)`` answers a request as the catalogue did
    when it was recorded. ``env`` holds the provider's settings, with ``secret`` among
    them (directly or through a file), and its allow lists naming the served glossary.
    """

    build: Callable[[Mapping[str, str], httpx.AsyncBaseTransport], ContextProvider]
    replay: Callable[[httpx.Request], httpx.Response]
    env: Mapping[str, str]
    secret: str
    names: ContractNames


def _require(condition: bool, message: str) -> None:
    if not condition:
        pytest.fail(message, pytrace=False)


class _Transport(httpx.AsyncBaseTransport):
    """Answers from the recordings, or as ``mode`` says; counts what is sent and in flight."""

    def __init__(self, replay: Callable[[httpx.Request], httpx.Response], secret: str) -> None:
        self.mode = _REPLAY
        self.sent: list[tuple[str, str, bytes]] = []
        self.in_flight = 0
        self._answers: dict[str, Callable[[httpx.Request], httpx.Response]] = {
            _REPLAY: replay,
            _SLOW: replay,
            _REFUSED: lambda request: httpx.Response(401, json={"error": "unauthorized"}),
            _DOWN: lambda request: httpx.Response(503, json={"error": "unavailable"}),
            _BAD_JSON: lambda request: httpx.Response(
                200, headers={"content-type": "application/json"}, content=b'{"entity": [1, 2'
            ),
            _LEAK: lambda request: httpx.Response(
                500, json={"error": f"internal error, credential {secret} rejected"}
            ),
        }

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        content = await request.aread()
        self.sent.append((request.method, str(request.url), content))
        self.in_flight += 1
        try:
            if self.mode == _SLOW:
                await asyncio.sleep(_SLOW_SECONDS)
            return self._answers[self.mode](request)
        finally:
            self.in_flight -= 1


@dataclass
class _Run:
    """A provider built for one check, on its own context loop, as the server runs it."""

    provider: ContextProvider
    transport: _Transport
    loop: ContextLoop

    def call(self, coro: Coroutine[Any, Any, Any]) -> Any:
        return self.loop.submit(coro, _SUBMIT_SECONDS)

    def describe(self, refs: Sequence[TableRef], seconds: float) -> Mapping[TableRef, Any]:
        deadline = time.monotonic() + seconds
        return self.call(self.provider.describe(list(refs), deadline=deadline))

    def terms(self, keys: Iterable[str], seconds: float) -> Mapping[str, Any]:
        deadline = time.monotonic() + seconds
        return self.call(self.provider.terms(sorted(keys), deadline=deadline))

    def error(self, refs: Sequence[TableRef], seconds: float) -> BaseException | None:
        return self.failure(lambda: self.describe(refs, seconds))

    def terms_error(self, keys: Iterable[str], seconds: float) -> BaseException | None:
        return self.failure(lambda: self.terms(keys, seconds))

    @staticmethod
    def failure(call: Callable[[], Any]) -> BaseException | None:
        try:
            call()
        except Exception as exc:  # noqa: BLE001 - the check says which error it expected
            return exc
        return None


def _check_harness(harness: ContractHarness) -> None:
    names = harness.names
    refs = _refs(names)
    columns = (names.term_column, names.partition_column)
    _require(
        all(ref == TableRef.of(ref.catalog, ref.schema, ref.table) for ref in refs)
        and all(column == column.lower() for column in columns),
        "ContractNames must be folded as Trino lists them (lower case, as TableRef.of does)",
    )
    _require(
        all(ref.catalog == names.catalog for ref in refs)
        and names.unmapped_catalog != names.catalog,
        "ContractNames: every table belongs to the mapped catalog, "
        "and the unmapped catalog differs from it",
    )
    _require(
        any(c in _TRICKY or not c.isascii() for c in names.trap.schema + names.trap.table),
        "ContractNames: the trap name must hold a quote, a dot, a space, an accent "
        "or a non-Latin character",
    )
    _require(len(harness.secret) >= 8, "the harness secret must be 8 characters at least")


@contextmanager
def _running(harness: ContractHarness, env: Mapping[str, str] | None = None) -> Iterator[_Run]:
    _check_harness(harness)
    transport = _Transport(harness.replay, harness.secret)
    provider = harness.build(harness.env if env is None else env, transport)
    loop = ContextLoop()
    loop.start()
    try:
        yield _Run(provider, transport, loop)
    finally:
        try:
            loop.submit(provider.aclose(), _SUBMIT_SECONDS)
        finally:
            loop.close()


class _Silent:
    """A catalogue on 127.0.0.1 that accepts connections and never answers.

    It never reads either: what a client sends stays queued, as at a catalogue that hangs.
    """

    def __init__(self) -> None:
        self._server = socket.create_server(("127.0.0.1", 0))
        self._server.setblocking(False)
        self.port: int = self._server.getsockname()[1]
        self._accepted: list[socket.socket] = []

    def asked(self) -> int:
        """Count the connections clients have opened so far."""
        self._accept()
        return len(self._accepted)

    def open_connections(self) -> int:
        """Count the connections their client has not closed."""
        self._accept()
        return sum(not _closed_by_client(conn) for conn in self._accepted)

    def close(self) -> None:
        for conn in self._accepted:
            conn.close()
        self._server.close()

    def _accept(self) -> None:
        while True:
            try:
                conn, _ = self._server.accept()
            except BlockingIOError:
                return
            conn.setblocking(False)
            self._accepted.append(conn)


def _closed_by_client(conn: socket.socket) -> bool:
    """Say whether the client closed ``conn``; what it sent and the catalogue left is read."""
    try:
        while conn.recv(65536):
            pass
    except OSError as error:
        # Nothing more to read: still open. A reset: closed.
        return not isinstance(error, BlockingIOError)
    return True


class _ToSilent(httpx.AsyncBaseTransport):
    """Sends every request over a real socket to a ``_Silent`` catalogue, whatever its address."""

    def __init__(self, port: int) -> None:
        self._port = port
        self._http = httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        request.url = request.url.copy_with(scheme="http", host="127.0.0.1", port=self._port)
        return await self._http.handle_async_request(request)

    async def aclose(self) -> None:
        await self._http.aclose()


@dataclass(frozen=True)
class _Silenced:
    """A provider built on a silent catalogue, its chain and their loop."""

    provider: ContextProvider
    chain: ContextChain
    loop: ContextLoop

    def alone(self, read: Coroutine[Any, Any, Any]) -> Any:
        """Run one read of the provider on the loop, without the chain."""
        return self.loop.submit(read, _SUBMIT_SECONDS)


@dataclass(frozen=True)
class _Cut:
    """A read of a silent catalogue: what it returned or raised, when, and what it left open."""

    name: str
    result: Any
    error: BaseException | None
    elapsed: float
    asked: int
    left: int


def _default_settings() -> ContextSettings:
    """The chain's settings at the server's defaults, whatever the environment says."""
    defaults: dict[str, Any] = {field.name: field.default for field in fields(Config)}
    return ContextSettings(
        **{field.name: defaults[f"context_{field.name}"] for field in fields(ContextSettings)}
    )


class _Lines(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))


@contextmanager
def _captured_logs() -> Iterator[list[str]]:
    """Every log line, tracebacks included; ``httpx`` at ``WARNING``, as the server sets it."""
    handler = _Lines()
    root, client = logging.getLogger(), logging.getLogger("httpx")
    levels = root.level, client.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    client.setLevel(logging.WARNING)
    try:
        yield handler.lines
    finally:
        root.removeHandler(handler)
        root.setLevel(levels[0])
        client.setLevel(levels[1])


def _refs(names: ContractNames) -> list[TableRef]:
    return [names.described, names.partitioned, names.dropped, names.unknown, names.trap]


def _object_names(names: ContractNames) -> set[str]:
    refs = _refs(names)
    columns = {names.term_column, names.partition_column}
    return {ref.schema for ref in refs} | {ref.table for ref in refs} | columns


def _columns(contexts: Iterable[TableContext]) -> list[ColumnContext]:
    return [column for context in contexts for column in context.columns.values()]


def _term_keys(contexts: Iterable[TableContext]) -> set[str]:
    contexts = list(contexts)
    return {key for c in contexts for key in c.terms} | {
        key for c in _columns(contexts) for key in c.terms
    }


def _classifications(contexts: Iterable[TableContext]) -> list[Labelled]:
    contexts = list(contexts)
    return [tag for c in contexts for tag in c.tags] + [
        label for c in _columns(contexts) for label in c.classifications
    ]


def _properties(contexts: Iterable[TableContext]) -> list[Labelled]:
    contexts = list(contexts)
    return [p for c in contexts for p in c.properties.values()] + [
        p for c in _columns(contexts) for p in c.properties.values()
    ]


def _items(env: Mapping[str, str], secret: str) -> set[str]:
    """The names the settings give, the secret's setting aside."""
    return {
        part.strip()
        for value in env.values()
        if secret not in value
        for part in value.split(",")
        if part.strip()
    }


def _secret_forms(secret: str) -> set[str]:
    """The secret as written, JSON-escaped, URL-encoded, as Python writes it, and in base64."""
    return {
        secret,
        json.dumps(secret)[1:-1],
        json.dumps(secret, ensure_ascii=False)[1:-1],
        repr(secret)[1:-1],
        quote(secret, safe=""),
        quote_plus(secret, safe=""),
        base64.b64encode(secret.encode("utf-8")).decode("ascii"),
    }


def _holds_secret(text: str, secret: str) -> bool:
    return any(form in text for form in _secret_forms(secret))


def _describes(harness: ContractHarness, env: Mapping[str, str], seconds: float) -> bool:
    """Whether the provider, built from ``env``, still describes the described table."""
    described = harness.names.described
    try:
        with _running(harness, env) as run:
            return described in run.describe([described], seconds)
    except Exception:  # noqa: BLE001 - a setting the provider cannot do without
        return False


def _without_allow_lists(harness: ContractHarness, seconds: float) -> dict[str, str]:
    """The settings left once every one is removed, one by one, that the provider can do
    without and still describe the described table: what is left builds the provider and
    maps the catalog, and names no set to serve."""
    env = dict(harness.env)
    for key in sorted(env):
        fewer = {k: v for k, v in env.items() if k != key}
        if _describes(harness, fewer, seconds):
            env = fewer
    return env


# ---- a value is exactly a core type, holding only its declared fields


def _plain(value: Any, kind: Any) -> bool:
    return type(value) is kind and set(vars(value)) == {f.name for f in fields(kind)}


def _texts(values: object) -> bool:
    return type(values) is tuple and all(type(v) is str for v in values)


def _label(value: Any) -> bool:
    return _plain(value, Labelled) and _texts((value.name, value.set))


def _labels(values: object) -> bool:
    return type(values) is tuple and all(map(_label, values))


def _property_map(values: object) -> bool:
    return isinstance(values, Mapping) and all(
        type(key) is str and _label(value) for key, value in values.items()
    )


def _ref(value: Any) -> bool:
    return _plain(value, TableRef) and _texts((value.catalog, value.schema, value.table))


def _join(value: Any) -> bool:
    return (
        _plain(value, Join)
        and _texts(value.columns)
        and _ref(value.target)
        and _texts(value.target_columns)
    )


def _column(value: Any) -> bool:
    return (
        _plain(value, ColumnContext)
        and type(value.description) is str
        and _labels(value.classifications)
        and _texts(value.terms)
        and _property_map(value.properties)
    )


def _table(value: Any) -> bool:
    return (
        _plain(value, TableContext)
        and _texts((value.description, value.owner, value.tier))
        and _labels(value.tags)
        and _texts(value.terms)
        and _property_map(value.properties)
        and _texts(value.grain)
        and type(value.joins) is tuple
        and all(map(_join, value.joins))
        and isinstance(value.columns, Mapping)
        and all(type(k) is str and _column(c) for k, c in value.columns.items())
    )


def _term_ref(value: Any) -> bool:
    return _plain(value, TermRef) and _texts(
        (value.key, value.name, value.glossary, value.relation)
    )


def _term(value: Any) -> bool:
    return (
        _plain(value, Term)
        and _texts((value.key, value.name, value.glossary, value.definition, value.status))
        and _texts(value.categories)
        and type(value.relations) is tuple
        and all(map(_term_ref, value.relations))
    )


def _folded(ref: TableRef, context: TableContext) -> list[str]:
    """Every name of a returned table that is not folded as Trino lists it."""
    names = [ref.catalog, ref.schema, ref.table, *context.columns, *context.grain]
    names += [
        name
        for join in context.joins
        for name in (
            *join.columns,
            join.target.catalog,
            join.target.schema,
            join.target.table,
            *join.target_columns,
        )
    ]
    return [name for name in names if name != name.lower()]


class ContextContract:
    """The checks every context provider passes; subclass it and give a ``harness`` fixture.

    ``REPLY_MARGIN_SECONDS`` is how late past its deadline a provider may hand back
    control, chosen for a busy CI event loop. ``DEADLINE_SECONDS`` is the deadline of a
    call that should succeed; ``SLOW_DEADLINE_SECONDS`` that of a call to a catalogue
    that does not answer in time. Both are class attributes: an adapter whose tests run on
    a slow CI raises them in its subclass (``DEADLINE_SECONDS = 15.0``), and the contract
    reads them on every check. Keep ``SLOW_DEADLINE_SECONDS`` well under 30 seconds, the
    time the contract's slow catalogue takes to answer. ``RESET_SECONDS`` bounds the
    optional ``reset()``, which the server calls from its signal handler.
    """

    REPLY_MARGIN_SECONDS = 0.5
    DEADLINE_SECONDS = 5.0
    SLOW_DEADLINE_SECONDS = 1.0
    # The longest an optional ``reset()`` may take: it runs in the server's signal handler.
    RESET_SECONDS = 0.1

    def _skip_without_http(self, harness: ContractHarness) -> None:
        with _running(harness) as run:
            run.describe([harness.names.described], self.DEADLINE_SECONDS)
            sent = bool(run.transport.sent)
        if not sent:
            pytest.skip("no HTTP")

    def _everything(self, run: _Run, names: ContractNames) -> tuple[Mapping, Mapping]:
        found = run.describe(_refs(names), self.DEADLINE_SECONDS)
        return found, run.terms(_term_keys(found.values()), self.DEADLINE_SECONDS)

    def test_describe_returns_the_context_partition_columns_included(
        self, harness: ContractHarness
    ) -> None:
        """``describe`` returns the known tables with their columns, and omits an unknown one."""
        names = harness.names
        with _running(harness) as run:
            found = run.describe(
                [names.described, names.partitioned, names.unknown], self.DEADLINE_SECONDS
            )
        _require(
            names.described in found and names.partitioned in found,
            "describe must return the described and the partitioned tables",
        )
        described = found[names.described]
        _require(
            any(column.description for column in described.columns.values()),
            "the described table must come with a described column",
        )
        _require(
            bool(described.columns.get(names.term_column, ColumnContext()).terms),
            "the term column must come with its term",
        )
        _require(
            names.partition_column in found[names.partitioned].columns,
            "a partition column must come with the other columns",
        )
        _require(names.unknown not in found, "a table the catalogue does not hold must be left out")

    def test_a_deleted_entity_is_never_returned(self, harness: ContractHarness) -> None:
        """An entity deleted in the catalogue is never returned, alone or in a batch."""
        names = harness.names
        with _running(harness) as run:
            alone = run.describe([names.dropped], self.DEADLINE_SECONDS)
            batch = run.describe([names.described, names.dropped], self.DEADLINE_SECONDS)
        _require(
            names.dropped not in alone and names.dropped not in batch,
            "a deleted entity must never be returned",
        )

    def test_an_unmapped_catalog_sends_no_request(self, harness: ContractHarness) -> None:
        """``maps`` is false for the unmapped catalog, and nothing is asked about it."""
        names = harness.names
        with _running(harness) as run:
            mapped = run.provider.maps(names.catalog)
            unmapped = run.provider.maps(names.unmapped_catalog)
            ref = TableRef(names.unmapped_catalog, names.described.schema, names.described.table)
            found = run.describe([ref], self.DEADLINE_SECONDS)
            sent = list(run.transport.sent)
        _require(mapped, "maps() must be true for the mapped catalog")
        _require(not unmapped, "maps() must be false for the unmapped catalog")
        _require(
            not found and not sent,
            "a table of an unmapped catalog must be neither returned nor asked for",
        )

    def test_nothing_outside_served_sets_is_returned(self, harness: ContractHarness) -> None:
        """Every glossary term, classification, property and owner returned is in a served set."""
        names = harness.names
        with _running(harness) as run:
            sets = run.provider.served_sets()
            found, terms = self._everything(run, names)
        contexts = list(found.values())
        glossaries = sets[GLOSSARY]
        _require(
            names.foreign_glossary not in glossaries, "the foreign glossary must not be served"
        )
        _require(
            _term_keys(contexts) <= set(terms),
            "every term key describe returns must be a term that terms() returns",
        )
        _require(
            all(
                {term.glossary, *(r.glossary for r in term.relations)} <= glossaries
                for term in terms.values()
            ),
            "a term, and a term it relates to, must belong to a served glossary",
        )
        _require(
            all(label.set in sets[CLASSIFICATION] for label in _classifications(contexts)),
            "a tag or a classification must belong to a served set",
        )
        _require(
            all(label.set in sets[PROPERTY] for label in _properties(contexts)),
            "a property must belong to a served set",
        )
        _require(
            bool(sets[OWNER]) or not any(context.owner for context in contexts),
            "an owner may be returned only when the owner is served",
        )

    def test_served_sets_reflect_the_configuration(self, harness: ContractHarness) -> None:
        """A glossary, classification or property set is served only when the settings name
        it; without its allow lists, a provider still describes the table and serves none."""
        names = harness.names
        with _running(harness) as run:
            sets = run.provider.served_sets()
        _require(
            set(sets) == _KINDS and all(type(value) is frozenset for value in sets.values()),
            "served_sets() must give a frozenset for each of glossary, classification, "
            "property and owner",
        )
        _require(
            sets[GLOSSARY] | sets[CLASSIFICATION] | sets[PROPERTY]
            <= _items(harness.env, harness.secret),
            "a set may be served only when the configuration names it",
        )
        _require(
            bool(sets[GLOSSARY]), "the configuration of the contract must serve the term's glossary"
        )
        with _running(harness, _without_allow_lists(harness, self.DEADLINE_SECONDS)) as run:
            left = run.provider.served_sets()
            found = run.describe([names.described], self.DEADLINE_SECONDS)
        context = found.get(names.described)
        _require(
            context is not None
            and bool(context.description or context.columns)
            and not (left[GLOSSARY] | left[CLASSIFICATION] | left[PROPERTY])
            and not _term_keys([context])
            and not _classifications([context])
            and not _properties([context]),
            "without its allow lists, a provider must still describe the table, and serve "
            "no glossary, classification or property set, nor return a term, a "
            "classification or a property",
        )

    def test_the_owner_follows_the_configuration(self, harness: ContractHarness) -> None:
        """A provider that declares the owner serves it only when its settings ask for it; one
        whose catalogue has no notion of an owner declares none, and is not checked."""
        names = harness.names
        with _running(harness) as run:
            declared = bool(run.provider.served_sets()[OWNER])
        if not declared:
            pytest.skip("the provider declares no owner: its catalogue has no notion of one")
        with _running(harness, _without_allow_lists(harness, self.DEADLINE_SECONDS)) as run:
            left = run.provider.served_sets()
            found = run.describe([names.described], self.DEADLINE_SECONDS)
        _require(
            not left[OWNER] and not any(context.owner for context in found.values()),
            "the owner must not be served once the settings that serve it are removed",
        )

    def test_no_type_carries_a_data_value(self, harness: ContractHarness) -> None:
        """What is returned is exactly the core types, holding names and labels, never data."""
        with _running(harness) as run:
            found, terms = self._everything(run, harness.names)
        _require(
            all(_ref(ref) and _table(context) for ref, context in found.items())
            and all(type(key) is str and _term(term) for key, term in terms.items()),
            "describe and terms must return exactly the core types, with their own fields only",
        )

    def test_a_slow_catalogue_returns_before_the_deadline_plus_margin_and_closes_its_connection(
        self, harness: ContractHarness
    ) -> None:
        """A catalogue that does not answer costs the deadline and the margin, no more, and the
        request is not left running."""
        self._skip_without_http(harness)
        with _running(harness) as run:
            run.transport.mode = _SLOW
            started = time.monotonic()
            run.error([harness.names.described], self.SLOW_DEADLINE_SECONDS)
            elapsed = time.monotonic() - started
            sent, in_flight = len(run.transport.sent), run.transport.in_flight
        _require(sent > 0, "the slow catalogue must have been asked")
        _require(
            elapsed <= self.SLOW_DEADLINE_SECONDS + self.REPLY_MARGIN_SECONDS,
            f"describe handed back control {elapsed:.2f}s after it started, past its deadline "
            f"of {self.SLOW_DEADLINE_SECONDS}s plus {self.REPLY_MARGIN_SECONDS}s",
        )
        _require(in_flight == 0, "a request past the deadline must be cancelled, not left open")

    def test_a_read_past_its_deadline_closes_its_connection(self, harness: ContractHarness) -> None:
        """Over a real socket to a catalogue that never answers, the provider alone keeps its
        deadline, raises ``ProviderUnavailable`` and closes its connection."""
        refs = [harness.names.described]
        cut = self._read_a_silent_catalogue(
            harness, lambda run, deadline: run.alone(run.provider.describe(refs, deadline=deadline))
        )
        self._require_closed_alone(cut, "describe")

    def test_a_read_the_chain_cuts_at_its_deadline_closes_its_connection(
        self, harness: ContractHarness
    ) -> None:
        """Behind the core's chain, as the server runs it, and over a real socket to a catalogue
        that never answers, a read costs the deadline and the chain's grace, no more, and its
        connection is closed: no request is left pending at the catalogue."""
        refs = [harness.names.described]
        cut = self._read_a_silent_catalogue(
            harness, lambda run, deadline: run.chain.gather(refs, deadline=deadline)
        )
        self._require_closed_behind_the_chain(cut, "describe")

    def test_a_terms_read_past_its_deadline_closes_its_connection(
        self, harness: ContractHarness
    ) -> None:
        """The same as a describe, for the glossary terms: the provider alone, over a real
        socket to a catalogue that never answers, raises in time and closes its connection."""
        keys = self._recorded_term_keys(harness)
        cut = self._read_a_silent_catalogue(
            harness, lambda run, deadline: run.alone(run.provider.terms(keys, deadline=deadline))
        )
        self._require_closed_alone(cut, "terms")

    def test_a_terms_read_the_chain_cuts_at_its_deadline_closes_its_connection(
        self, harness: ContractHarness
    ) -> None:
        """The same as a describe, for the glossary terms, behind the core's chain."""
        keys = self._recorded_term_keys(harness)
        cut = self._read_a_silent_catalogue(
            harness,
            lambda run, deadline: run.chain.terms({run.provider.name: keys}, deadline=deadline),
        )
        self._require_closed_behind_the_chain(cut, "terms")

    def _recorded_term_keys(self, harness: ContractHarness) -> list[str]:
        """The keys of the terms the recorded catalogue attaches to the tables of the names."""
        self._skip_without_http(harness)
        with _running(harness) as run:
            found = run.describe(_refs(harness.names), self.DEADLINE_SECONDS)
        keys = sorted(_term_keys(found.values()))
        _require(
            bool(keys),
            "ContractNames: the term column must carry a term of a served glossary",
        )
        return keys

    def _read_a_silent_catalogue(
        self, harness: ContractHarness, read: Callable[["_Silenced", float], Any]
    ) -> "_Cut":
        """Run ``read`` with the slow deadline against a catalogue that never answers, over a
        real socket; then count the connections still open, given the margin to close."""
        self._skip_without_http(harness)
        settings = replace(_default_settings(), timeout_seconds=self.SLOW_DEADLINE_SECONDS)
        got: list = []
        # Each resource is closed only once it is open, the last opened first.
        with ExitStack() as opened:
            silent = _Silent()
            opened.callback(silent.close)
            loop = ContextLoop()
            loop.start()
            opened.callback(loop.close)
            provider = harness.build(harness.env, _ToSilent(silent.port))
            opened.callback(_Run.failure, lambda: loop.submit(provider.aclose(), _SUBMIT_SECONDS))
            run = _Silenced(provider, ContextChain([provider], settings, loop=loop), loop)
            started = time.monotonic()
            deadline = started + self.SLOW_DEADLINE_SECONDS
            error = _Run.failure(lambda: got.append(read(run, deadline)))
            elapsed = time.monotonic() - started
            # The close is sent on the loop's next turns: given the margin, never more.
            end = time.monotonic() + self.REPLY_MARGIN_SECONDS
            while (left := silent.open_connections()) and time.monotonic() < end:
                time.sleep(0.01)
            asked = silent.asked()
        return _Cut(provider.name, got[0] if got else None, error, elapsed, asked, left)

    def _require_closed_alone(self, cut: "_Cut", read: str) -> None:
        _require(cut.asked > 0, "the silent catalogue must have been asked")
        _require(
            cut.elapsed <= self.SLOW_DEADLINE_SECONDS + self.REPLY_MARGIN_SECONDS,
            f"over a socket, {read} handed back control {cut.elapsed:.2f}s after it started, "
            f"past its deadline of {self.SLOW_DEADLINE_SECONDS}s plus "
            f"{self.REPLY_MARGIN_SECONDS}s",
        )
        _require(
            isinstance(cut.error, ProviderUnavailable),
            f"{read} of a catalogue past the deadline must raise ProviderUnavailable",
        )
        _require(
            cut.left == 0,
            f"a {read} read past its deadline must close its connection: {cut.left} of "
            f"{cut.asked} left open, the request still pending at the catalogue",
        )

    def _require_closed_behind_the_chain(self, cut: "_Cut", read: str) -> None:
        _require(cut.asked > 0, "the silent catalogue must have been asked")
        _require(
            cut.error is None,
            f"behind the chain, {read} raised {type(cut.error).__name__}: the chain must "
            "hand back control within its grace",
        )
        _require(
            cut.elapsed <= self.SLOW_DEADLINE_SECONDS + self.REPLY_MARGIN_SECONDS,
            f"behind the chain, {read} handed back control {cut.elapsed:.2f}s after it "
            f"started, past its deadline of {self.SLOW_DEADLINE_SECONDS}s plus "
            f"{self.REPLY_MARGIN_SECONDS}s",
        )
        _require(
            cut.result.statuses[cut.name] == UNAVAILABLE,
            f"a {read} read past its deadline must say unavailable",
        )
        _require(
            cut.left == 0,
            f"a {read} read cut at its deadline must close its connection: {cut.left} of "
            f"{cut.asked} left open, the request still pending at the catalogue",
        )

    def test_a_401_raises_refused_and_is_not_retried(self, harness: ContractHarness) -> None:
        """A refused account raises ``ProviderRefused`` and no request is sent twice; the core
        keeps the source closed until the long delay or the signal."""
        self._skip_without_http(harness)
        with _running(harness) as run:
            run.transport.mode = _REFUSED
            error = run.error([harness.names.described], self.DEADLINE_SECONDS)
            sent = list(run.transport.sent)
        _require(isinstance(error, ProviderRefused), "a 401 must raise ProviderRefused")
        _require(len(set(sent)) == len(sent), "a refused request must not be sent again")

    def test_a_timeout_or_bad_json_raises_unavailable(self, harness: ContractHarness) -> None:
        """A catalogue past the deadline, down, or answering a body that is not JSON raises
        ``ProviderUnavailable``."""
        self._skip_without_http(harness)
        for mode in (_SLOW, _DOWN, _BAD_JSON):
            with _running(harness) as run:
                run.transport.mode = mode
                error = run.error([harness.names.described], self.SLOW_DEADLINE_SECONDS)
            _require(
                isinstance(error, ProviderUnavailable),
                f"a catalogue that answers {mode!r} must raise ProviderUnavailable",
            )

    def test_logs_and_check_lines_never_hold_the_secret_or_an_object_name(
        self, harness: ContractHarness
    ) -> None:
        """Neither the logs nor ``check_lines``, read before and after each catalogue error,
        hold the secret or a schema, table or column name, and an error answer that holds
        the secret leaves it in no exception."""
        names, secret = harness.names, harness.secret
        with _captured_logs() as lines, _running(harness) as run:
            found, _ = self._everything(run, names)
            keys = _term_keys(found.values())
            checks = run.provider.check_lines()
            http = bool(run.transport.sent)
            errors, after = [], []
            for mode in (_LEAK, _REFUSED, _DOWN, _BAD_JSON):
                run.transport.mode = mode
                errors += [
                    run.error([names.described], self.DEADLINE_SECONDS),
                    run.terms_error(keys, self.DEADLINE_SECONDS),
                ]
                after.append(run.provider.check_lines())
        if http:
            _require(
                all(isinstance(error, ProviderError) for error in errors[:2]),
                "an error answer must raise a ProviderError",
            )
            _require(
                not any(
                    _holds_secret("".join(traceback.format_exception(error)), secret)
                    for error in errors
                    if error is not None
                ),
                "an exception, its causes included, must never hold the secret",
            )
        _require(
            all(type(c) is list and all(type(line) is str for line in c) for c in [checks, *after]),
            "check_lines() must return a list of texts",
        )
        text = "\n".join(lines + checks + [line for c in after for line in c])
        _require(
            not _holds_secret(text, secret), "the logs and check_lines() must never hold the secret"
        )
        leaked = sorted(name for name in _object_names(names) if name in text)
        _require(not leaked, f"the logs and check_lines() must never hold an object name: {leaked}")

    def test_a_factory_refusal_never_holds_the_secret(self, harness: ContractHarness) -> None:
        """A factory that refuses a malformed setting, by whatever exception, does not put the
        secret in its message, the causes included: ``--check`` prints that message. Each setting
        that holds the secret is given back padded with white space, then in quotes."""
        secret = harness.secret
        holders = [name for name, value in harness.env.items() if secret in value]
        if not holders:
            pytest.skip("no setting holds the secret")
        refusals = []
        for name in holders:
            value = harness.env[name]
            for malformed in (f" {value}\n", f'"{value}"'):
                try:
                    with _running(harness, {**harness.env, name: malformed}):
                        pass
                except Exception as error:
                    refusals.append("".join(traceback.format_exception(error)))
        _require(
            not any(_holds_secret(text, secret) for text in refusals),
            "a factory's exception must never hold the secret, as written or encoded",
        )

    def test_the_adapter_imports_neither_the_trino_client_nor_the_identity(
        self, harness: ContractHarness
    ) -> None:
        """The provider's module, imported alone, loads nothing of the core's Trino side."""
        with _running(harness) as run:
            module = type(run.provider).__module__
        code = (
            "import importlib, sys\n"
            "importlib.import_module(sys.argv[1])\n"
            "print(' '.join(m for m in sys.argv[2:] if m in sys.modules))\n"
        )
        done = subprocess.run(
            [sys.executable, "-c", code, module, *_FORBIDDEN_MODULES],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        _require(done.returncode == 0, f"importing {module} alone failed: {done.stderr[-500:]}")
        _require(
            not done.stdout.strip(),
            f"importing {module} loads what an adapter must not: {done.stdout.strip()}",
        )

    def test_trap_names_are_looked_up_as_trino_lists_them(self, harness: ContractHarness) -> None:
        """A name with a quote, a dot, a space or an accent is found under that exact name."""
        trap = harness.names.trap
        with _running(harness) as run:
            found = run.describe([trap], self.DEADLINE_SECONDS)
        _require(set(found) == {trap}, "the trap table must be found under its exact name")

    def test_returned_names_are_folded_as_trino_lists_them(self, harness: ContractHarness) -> None:
        """Tables come back under the references asked, and every name is folded to lower case,
        because the core compares names exactly."""
        names = harness.names
        with _running(harness) as run:
            found = run.describe(_refs(names), self.DEADLINE_SECONDS)
        _require(set(found) <= set(_refs(names)), "describe must answer only the tables asked")
        unfolded = sorted(
            {name for ref, context in found.items() for name in _folded(ref, context)}
        )
        _require(
            not unfolded,
            f"names must be folded as Trino lists them (lower case): {len(unfolded)} are not",
        )

    def test_a_reset_hook_is_synchronous_quick_and_quiet(self, harness: ContractHarness) -> None:
        """A provider that defines the optional ``reset()`` defines it as a plain function
        that returns ``None`` within ``RESET_SECONDS``, sends no request and does not raise,
        since the server calls it from its signal handler; one without it is not checked."""
        with _running(harness) as run:
            hook = getattr(run.provider, "reset", None)
            if hook is None:
                pytest.skip("the provider has no reset(): nothing to check")
            _require(
                callable(hook)
                and not inspect.iscoroutinefunction(hook)
                and not inspect.isasyncgenfunction(hook),
                "reset() must be a plain function, not a coroutine function: the server's "
                "signal handler never awaits it",
            )
            run.describe([harness.names.described], self.DEADLINE_SECONDS)
            sent = len(run.transport.sent)
            raised = False
            started = time.monotonic()
            try:
                result = hook()
            except Exception:  # noqa: BLE001 - the check says it, never the error's text
                result, raised = None, True
            elapsed = time.monotonic() - started
            asked = len(run.transport.sent) - sent
        _require(not raised, "reset() must not raise")
        _require(result is None, "reset() must return None")
        _require(
            elapsed <= self.RESET_SECONDS,
            f"reset() must return within {self.RESET_SECONDS}s; it took {elapsed:.2f}s",
        )
        _require(not asked, "reset() must send no request")

    def test_snapshot_pages_without_duplicate_or_loss(self, harness: ContractHarness) -> None:
        """``snapshot`` is checked from 0.8.x, with pre-warming; no provider offers it in 0.8."""
        pytest.skip("snapshot arrives with pre-warming in 0.8.x; nothing to check in 0.8")


# ---- the gate, end to end: the provider, the core's tools and a fake Trino

_MODES = ("jwt", "impersonation")
_HIDDEN_TABLE, _HIDDEN_COLUMN, _HIDDEN_JOIN = (
    "the hidden table",
    "the hidden column",
    "the hidden join target",
)
_SCENARIOS = (_HIDDEN_TABLE, _HIDDEN_COLUMN, _HIDDEN_JOIN)
# How Trino answers an object it hides: as missing (metadata filtered) or refused (rules).
_FILTERS, _REFUSES = "a Trino that filters", "a Trino that refuses"
_BEHAVIOURS = (_FILTERS, _REFUSES)
# Keys of a reply that come from Trino's error, not from the core.
_TRINO_ERROR = frozenset({"error", "kind", "identifier"})
_SEES_ALL, _CALLER = "gate-sees-all", "gate-caller"
_CANARY_SCHEMA, _CANARY_TABLE = "mcp_gate_canary", "sentinel"
# A text shorter than this is too common to be looked for in what the caller receives.
_MIN_TEXT = 4
# What an agent may write as a catalog, a schema or a table (the tools refuse the rest).
_AGENT_NAME = re.compile(r"[A-Za-z0-9_-]+")
_STATUSES = frozenset(
    {OK, PARTIAL, NOT_FOUND, NOT_MAPPED, UNAVAILABLE, REFUSED, ERROR, NOT_CHECKED}
)


@dataclass
class GateHarness:
    """The contract's harness, and which of its recorded objects the gate hides from a caller.

    ``hidden_table`` is a table the catalogue describes; ``hidden_join_target`` the target
    of a join the catalogue gives ``names.described``; ``hidden_column`` a column of
    ``names.described`` the catalogue describes. A catalogue with no notion of a join leaves
    ``hidden_join_target`` to ``None``: the join scenario is then left out of every check,
    its own check is skipped with the reason, and the contract fails if the provider gives a
    join for any table it describes, so that a provider with joins cannot leave them
    unchecked. ``names.unknown`` plays the missing
    table. Each is written as Trino lists it, at least 4 characters long, and as an agent
    names a table (letters, digits, ``_`` and ``-``); none of them is ``names.described``.
    Its name must be part of no name or text the caller is allowed to see: the contract
    looks for it, and for every text the catalogue gives it alone, in all the caller
    receives.
    """

    contract: ContractHarness
    hidden_table: TableRef
    hidden_column: str
    hidden_join_target: TableRef | None = None


@dataclass
class _Answer:
    """One tool call of a gate run: the reply, the audit line and the statements it sent."""

    user: str
    tool: str
    args: Mapping[str, str]
    text: str
    line: Mapping[str, Any]
    statements: list

    @property
    def reply(self) -> dict:
        return json.loads(self.text)

    def asked(self) -> TableRef | None:
        if not {"catalog", "schema", "table"} <= set(self.args):
            return None
        return TableRef.of(self.args["catalog"], self.args["schema"], self.args["table"])

    def scrubbed(self, text: str) -> str:
        """``text`` without the table the agent asked for, by name."""
        asked = self.asked()
        return text.replace(_dotted(asked), "<asked>") if asked is not None else text

    def line_text(self) -> str:
        return json.dumps(self.line, ensure_ascii=False)


@dataclass
class _Server:
    """The core's tools over one provider and a fake Trino, called as a verified user."""

    tools: Mapping[str, Callable[..., str]]
    fake: Any
    audit: Any
    metrics: Any
    provider: str

    def call(self, user: str, tool: str, args: Mapping[str, str]) -> _Answer:
        from ..audit import reset_current_request_id, set_current_request_id
        from ..auth import Principal
        from ..identity import (
            reset_current_bearer,
            reset_current_principal,
            set_current_bearer,
            set_current_principal,
        )

        first, events = len(self.fake.statements), len(self.audit.events)
        principal = set_current_principal(Principal(subject=user, token_id=f"{user}-jti"))
        bearer = set_current_bearer(_bearer(user))
        request = set_current_request_id(f"gate-{events}")
        try:
            text = self.tools[tool](**args)
        finally:
            reset_current_request_id(request)
            reset_current_bearer(bearer)
            reset_current_principal(principal)
        event = self.audit.events[events]
        line = {
            **event.as_dict(),
            "tables": list(event.call.tables),
            "metadata_served": list(event.call.metadata_served),
        }
        return _Answer(user, tool, dict(args), text, line, self.fake.statements[first:])

    def metrics_text(self) -> str:
        return self.metrics.render()[0].decode("utf-8")


def _dotted(ref: TableRef) -> str:
    return f"{ref.catalog}.{ref.schema}.{ref.table}"


def _bearer(user: str) -> str:
    """The verified bearer of ``user`` in a gate run: one per caller."""
    return f"bearer-of-{user}"


def _trino_token(bearer: str) -> str:
    """The token for Trino the gate run's token exchange gives for ``bearer``."""
    return f"token-for-trino:{bearer}"


def _table_args(ref: TableRef) -> dict[str, str]:
    return {"catalog": ref.catalog, "schema": ref.schema, "table": ref.table}


def _tables(gate: GateHarness) -> list[TableRef]:
    """The tables of the harness: the described one, the hidden one, the join target if any."""
    target = [] if gate.hidden_join_target is None else [gate.hidden_join_target]
    return list(dict.fromkeys([gate.contract.names.described, gate.hidden_table, *target]))


def _scenarios(gate: GateHarness) -> tuple[str, ...]:
    """The scenarios a harness can play: the join only when the catalogue gives one."""
    if gate.hidden_join_target is None:
        return (_HIDDEN_TABLE, _HIDDEN_COLUMN)
    return _SCENARIOS


def _calls(gate: GateHarness) -> list[tuple[str, dict[str, str]]]:
    """What the caller of a gate run asks: each table, then each schema, of the harness."""
    tables = _tables(gate)
    schemas = dict.fromkeys((ref.catalog, ref.schema) for ref in tables)
    return [("describe_table", _table_args(ref)) for ref in tables] + [
        ("describe_schema", {"catalog": catalog, "schema": schema}) for catalog, schema in schemas
    ]


def _check_gate(gate: GateHarness, found: Mapping[TableRef, TableContext]) -> None:
    names = gate.contract.names
    target = gate.hidden_join_target
    hidden = [gate.hidden_table] + ([] if target is None else [target])
    refs = [*hidden, names.described, names.unknown]
    _require(
        all(ref == TableRef.of(ref.catalog, ref.schema, ref.table) for ref in refs)
        and gate.hidden_column == gate.hidden_column.lower(),
        "GateHarness names must be folded as Trino lists them (lower case, as TableRef.of does)",
    )
    _require(
        all(
            len(name) >= _MIN_TEXT for name in (*(ref.table for ref in hidden), gate.hidden_column)
        ),
        "GateHarness: each hidden name must be 4 characters at least",
    )
    _require(
        all(_AGENT_NAME.fullmatch(part) for ref in refs for part in _table_args(ref).values())
        and names.described not in hidden
        and names.unknown != gate.hidden_table,
        "GateHarness: the tables are named as an agent names them (letters, digits, _ and -), "
        "and neither hidden table is the described one nor the unknown one",
    )
    _require(
        names.described in found and gate.hidden_table in found,
        "GateHarness: the hidden table must be a table the catalogue describes, "
        "as is the described table",
    )
    described = found[names.described]
    _require(
        gate.hidden_column in described.columns,
        "GateHarness: the hidden column must be a column the catalogue describes "
        "for the described table",
    )
    if target is None:
        _require(
            not any(ctx.joins for ctx in found.values()),
            "the provider gives joins: set hidden_join_target, so that the join scenario "
            "is checked",
        )
        return
    _require(
        target in found and any(join.target == target for join in described.joins),
        "GateHarness: the hidden join target must be the target of a join the catalogue "
        "gives the described table, and a table it describes",
    )


def _trino_columns(found: Mapping[TableRef, TableContext]) -> dict[TableRef, tuple[str, ...]]:
    """The columns Trino has, for a caller who sees everything: every column the catalogue
    names for a table, in its context, its grain, its joins, or as the target of a join."""
    columns: dict[TableRef, list[str]] = {
        ref: [*ctx.columns, *ctx.grain, *(c for join in ctx.joins for c in join.columns)]
        for ref, ctx in found.items()
    }
    for ctx in found.values():
        for join in ctx.joins:
            if join.target in columns:
                columns[join.target] += join.target_columns
    return {ref: tuple(dict.fromkeys(names)) for ref, names in columns.items()}


def _rights(
    scenario: str, gate: GateHarness, everything: Mapping[TableRef, tuple[str, ...]]
) -> dict[TableRef, tuple[str, ...]]:
    """What the caller of ``scenario`` sees: everything but the object it hides."""
    seen = dict(everything)
    if scenario == _HIDDEN_TABLE:
        seen.pop(gate.hidden_table)
    elif scenario == _HIDDEN_JOIN:
        assert gate.hidden_join_target is not None  # the scenario needs a join target
        seen.pop(gate.hidden_join_target)
    else:
        described = gate.contract.names.described
        seen[described] = tuple(c for c in seen[described] if c != gate.hidden_column)
    return seen


def _term_texts(keys: Iterable[str], terms: Mapping[str, Term]) -> set[str]:
    return {
        text
        for key in keys
        if key in terms
        for text in (terms[key].name, terms[key].definition, *terms[key].categories)
    }


def _labelled_texts(labels: Iterable[Labelled], properties: Mapping[str, Labelled]) -> set[str]:
    return (
        {label.name for label in labels}
        | set(properties)
        | {value.name for value in properties.values()}
    )


def _column_texts(name: str, column: ColumnContext, terms: Mapping[str, Term]) -> set[str]:
    return (
        {name, column.description}
        | _labelled_texts(column.classifications, column.properties)
        | _term_texts(column.terms, terms)
    )


def _table_texts(
    ref: TableRef,
    ctx: TableContext,
    terms: Mapping[str, Term],
    seen: Mapping[TableRef, tuple[str, ...]] | None = None,
) -> set[str]:
    """Every text the catalogue gives ``ref``; with ``seen``, only what a caller who sees
    ``seen`` may be served of it: its visible columns, a grain all visible, joins whose
    columns and target are visible."""
    columns = seen.get(ref, ()) if seen is not None else tuple(ctx.columns)

    def visible_join(join: Join) -> bool:
        if seen is None:
            return True
        target = seen.get(join.target)
        return (
            target is not None
            and all(c in columns for c in join.columns)
            and all(c in target for c in join.target_columns)
        )

    joins = [join for join in ctx.joins if visible_join(join)]
    texts = {ref.table, ctx.description, ctx.owner, ctx.tier}
    texts |= _labelled_texts(ctx.tags, ctx.properties) | _term_texts(ctx.terms, terms)
    if all(c in columns for c in ctx.grain):
        texts |= set(ctx.grain)
    for join in joins:
        texts |= {join.target.table, *join.columns, *join.target_columns}
    for name, column in ctx.columns.items():
        if name in columns:
            texts |= _column_texts(name, column, terms)
    return texts


def _hidden_texts(
    scenario: str,
    gate: GateHarness,
    found: Mapping[TableRef, TableContext],
    terms: Mapping[str, Term],
    seen: Mapping[TableRef, tuple[str, ...]],
) -> tuple[str, set[str]]:
    """The name of the object ``scenario`` hides, and every text the catalogue gives it
    that is part of nothing the caller may see: none may reach the caller."""
    if scenario == _HIDDEN_TABLE:
        table = gate.hidden_table
        name, texts = table.table, _table_texts(table, found[table], terms)
    elif scenario == _HIDDEN_JOIN:
        target = gate.hidden_join_target
        assert target is not None  # the scenario needs a join target
        name, texts = target.table, _table_texts(target, found[target], terms)
    else:
        described = found[gate.contract.names.described]
        name = gate.hidden_column
        texts = _column_texts(name, described.columns[name], terms)
    visible = set().union(
        *(_table_texts(ref, ctx, terms, seen) for ref, ctx in found.items() if ref in seen)
    )
    hidden = {t for t in texts if len(t) >= _MIN_TEXT and not any(t in v for v in visible)}
    _require(
        name in hidden,
        f"GateHarness: the name of {scenario} must be part of no name or text the caller "
        "may see, or nothing tells it apart",
    )
    return name, hidden


def _shape(value: Any) -> Any:
    """The keys and kinds of a reply, with its ``kind`` and ``context_sources`` as they are."""
    if isinstance(value, dict):
        return {
            key: item if key in ("kind", "context_sources") else _shape(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_shape(item) for item in value]
    return type(value).__name__


class GateContract:
    """The gate of the core proven end to end over a provider; subclass it and give a
    ``gate`` fixture that returns a :class:`GateHarness`.

    Each check builds the server as it runs, with the provider, for each identity mode
    the gate serves (``jwt``, ``impersonation`` with ``MCP_CONTEXT_REQUIRE_SELECT=true``),
    and a :class:`~akko_mcp_trino.testing.FakeTrino` in place of Trino, once answering a
    hidden object as missing (an access control that filters the metadata), once refusing
    it (``PERMISSION_DENIED``, rule-based). A caller sees every table the harness names but
    one object: the hidden table, then the hidden column, then the hidden join target.
    Another caller, who sees everything, proves that each of them is served when it is
    visible, so the checks test something. One more check gives a table Trino lists but
    refuses a read of. ``DEADLINE_SECONDS`` is the deadline of a catalogue read, raised in
    a subclass for a slow CI.
    """

    DEADLINE_SECONDS = 5.0

    def _catalogue(self, gate: GateHarness) -> tuple[Mapping, Mapping]:
        names = gate.contract.names
        wanted = _tables(gate)
        with _running(gate.contract) as run:
            found = run.describe(wanted, self.DEADLINE_SECONDS)
            terms = run.terms(_term_keys(found.values()), self.DEADLINE_SECONDS)
            # Without a join target, every table the contract names is asked too: a join
            # given for any of them means the provider has joins to check.
            others = (
                run.describe(_refs(names), self.DEADLINE_SECONDS)
                if gate.hidden_join_target is None
                else {}
            )
        _check_gate(gate, {**others, **found})
        return found, terms

    @contextmanager
    def _serving(
        self,
        gate: GateHarness,
        mode: str,
        monkeypatch: Any,
        rights: Mapping[str, Any],
        *,
        existing: Mapping[TableRef, tuple[str, ...]] | None = None,
        require_select: bool | None = None,
    ) -> Iterator[_Server]:
        """The server built as it runs in ``mode``, over the provider and a fake Trino.

        ``rights`` gives each user's ``Visible``; ``existing``, Trino's own tables, makes it
        a Trino that refuses a hidden object rather than one that answers it as missing.
        ``MCP_CONTEXT_REQUIRE_SELECT`` is ``require_select``, by default true in
        ``impersonation`` only."""
        from ..audit import InMemoryAudit
        from ..config import Config
        from ..context.loading import close_providers
        from ..metrics import Metrics
        from ..server import build_server
        from ..trino_client import JwtIdentity
        from .fake_trino import FakeTrino, Visible

        contract, names = gate.contract, gate.contract.names
        fake = FakeTrino(
            {
                user: seen if isinstance(seen, Visible) else Visible(seen)
                for user, seen in rights.items()
            },
            configured=(_CANARY_SCHEMA, _CANARY_TABLE),
            existing=existing,
        )
        fake.install(monkeypatch)
        # The identity provider is not there: the exchanged token names the caller's bearer.
        monkeypatch.setattr(JwtIdentity, "trino_token", lambda self, bearer: _trino_token(bearer))
        provider = contract.build(contract.env, _Transport(contract.replay, contract.secret))
        config = Config(
            trino_host="trino.invalid",
            trino_port=8443,
            trino_user="mcp-service",
            trino_catalog=names.catalog,
            max_rows=1000,
            read_only=True,
            auth_enabled=True,
            server_name="gate-contract",
            health_port=0,
            jwks_url="https://identity.invalid/certs",
            oidc_issuer="https://identity.invalid",
            oidc_audience="mcp",
            transport="streamable-http",
            mcp_port=0,
            auth_required=True,
            trino_http_scheme="https",
            trino_identity_mode=mode,
            trino_password="gate-contract-password" if mode == "impersonation" else "",
            token_exchange_url="https://identity.invalid/token",
            token_exchange_client_id="mcp",
            token_exchange_client_secret="gate-contract-exchange",
            token_exchange_audience="trino",
            context_providers=provider.name,
            context_canary=f"{names.catalog}.{_CANARY_SCHEMA}.{_CANARY_TABLE}",
            context_require_select=(
                mode == "impersonation" if require_select is None else require_select
            ),
        )
        captured: dict[str, Callable[..., str]] = {}

        class _Capture:
            def tool(self, annotations: Any = None, **_: Any) -> Callable[[Callable], Callable]:
                def keep(fn: Callable) -> Callable:
                    captured[fn.__name__] = fn
                    return fn

                return keep

        setups: list[Any] = []
        audit, metrics = InMemoryAudit(), Metrics()
        try:
            build_server(
                config,
                mcp_factory=lambda name, instructions=None: _Capture(),
                metrics=metrics,
                audit=audit,
                context_providers=[provider],
                context_started=setups.append,
            )
        except Exception:
            close_providers([provider])
            raise
        try:
            yield _Server(captured, fake, audit, metrics, provider.name)
        finally:
            for setup in setups:
                setup.close()
        _require(
            not fake.unanswered,
            "the fake Trino received a statement it does not answer: "
            f"{[s.sql for s in fake.unanswered][:3]}",
        )

    def _sees_all_is_served(
        self, gate: GateHarness, mode: str, monkeypatch: Any, everything: Mapping
    ) -> None:
        """A caller who sees everything is served each hidden object: the checks test something."""
        names = gate.contract.names
        with self._serving(gate, mode, monkeypatch, {_SEES_ALL: everything}) as server:
            described = server.call(_SEES_ALL, "describe_table", _table_args(names.described))
            hidden = server.call(_SEES_ALL, "describe_table", _table_args(gate.hidden_table))
        reply, other = described.reply, hidden.reply
        joins = reply.get("table", {}).get("joins", [])
        target = gate.hidden_join_target
        _require(
            gate.hidden_column in reply.get("column_context", {})
            and (target is None or any(join.get("table") == _dotted(target) for join in joins))
            and bool(other.get("table") or other.get("column_context")),
            f"a caller who sees everything ({mode}) is not served the hidden column, "
            + ("" if target is None else "the join to the hidden target, ")
            + "and the hidden table's context: the gate run cannot test anything",
        )

    def _each_run(
        self, gate: GateHarness, monkeypatch: Any, scenarios: tuple[str, ...] | None = None
    ) -> Iterator[tuple]:
        """For each mode, each way Trino hides and each hidden object (those of
        ``scenarios``, by default every one the harness can play): the scenario, where it
        runs, the caller's answers, the logs, the metrics, what the caller sees and the texts
        of the hidden object."""
        found, terms = self._catalogue(gate)
        everything = _trino_columns(found)
        for mode in _MODES:
            self._sees_all_is_served(gate, mode, monkeypatch, everything)
            for behaviour in _BEHAVIOURS:
                existing = everything if behaviour == _REFUSES else None
                for scenario in scenarios or _scenarios(gate):
                    seen = _rights(scenario, gate, everything)
                    name, hidden = _hidden_texts(scenario, gate, found, terms, seen)
                    with (
                        _captured_logs() as lines,
                        self._serving(
                            gate, mode, monkeypatch, {_CALLER: seen}, existing=existing
                        ) as server,
                    ):
                        answers = [server.call(_CALLER, tool, args) for tool, args in _calls(gate)]
                        metrics = server.metrics_text()
                        provider = server.provider
                    where = f"{scenario}, {mode}, {behaviour}"
                    yield (
                        mode,
                        scenario,
                        where,
                        answers,
                        list(lines),
                        metrics,
                        seen,
                        (name, hidden),
                        provider,
                    )

    def test_nothing_of_a_hidden_object_reaches_any_answer_status_or_audit_line(
        self, gate: GateHarness, monkeypatch: Any
    ) -> None:
        """No reply, ``context_sources`` status, audit line, log line or metric holds the
        name of a hidden table, column or join target, nor a text the catalogue gives it."""
        self._nothing_reaches_the_caller(self._each_run(gate, monkeypatch))

    def test_a_hidden_join_target_is_checked_when_the_catalogue_gives_joins(
        self, gate: GateHarness, monkeypatch: Any
    ) -> None:
        """The join scenario alone: nothing of a join target the caller does not see reaches
        them. A catalogue with no notion of a join (``hidden_join_target`` is ``None``) skips
        it, and the other checks fail if its provider gives a join all the same."""
        if gate.hidden_join_target is None:
            pytest.skip("the provider gives no join: its catalogue has no notion of one")
        self._nothing_reaches_the_caller(
            self._each_run(gate, monkeypatch, scenarios=(_HIDDEN_JOIN,))
        )

    def _nothing_reaches_the_caller(self, runs: Iterator[tuple]) -> None:
        for _, scenario, where, answers, lines, metrics, seen, hidden, provider in runs:
            _, texts = hidden
            visible = {_dotted(ref) for ref in seen}
            for answer in answers:
                reply = answer.reply
                sources = reply.get("context_sources", {})
                _require(
                    set(sources) <= {provider} and set(sources.values()) <= _STATUSES,
                    f"context_sources must name the provider and a status, nothing else ({where})",
                )
                _require(
                    set(answer.line["metadata_served"]) <= visible,
                    f"the audit line says context was served of a table the caller does not "
                    f"see ({where})",
                )
            channels = {
                "a reply": [answer.scrubbed(answer.text) for answer in answers],
                "the audit line": [answer.scrubbed(answer.line_text()) for answer in answers],
                "the logs": lines,
                "the metrics": [metrics],
            }
            for channel, received in channels.items():
                leaked = sorted(t for t in texts if any(t in text for text in received))
                _require(
                    not leaked,
                    f"{scenario} reaches the caller ({where}) through {channel}: {leaked}",
                )

    def test_no_statement_holds_a_name_trino_had_not_listed_to_that_caller(
        self, gate: GateHarness, monkeypatch: Any
    ) -> None:
        """Every statement runs as the caller and names only what Trino has listed to them;
        only the agent's own statements also name what the agent wrote, and the only
        statement that names a hidden object is the ``DESCRIBE`` the agent asked of it."""
        from ..trino_client import GATE_TAG
        from .fake_trino import names_in

        gated = 0
        for mode, scenario, where, answers, _, _, _, hidden, _ in self._each_run(gate, monkeypatch):
            name, _ = hidden
            token = _trino_token(_bearer(_CALLER)) if mode == "jwt" else ""
            for answer in answers:
                asked = answer.asked()
                written = {value.lower() for value in answer.args.values()}
                for statement in answer.statements:
                    gate_statement = GATE_TAG in statement.client_tags
                    gated += gate_statement
                    _require(
                        statement.user == _CALLER and statement.token == token,
                        f"a statement runs as another user than the caller, or with another "
                        f"token for Trino than the caller's ({where})",
                    )
                    allowed = set() if gate_statement else written
                    unlisted = [n for n in statement.unlisted if n not in allowed]
                    _require(
                        not unlisted,
                        f"a statement names what Trino had not listed to its caller ({where}): "
                        f"{statement.sql}",
                    )
                    if name in names_in(statement.sql):
                        _require(
                            scenario != _HIDDEN_COLUMN
                            and not gate_statement
                            and asked is not None
                            and asked.table == name
                            and statement.sql.lstrip().upper().startswith("DESCRIBE "),
                            f"a statement names what Trino had not listed to its caller "
                            f"({where}): {statement.sql}",
                        )
        _require(gated > 0, "the gate ran no statement of its own: nothing was checked")

    def test_a_hidden_and_a_missing_table_answer_the_same_shape(
        self, gate: GateHarness, monkeypatch: Any
    ) -> None:
        """``describe_table`` of a table Trino hides from the caller and of one that does
        not exist get the same from the core, though the catalogue describes only the first:
        the same keys, kinds and statuses besides Trino's own error, the same audit line
        besides its error code, and the same statements. Trino may answer the hidden one as
        missing or refuse it; both are run."""
        found, _ = self._catalogue(gate)
        everything = _trino_columns(found)
        seen = _rights(_HIDDEN_TABLE, gate, everything)

        def statements(answer: _Answer) -> list[tuple]:
            table = answer.args["table"]
            return [(s.client_tags, s.sql.replace(table, "<asked>")) for s in answer.statements]

        def added(answer: _Answer) -> Any:
            reply = {k: v for k, v in answer.reply.items() if k not in _TRINO_ERROR}
            line = {
                k: v
                for k, v in answer.line.items()
                if k not in ("request_id", "duration_ms", "error")
            }
            return _shape(reply), line["ok"], line["metadata_served"], _shape(line)

        for mode in _MODES:
            for behaviour in _BEHAVIOURS:
                existing = everything if behaviour == _REFUSES else None
                answers = []
                for ref in (gate.hidden_table, gate.contract.names.unknown):
                    with self._serving(
                        gate, mode, monkeypatch, {_CALLER: seen}, existing=existing
                    ) as server:
                        answers.append(server.call(_CALLER, "describe_table", _table_args(ref)))
                hidden, missing = answers
                _require(
                    added(hidden) == added(missing) and statements(hidden) == statements(missing),
                    "a hidden and a missing table must answer the same keys, kinds and "
                    "statuses, the same audit line and the same statements "
                    f"({mode}, {behaviour})",
                )

    def test_a_table_listed_but_not_readable_has_no_context_when_a_read_is_required(
        self, gate: GateHarness, monkeypatch: Any
    ) -> None:
        """With ``MCP_CONTEXT_REQUIRE_SELECT=true``, a table Trino lists to the caller but
        refuses them a read of gets its columns and no context: every source says
        ``not_checked`` and the audit line says nothing was served. Without the setting,
        the context follows what Trino lists, and is served."""
        from .fake_trino import Visible

        found, _ = self._catalogue(gate)
        everything = _trino_columns(found)
        listed = Visible(everything, unreadable=frozenset({gate.hidden_table}))
        description = found[gate.hidden_table].description
        for mode in _MODES:
            replies = {}
            for required in (True, False):
                with self._serving(
                    gate, mode, monkeypatch, {_CALLER: listed}, require_select=required
                ) as server:
                    answer = server.call(_CALLER, "describe_table", _table_args(gate.hidden_table))
                replies[required] = (answer.reply, answer.line)
            (refused, line), (followed, _) = replies[True], replies[False]
            _require(
                bool(followed.get("table") or followed.get("column_context")),
                f"without MCP_CONTEXT_REQUIRE_SELECT, the hidden table's context must follow "
                f"what Trino lists ({mode}): the check cannot test anything",
            )
            _require(
                bool(refused.get("rows"))
                and not (refused.get("table") or refused.get("column_context"))
                and not refused.get("terms")
                and set(refused.get("context_sources", {}).values()) == {NOT_CHECKED}
                and not line["metadata_served"]
                and (not description or description not in json.dumps(refused)),
                f"a table the caller may not read must get its columns and no context when a "
                f"read is required ({mode})",
            )
