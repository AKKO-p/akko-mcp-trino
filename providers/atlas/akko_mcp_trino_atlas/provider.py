"""``AtlasProvider``: the tables Trino lists, read from Atlas by batch.

A Trino table ``catalog.schema.table`` is the Atlas entity whose qualified name is
``schema.table@<cluster of the catalog>`` (``ATLAS_CLUSTERS``), looked up under each type
of ``ATLAS_TABLE_TYPES`` with ``GET /api/atlas/v2/entity/bulk/uniqueAttribute/type/{type}``,
the names split in requests whose query stays under ``ATLAS_MAX_QUERY_CHARS``. A type
Atlas does not know yields nothing. Only names Trino listed are asked for; nothing is
searched.

An entity whose status is not ``ACTIVE`` is ignored, and so is a relation (a column, a
partition key, a glossary term) when the relation or its target is not ``ACTIVE``, or a
classification propagated from an entity that is not. The columns come from ``columns``
and ``partitionKeys``.

Every name returned is folded to lower case, as Trino lists it. When two Atlas names
fold to the same Trino name, the one already written in lower case wins, else the first
in code point order; across entity types, the first type of ``ATLAS_TABLE_TYPES`` wins.
The choice does not depend on the order of Atlas's answer.

What is served beyond descriptions is what the allow lists name, by exact name:
``ATLAS_GLOSSARIES`` (by the glossary suffix of a term's qualified name),
``ATLAS_CLASSIFICATIONS`` and ``ATLAS_BUSINESS_METADATA``; empty, none is served.
``ATLAS_OWNER_ATTRIBUTE`` serves the owner from one business metadata attribute.
"""

import asyncio
import time
from collections.abc import Iterable, Mapping, Sequence
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
    ProviderUnavailable,
    TableContext,
    TableRef,
    Term,
)

from .client import AtlasClient
from .settings import AtlasSettings

BULK = "/api/atlas/v2/entity/bulk/uniqueAttribute/type/"
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
        self._client = AtlasClient(settings, transport)

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
                name = f"{ref.schema}.{ref.table}@{cluster}"
                wanted.setdefault(name.lower(), []).append(ref)
        if not wanted:
            return {}
        try:
            async with asyncio.timeout(deadline - time.monotonic()):
                found = await self._entities(list(wanted))
        except TimeoutError:
            raise ProviderUnavailable("the deadline passed before atlas answered") from None
        return {
            ref: self._table(entity, referred)
            for name, (entity, referred) in found.items()
            for ref in wanted[name]
        }

    async def terms(self, keys: Sequence[str], *, deadline: float) -> Mapping[str, Term]:
        """Glossary terms are not read yet: no term is returned."""
        return {}

    def check_lines(self) -> list[str]:
        """Return the addresses, the authentication and what is served, never a secret."""
        s, sets = self.settings, self.served_sets()

        def names(values: Iterable[str]) -> str:
            return ", ".join(sorted(values)) or "none"

        return [
            f"atlas: {', '.join(s.urls)} ({s.auth}); catalogs: {names(s.clusters)}",
            f"atlas: glossaries: {names(sets[GLOSSARY])}; classifications: "
            f"{names(sets[CLASSIFICATION])}; business metadata: {names(sets[PROPERTY])}; "
            "owner: " + ("served" if sets[OWNER] else "not served"),
        ]

    async def aclose(self) -> None:
        """Close the HTTP client."""
        await self._client.aclose()

    def credentials_stamp(self) -> str:
        """Return a value that changes when the secret's file changes; empty without one."""
        return self.settings.credentials_stamp()

    # -- reading

    async def _entities(
        self, names: list[str]
    ) -> dict[str, tuple[Mapping[str, Any], Mapping[str, Any]]]:
        """Each table found, by folded qualified name, with the entities it refers to."""
        found: dict[str, tuple[Mapping[str, Any], Mapping[str, Any]]] = {}
        for type_name in self.settings.table_types:
            left = [name for name in names if name not in found]
            this_type: dict[str, dict[str, tuple[Mapping[str, Any], Mapping[str, Any]]]] = {}
            for group in batches(left, self.settings.max_query_chars):
                doc = await self._client.get(
                    BULK + quote(type_name, safe=""),
                    {f"attr_{i}:qualifiedName": name for i, name in enumerate(group)},
                )
                body = _object(doc)
                referred = _object(body.get("referredEntities"))
                asked = set(group)
                for entity in _items(body.get("entities")):
                    qualified = _text(_object(entity.get("attributes")).get("qualifiedName"))
                    if qualified.lower() in asked and _active(entity):
                        this_type.setdefault(qualified.lower(), {})[qualified] = (
                            entity,
                            referred,
                        )
            for name, spellings in this_type.items():
                found[name] = spellings[_wins(spellings)]
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
            if _active(m) and _text(m.get("qualifiedName")).rpartition("@")[2] in allowed
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
