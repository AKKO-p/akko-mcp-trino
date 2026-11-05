"""The contract every context provider must pass, published for adapter authors.

An adapter's tests subclass :class:`ContextContract` and give it a ``harness`` pytest
fixture that returns a :class:`ContractHarness`: how to build the provider from its
settings and an HTTP transport, the catalogue's recorded answers, the settings with a
secret the contract can look for, and which recorded objects play which role
(:class:`ContractNames`). The contract needs no network, no catalogue and no Trino:
every request the provider sends goes to a transport that answers from the recordings,
or, on purpose, slowly, with a 401, a 503, a body that is not JSON, or a 500 whose body
holds the secret. The transport counts what is sent and what is still in flight.

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
import json
import logging
import subprocess
import sys
import time
import traceback
from collections.abc import Callable, Coroutine, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, fields
from typing import Any
from urllib.parse import quote, quote_plus

import httpx
import pytest

from ..context.errors import ProviderError, ProviderRefused, ProviderUnavailable
from ..context.loop import ContextLoop
from ..context.provider import ContextProvider
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
    time the contract's slow catalogue takes to answer.
    """

    REPLY_MARGIN_SECONDS = 0.5
    DEADLINE_SECONDS = 5.0
    SLOW_DEADLINE_SECONDS = 1.0

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
        """A factory that refuses a malformed setting (``ValueError``) does not put the secret
        in its message, the causes included: ``--check`` prints that message. Each setting
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
                except ValueError as error:
                    refusals.append("".join(traceback.format_exception(error)))
        _require(
            not any(_holds_secret(text, secret) for text in refusals),
            "a factory's ValueError must never hold the secret, as written or encoded",
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

    def test_snapshot_pages_without_duplicate_or_loss(self, harness: ContractHarness) -> None:
        """``snapshot`` is checked from 0.8.x, with pre-warming; no provider offers it in 0.8."""
        pytest.skip("snapshot arrives with pre-warming in 0.8.x; nothing to check in 0.8")
