"""``AtlasProvider``: the tables Trino lists, read from Atlas by batch.

A Trino table ``catalog.schema.table`` is the Atlas entity whose qualified name is
``schema.table@<cluster of the catalog>`` (``ATLAS_CLUSTERS``), looked up under each type
of ``ATLAS_TABLE_TYPES`` with ``GET /api/atlas/v2/entity/bulk/uniqueAttribute/type/{type}``,
the names split in requests whose query stays under ``ATLAS_MAX_QUERY_CHARS``. Only names
Trino listed are asked for, nothing is searched, and the cluster keeps the case
``ATLAS_CLUSTERS`` gives it, since Atlas matches a qualified name exactly. A type Atlas
says does not exist yields nothing, and is not asked again until ``reset()`` (the
server's SIGHUP) or a change of the secret's file.

An entity whose status is not ``ACTIVE`` is ignored, and so is a relation (a column, a
partition key, a glossary term) when the relation or its target is not ``ACTIVE``, or a
classification propagated from an entity that is not. The columns come from ``columns``
and ``partitionKeys``.

Every name returned is folded to lower case, as Trino lists it. When two Atlas columns
fold to the same Trino name, the one already written in lower case wins, else the first
in code point order, whatever the order of Atlas's answer; across entity types, the
first type of ``ATLAS_TABLE_TYPES`` that holds the table wins.

What is served beyond descriptions is what the allow lists name, by exact name:
``ATLAS_GLOSSARIES`` (by the glossary suffix of a term's qualified name),
``ATLAS_CLASSIFICATIONS`` and ``ATLAS_BUSINESS_METADATA``; empty, none is served.
``ATLAS_OWNER_ATTRIBUTE`` serves the owner from one business metadata attribute.

A glossary term is read by its GUID, ``GET /api/atlas/v2/glossary/term/{guid}``, once per
distinct key: the first read alone, so that a refused account costs one request and the
next reads reuse its session, then at most ``ATLAS_MAX_CONCURRENT_REQUESTS`` at a time,
all within the call's deadline. Its glossary is the suffix of its qualified name
(``<term>@<glossary>``); a term outside ``ATLAS_GLOSSARIES``, or that names no glossary,
is never served. The definition is ``shortDescription``, else ``longDescription``; the
status is ``additionalAttributes.<ATLAS_TERM_STATUS_ATTRIBUTE>``, never the status of an
assignment. Related terms (``synonyms``, ``preferredTerms``, ``preferredToTerms``,
``seeAlso``, ``isA``, ``replacedBy``) and categories come from the term's own answer, by
the name and qualified name it gives them, with no other request; a related term of a
glossary outside the allow list, or without a qualified name, is left out. Other
relations (``classifies``, ``replacementTerms``, ``antonyms``...) are not read.
"""

import asyncio
import itertools
import logging
import time
from collections.abc import Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from types import MappingProxyType
from typing import Any
from urllib.parse import quote

import httpx
from akko_mcp_trino.context import (
    CLASSIFICATION,
    CONTEXT_API,
    GLOSSARY,
    OWNER,
    PROPERTY,
    ColumnContext,
    Labelled,
    ProviderRefused,
    ProviderUnavailable,
    TableContext,
    TableRef,
    Term,
    TermRef,
)

from .client import INSTANCE_BY_ATTRIBUTE, INSTANCE_BY_GUID, UNKNOWN_TYPE, AtlasClient, Missing
from .settings import KERBEROS, KNOX, AtlasSettings

log = logging.getLogger(__name__)

BULK = "/api/atlas/v2/entity/bulk/uniqueAttribute/type/"
# A name no table carries, asked by --check under each table type: a type Atlas does not
# know answers that it does not exist.
PROBE_NAME = "check.probe@check"
TERM = "/api/atlas/v2/glossary/term/"
VERSION = "/api/atlas/admin/version"
# A bulk read says nothing of a table Atlas does not hold, or of a type it does not know.
_BULK_MISSING = MappingProxyType({**INSTANCE_BY_ATTRIBUTE, **UNKNOWN_TYPE})
# The relations of a term that are read, and the name the core gives each.
RELATIONS = (
    ("synonyms", "synonym"),
    ("preferredTerms", "preferred"),
    ("preferredToTerms", "preferred_to"),
    ("seeAlso", "see_also"),
    ("isA", "is_a"),
    ("replacedBy", "replaced_by"),
)
_ACTIVE = "ACTIVE"
_STATUSES = ("status", "relationshipStatus", "entityStatus")


def _active(node: Mapping[str, Any]) -> bool:
    """An entity, or a relation whose relation and target both still exist."""
    return all(node.get(key, _ACTIVE) == _ACTIVE for key in _STATUSES)


def _object(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, dict) else {}


def _items(value: Any) -> list[Mapping[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _description(attributes: Mapping[str, Any]) -> str:
    """The steward's description, else the technical one, else the metastore comment."""
    for key in ("userDescription", "description", "comment"):
        if text := _text(attributes.get(key)).strip():
            return text
    return ""


def _value(value: Any) -> str:
    """A business metadata value as text; a list joined, nothing for an empty value."""
    if isinstance(value, list):
        return ", ".join(str(v) for v in value if v not in (None, ""))
    if value is None or isinstance(value, dict):
        return ""
    return str(value)


def _glossary(qualified_name: Any) -> str:
    """The glossary a term's qualified name ends with, ``<term>@<glossary>``; empty
    without one."""
    name, at, glossary = _text(qualified_name).rpartition("@")
    return glossary if at and name else ""


def _first_of(group: BaseExceptionGroup) -> BaseException:
    """The error a call raises when parallel reads failed: a refusal first, then an outage,
    then whatever came first (the server says ``error`` for it)."""
    errors = group.exceptions
    for kind in (ProviderRefused, ProviderUnavailable):
        for error in errors:
            if isinstance(error, kind):
                return error
    return errors[0]


def _said_refused(refused: list[str]) -> None:
    """Log, once for a call, that Atlas refused the account some of the objects asked."""
    if refused:
        log.warning(
            "atlas refused some objects to the account; "
            "an account with no read grant serves nothing"
        )


def _wins(names: Iterable[str]) -> str:
    """Of names that fold to the same Trino name, the one written in lower case, else the
    first in code point order."""
    return min(names, key=lambda name: (name != name.lower(), name))


def _cost(index: int, name: str) -> int:
    """The characters one name adds to a bulk query string, its ``&`` included."""
    return len(str(httpx.QueryParams({f"attr_{index}:qualifiedName": name}))) + bool(index)


def batches(names: Sequence[str], limit: int) -> list[list[str]]:
    """The qualified names in groups whose bulk query string stays under ``limit``
    characters; a name longer than the limit goes alone in a group of its own."""
    groups: list[list[str]] = []
    current: list[str] = []
    size = 0
    for name in names:
        cost = _cost(len(current), name)
        if current and size + cost > limit:
            groups.append(current)
            current, size = [], 0
            cost = _cost(0, name)
        current.append(name)
        size += cost
    if current:
        groups.append(current)
    return groups


class AtlasProvider:
    """A ``ContextProvider`` backed by the REST API v2 of Apache Atlas (2.1 and later)."""

    name = "atlas"
    context_api = CONTEXT_API

    def __init__(
        self, settings: AtlasSettings, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        """Build the provider; ``transport`` replaces the network (tests, the contract)."""
        self.settings = settings
        self._transport = transport
        self._client = AtlasClient(settings, transport)
        # Entity types Atlas said do not exist, each with the reset generation and the
        # secret's stamp it was learnt under; a record from an older one is stale. Only the
        # event loop writes ``_absent``, only ``reset()`` writes ``_generation``: neither
        # write can undo the other, and the signal handler takes no lock.
        self._absent: Mapping[str, tuple[int, str]] = MappingProxyType({})
        self._resets = itertools.count(1)
        self._generation = 0

    # -- the contract

    def maps(self, catalog: str) -> bool:
        """Say whether ``ATLAS_CLUSTERS`` maps this Trino catalog."""
        return catalog.lower() in self.settings.clusters

    def served_sets(self) -> Mapping[str, frozenset[str]]:
        """Return the allow lists, and the owner's set when an owner is served."""
        s = self.settings
        owner = s.owner_attribute.partition(".")[0]
        return {
            GLOSSARY: s.glossaries,
            CLASSIFICATION: s.classifications,
            PROPERTY: s.business_metadata,
            OWNER: frozenset({owner}) if owner else frozenset(),
        }

    async def describe(
        self,
        refs: Sequence[TableRef],
        *,
        deadline: float,
        on_behalf_of: None = None,
    ) -> Mapping[TableRef, TableContext]:
        """Describe the mapped tables Atlas holds, their columns included, by batch."""
        wanted: dict[str, list[TableRef]] = {}
        for ref in refs:
            cluster = self.settings.clusters.get(ref.catalog.lower())
            if cluster is not None:
                # Trino's names, already lower case, and the cluster as Atlas writes it.
                wanted.setdefault(f"{ref.schema}.{ref.table}@{cluster}", []).append(ref)
        if not wanted:
            return {}
        with self._client.watch() as watch:
            try:
                async with asyncio.timeout(deadline - time.monotonic()):
                    found = await self._entities(list(wanted))
            except TimeoutError:
                self._client.leave(watch)
                raise ProviderUnavailable("the deadline passed before atlas answered") from None
        return {
            ref: self._table(entity, referred)
            for name, (entity, referred) in found.items()
            for ref in wanted[name]
        }

    async def terms(self, keys: Sequence[str], *, deadline: float) -> Mapping[str, Term]:
        """Read glossary terms by GUID, once each, and return those of allowed glossaries."""
        unique = [key for key in dict.fromkeys(keys) if key]
        if not unique:
            return {}
        refused: list[str] = []
        with self._client.watch() as watch:
            try:
                async with asyncio.timeout(deadline - time.monotonic()):
                    first = await self._term(unique[0], refused)
                    rest = await self._parallel(unique[1:], refused)
            except TimeoutError:
                self._client.leave(watch)
                raise ProviderUnavailable("the deadline passed before atlas answered") from None
        _said_refused(refused)
        return {term.key: term for term in (first, *rest) if term is not None}

    def check_lines(self) -> list[str]:
        """Return one line per setting, never a secret, a keytab's path or a proxy's address,
        then what Atlas answers: its version, and the table types it does not know."""
        s, sets = self.settings, self.served_sets()

        def names(values: Iterable[str]) -> str:
            return ", ".join(sorted(values)) or "none"

        if s.auth == KERBEROS:
            service = s.spn or "HTTP@<host of each address>"
            authentication = f"kerberos, principal {s.principal}, service {service}"
        elif s.auth == KNOX:
            authentication = "knox token"
        else:
            authentication = f"basic, user {s.user}"
        clusters = ", ".join(f"{c} -> {s.clusters[c]}" for c in sorted(s.clusters)) or "none"
        return [
            f"atlas: addresses: {', '.join(s.urls)}",
            f"atlas: authentication: {authentication}",
            f"atlas: catalogs: {clusters}",
            f"atlas: table types: {', '.join(s.table_types)}",
            f"atlas: timeouts: connect {s.connect_timeout_seconds:g} s, "
            f"read {s.timeout_seconds:g} s",
            f"atlas: bulk reads: {s.max_query_chars} characters of query at most",
            f"atlas: term reads: {s.max_concurrent_requests} at once at most",
            f"atlas: glossaries: {names(sets[GLOSSARY])}",
            f"atlas: classifications: {names(sets[CLASSIFICATION])}",
            f"atlas: business metadata: {names(sets[PROPERTY])}",
            f"atlas: owner: {s.owner_attribute or 'not served'}",
            f"atlas: term status attribute: {s.term_status_attribute or 'none'}",
            "atlas: CA bundle: " + ("set" if s.verify is not True else "the system's"),
            "atlas: proxy: " + ("set" if s.proxy else "none"),
            *self._probe(),
        ]

    def _probe(self) -> list[str]:
        """Ask Atlas its version, then each table type for a name no table carries, on a
        client and an event loop of the probe's own, so that it runs from any thread; the
        lines say what Atlas answered, never in its own words."""

        async def ask() -> tuple[Any, list[str]]:
            client = AtlasClient(self.settings, self._transport)
            try:
                version = await client.get(VERSION)
                absent = []
                for type_name in self.settings.table_types:
                    answer = await client.get(
                        BULK + quote(type_name, safe=""),
                        {"attr_0:qualifiedName": PROBE_NAME},
                        missing=_BULK_MISSING,
                    )
                    if answer is Missing.TYPE:
                        absent.append(type_name)
                return version, absent
            finally:
                await client.aclose()

        with ThreadPoolExecutor(max_workers=1) as pool:
            try:
                answer, absent = pool.submit(asyncio.run, ask()).result()
            except ProviderRefused:
                return ["UNREACHABLE: atlas refused the account"]
            except ProviderUnavailable:
                return ["UNREACHABLE: atlas did not answer"]
            except Exception as exc:  # noqa: BLE001 - the check says it failed, by type
                return [f"UNREACHABLE: the atlas check failed ({type(exc).__name__})"]
        version = _text(_object(answer).get("Version")).strip()
        lines = [f"atlas: answers, version {version or 'not given'}"]
        if len(absent) == len(self.settings.table_types):
            lines.append(f"atlas: knows none of the table types ({', '.join(absent)})")
        elif absent:
            lines.append(f"atlas: does not know the table types {', '.join(absent)}")
        return lines

    async def aclose(self) -> None:
        """Close the HTTP client."""
        await self._client.aclose()

    def credentials_stamp(self) -> str:
        """Return a value that changes when the secret's file changes; empty without one."""
        return self.settings.credentials_stamp()

    def reset(self) -> None:
        """Forget the entity types Atlas said do not exist; the server calls it on SIGHUP,
        from its signal handler. It only stores a new generation, so it never waits, and
        a type found absent by a read already under way is forgotten too."""
        self._generation = next(self._resets)

    # -- reading

    async def _parallel(self, keys: list[str], refused: list[str]) -> list[Term | None]:
        """Read terms, at most ``ATLAS_MAX_CONCURRENT_REQUESTS`` at a time; the first
        failure cancels the others and is the call's error."""
        slots = asyncio.Semaphore(self.settings.max_concurrent_requests)

        async def one(key: str) -> Term | None:
            async with slots:
                return await self._term(key, refused)

        try:
            async with asyncio.TaskGroup() as group:
                tasks = [group.create_task(one(key)) for key in keys]
        except BaseExceptionGroup as raised:
            raise _first_of(raised) from None
        return [task.result() for task in tasks]

    async def _term(self, key: str, refused: list[str]) -> Term | None:
        """One term, or ``None`` when Atlas does not hold it, refuses it to the account (noted
        in ``refused``) or it may not be served."""
        doc = await self._client.get(TERM + quote(key, safe=""), missing=INSTANCE_BY_GUID)
        if doc is Missing.FORBIDDEN:
            refused.append(key)
        if isinstance(doc, Missing):
            return None
        body = _object(doc)
        glossary = _glossary(body.get("qualifiedName"))
        name = _text(body.get("name")).strip()
        if not name or glossary not in self.settings.glossaries:
            return None
        attribute = self.settings.term_status_attribute
        status = (
            _value(_object(body.get("additionalAttributes")).get(attribute)) if attribute else ""
        )
        definition = (
            _text(body.get("shortDescription")).strip()
            or _text(body.get("longDescription")).strip()
        )
        categories = (_text(c.get("displayText")) for c in _items(body.get("categories")))
        return Term(
            key=key,
            name=name,
            glossary=glossary,
            definition=definition,
            status=status,
            categories=tuple(c for c in dict.fromkeys(categories) if c),
            relations=self._relations(body),
        )

    def _relations(self, body: Mapping[str, Any]) -> tuple[TermRef, ...]:
        """The six relations read, each to a term of an allowed glossary, once each."""
        refs: dict[tuple[str, str], TermRef] = {}
        for field, relation in RELATIONS:
            for header in _items(body.get(field)):
                glossary = _glossary(header.get("qualifiedName"))
                key, name = _text(header.get("termGuid")), _text(header.get("displayText"))
                if key and name and glossary in self.settings.glossaries:
                    refs.setdefault((key, relation), TermRef(key, name, glossary, relation))
        return tuple(refs.values())

    async def _entities(
        self, names: list[str]
    ) -> dict[str, tuple[Mapping[str, Any], Mapping[str, Any]]]:
        """Each table found, by the qualified name asked, with the entities it refers to."""
        found: dict[str, tuple[Mapping[str, Any], Mapping[str, Any]]] = {}
        learnt = (self._generation, self.settings.credentials_stamp())
        types = [t for t in self.settings.table_types if self._absent.get(t) != learnt]
        refused: list[str] = []
        for type_name in types:
            left = [name for name in names if name not in found]
            for group in batches(left, self.settings.max_query_chars):
                doc = await self._client.get(
                    BULK + quote(type_name, safe=""),
                    {f"attr_{i}:qualifiedName": name for i, name in enumerate(group)},
                    missing=_BULK_MISSING,
                )
                if doc is Missing.TYPE:
                    self._absent = MappingProxyType({**self._absent, type_name: learnt})
                    if all(self._absent.get(t) == learnt for t in self.settings.table_types):
                        log.warning("atlas knows none of the table types")
                    break
                if doc is Missing.FORBIDDEN:
                    # One table the account may not read hides its whole batch.
                    refused += group
                    continue
                body = _object(doc)
                referred = _object(body.get("referredEntities"))
                for entity in _items(body.get("entities")):
                    qualified = _text(_object(entity.get("attributes")).get("qualifiedName"))
                    if qualified in group and _active(entity):
                        found.setdefault(qualified, (entity, referred))
        _said_refused(refused)
        return found

    def _table(self, entity: Mapping[str, Any], referred: Mapping[str, Any]) -> TableContext:
        return TableContext(
            description=_description(_object(entity.get("attributes"))),
            owner=self._owner(entity),
            tags=self._classifications(entity),
            terms=self._terms(entity),
            properties=self._properties(entity),
            columns=self._columns(entity, referred),
        )

    def _columns(
        self, entity: Mapping[str, Any], referred: Mapping[str, Any]
    ) -> Mapping[str, ColumnContext]:
        """The live columns and partition keys, by lower-case name."""
        relations = _object(entity.get("relationshipAttributes"))
        by_name: dict[str, dict[str, Mapping[str, Any]]] = {}
        for key in ("columns", "partitionKeys"):
            for header in _items(relations.get(key)):
                column = _object(referred.get(_text(header.get("guid"))))
                name = _text(_object(column.get("attributes")).get("name"))
                if name and _active(header) and _active(column):
                    by_name.setdefault(name.lower(), {}).setdefault(name, column)
        return MappingProxyType(
            {
                folded: self._column(spellings[_wins(spellings)])
                for folded, spellings in by_name.items()
            }
        )

    def _column(self, column: Mapping[str, Any]) -> ColumnContext:
        return ColumnContext(
            description=_description(_object(column.get("attributes"))),
            classifications=self._classifications(column),
            terms=self._terms(column),
            properties=self._properties(column),
        )

    def _classifications(self, entity: Mapping[str, Any]) -> tuple[Labelled, ...]:
        """The allowed classifications, direct or propagated from a live entity, once each."""
        allowed = self.settings.classifications
        names = (
            _text(item.get("typeName"))
            for item in _items(entity.get("classifications"))
            if _active(item)
        )
        return tuple(Labelled(name, name) for name in dict.fromkeys(names) if name in allowed)

    def _terms(self, entity: Mapping[str, Any]) -> tuple[str, ...]:
        """The keys (GUIDs) of the live terms of allowed glossaries attached to the entity."""
        allowed = self.settings.glossaries
        meanings = _items(_object(entity.get("relationshipAttributes")).get("meanings"))
        keys = (
            _text(m.get("guid"))
            for m in meanings
            if _active(m) and _glossary(m.get("qualifiedName")) in allowed
        )
        return tuple(key for key in dict.fromkeys(keys) if key)

    def _properties(self, entity: Mapping[str, Any]) -> Mapping[str, Labelled]:
        """Business metadata of the allowed sets, as ``<set>.<attribute>``."""
        out: dict[str, Labelled] = {}
        for business, values in sorted(_object(entity.get("businessAttributes")).items()):
            if business not in self.settings.business_metadata:
                continue
            for attribute, value in sorted(_object(values).items()):
                if text := _value(value):
                    out[f"{business}.{attribute}"] = Labelled(text, business)
        return MappingProxyType(out)

    def _owner(self, entity: Mapping[str, Any]) -> str:
        business, _, attribute = self.settings.owner_attribute.partition(".")
        if not business:
            return ""
        values = _object(_object(entity.get("businessAttributes")).get(business))
        return _value(values.get(attribute))
