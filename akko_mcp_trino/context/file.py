"""The ``file`` context provider: a catalogue kept in one JSON file.

It is the reference for the provider contract and needs no service. The file describes
tables, columns and glossary terms by name, never data: a ``values`` or ``samples`` key
anywhere refuses it. The provider folds the catalog, schema, table and column names to
lower case, as Trino lists them, because the core compares them exactly (term keys are kept as
written); a file that holds the same table or the same column twice once folded is refused,
not merged.

What is served is what the file declares and the operator allows by exact name
(``MCP_CONTEXT_FILE_GLOSSARIES``, ``_CLASSIFICATIONS``, ``_PROPERTIES``; ``*`` is a name,
never a wildcard). The file is read again, off the event loop, when it changes; a changed
file that is no longer valid keeps the last good content and logs one warning.
"""

import asyncio
import json
import logging
import os
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any

from . import CONTEXT_API
from .errors import ProviderUnavailable
from .types import (
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

log = logging.getLogger(__name__)

ENV_FILE = "MCP_CONTEXT_FILE"
ENV_GLOSSARIES = "MCP_CONTEXT_FILE_GLOSSARIES"
ENV_CLASSIFICATIONS = "MCP_CONTEXT_FILE_CLASSIFICATIONS"
ENV_PROPERTIES = "MCP_CONTEXT_FILE_PROPERTIES"
ENV_SERVE_OWNER = "MCP_CONTEXT_FILE_SERVE_OWNER"

_DATA_KEYS = frozenset({"values", "samples"})
_OWNER_SET = "owner"


class _Invalid(ValueError):
    """The file is not in the format; the reason is a fixed phrase, never the file's text."""


@dataclass(frozen=True)
class _Content:
    """What one reading of the file holds, with the sets it declares."""

    catalogs: frozenset[str]
    tables: Mapping[TableRef, TableContext]
    terms: Mapping[str, Term]
    glossaries: frozenset[str]
    classifications: frozenset[str]
    properties: frozenset[str]
    has_owner: bool


@dataclass(frozen=True)
class _Allowed:
    """The sets the operator allows, by exact name."""

    glossaries: frozenset[str]
    classifications: frozenset[str]
    properties: frozenset[str]
    owner: bool


# ---- reading the format


def _has_data_key(node: Any) -> bool:
    """Tell whether any key of the document, at any depth, is a data field."""
    return _fields(node, {})


def _fields(node: Any, special: Mapping[str, Callable[[Any], bool]]) -> bool:
    """Check the keys of an object as field names, and its values as ``special`` says."""
    if isinstance(node, dict):
        return any(
            key.lower() in _DATA_KEYS or special.get(key, _has_data_key)(value)
            for key, value in node.items()
        )
    if isinstance(node, list):
        return any(_has_data_key(item) for item in node)
    return False


def _keys_are_names(each: Callable[[Any], bool]) -> Callable[[Any], bool]:
    """Check an object whose keys are names (a column, a term, a property), not fields."""

    def check(node: Any) -> bool:
        if isinstance(node, dict):
            return any(each(value) for value in node.values())
        return _has_data_key(node)

    return check


def _column_has_data(node: Any) -> bool:
    return _fields(node, {"properties": _keys_are_names(_has_data_key)})


def _table_has_data(node: Any) -> bool:
    special = {
        "columns": _keys_are_names(_column_has_data),
        "properties": _keys_are_names(_has_data_key),
    }
    return _fields(node, special)


def _document_has_data(node: Any) -> bool:
    """Tell whether the file carries data: a ``values`` or ``samples`` field, never a name."""
    tables = (
        (lambda value: any(map(_table_has_data, value)))
        if isinstance(node, dict) and isinstance(node.get("tables"), list)
        else _has_data_key
    )
    return _fields(node, {"tables": tables, "terms": _keys_are_names(_has_data_key)})


def _object(node: Any) -> dict[str, Any]:
    if not isinstance(node, dict):
        raise _Invalid("an object was expected")
    return node


def _list(node: Any) -> list[Any]:
    if not isinstance(node, list):
        raise _Invalid("a list was expected")
    return node


def _text(node: dict[str, Any], key: str, *, required: bool = False) -> str:
    value = node.get(key)
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        raise _Invalid(f"{key} must be a text")
    return value


def _texts(node: dict[str, Any], key: str) -> tuple[str, ...]:
    values = _list(node.get(key, []))
    if not all(isinstance(value, str) for value in values):
        raise _Invalid(f"{key} must be a list of texts")
    return tuple(values)


def _columns_of(node: dict[str, Any], key: str) -> tuple[str, ...]:
    return tuple(name.lower() for name in _texts(node, key))


def _labels(node: dict[str, Any], key: str) -> tuple[Labelled, ...]:
    return tuple(
        Labelled(_text(item, "name", required=True), _text(item, "set", required=True))
        for item in map(_object, _list(node.get(key, [])))
    )


def _properties(node: dict[str, Any]) -> Mapping[str, Labelled]:
    specs = _object(node.get("properties", {}))
    return MappingProxyType(
        {
            key: Labelled(_text(spec, "value", required=True), _text(spec, "set", required=True))
            for key, spec in ((key, _object(value)) for key, value in specs.items())
        }
    )


def _join(node: Any) -> Join:
    spec = _object(node)
    columns, target_columns = _columns_of(spec, "columns"), _columns_of(spec, "target_columns")
    if not columns or len(columns) != len(target_columns):
        raise _Invalid("a join needs as many target columns as columns, and at least one")
    target = _object(spec.get("target"))
    ref = TableRef.of(
        _text(target, "catalog", required=True),
        _text(target, "schema", required=True),
        _text(target, "table", required=True),
    )
    return Join(columns, ref, target_columns)


def _column(node: Any) -> ColumnContext:
    spec = _object(node)
    return ColumnContext(
        description=_text(spec, "description"),
        classifications=_labels(spec, "classifications"),
        terms=_texts(spec, "terms"),
        properties=_properties(spec),
    )


def _table(spec: dict[str, Any]) -> tuple[TableRef, TableContext]:
    ref = TableRef.of(
        _text(spec, "catalog", required=True),
        _text(spec, "schema", required=True),
        _text(spec, "table", required=True),
    )
    columns: dict[str, ColumnContext] = {}
    for name, column in _object(spec.get("columns", {})).items():
        if name.lower() in columns:
            raise _Invalid("the same column twice once folded to lower case")
        columns[name.lower()] = _column(column)
    return ref, TableContext(
        description=_text(spec, "description"),
        owner=_text(spec, "owner"),
        tier=_text(spec, "tier"),
        tags=_labels(spec, "tags"),
        terms=_texts(spec, "terms"),
        properties=_properties(spec),
        grain=_columns_of(spec, "grain"),
        joins=tuple(_join(item) for item in _list(spec.get("joins", []))),
        columns=MappingProxyType(columns),
    )


def _term(key: str, node: Any) -> Term:
    spec = _object(node)
    relations = tuple(
        TermRef(
            _text(item, "key", required=True),
            _text(item, "name"),
            _text(item, "glossary", required=True),
            _text(item, "relation"),
        )
        for item in map(_object, _list(spec.get("relations", [])))
    )
    return Term(
        key=key,
        name=_text(spec, "name", required=True),
        glossary=_text(spec, "glossary", required=True),
        definition=_text(spec, "definition"),
        status=_text(spec, "status"),
        categories=_texts(spec, "categories"),
        relations=relations,
    )


def _build(document: Any) -> _Content:
    root = _object(document)
    version = root.get("version")
    if type(version) is not int or version != 1:
        raise _Invalid("version must be 1")
    catalogs = _texts(root, "catalogs")
    tables: dict[TableRef, TableContext] = {}
    for spec in map(_object, _list(root.get("tables", []))):
        ref, context = _table(spec)
        if ref in tables:
            raise _Invalid("the same table twice once folded to lower case")
        tables[ref] = context
    terms = {key: _term(key, node) for key, node in _object(root.get("terms", {})).items()}
    contexts = list(tables.values())
    columns = [column for context in contexts for column in context.columns.values()]
    return _Content(
        catalogs=frozenset(name.lower() for name in catalogs),
        tables=MappingProxyType(tables),
        terms=MappingProxyType(terms),
        glossaries=frozenset(term.glossary for term in terms.values()),
        classifications=frozenset(
            label.set
            for labels in [c.tags for c in contexts] + [c.classifications for c in columns]
            for label in labels
        ),
        properties=frozenset(
            label.set
            for maps in [c.properties for c in contexts] + [c.properties for c in columns]
            for label in maps.values()
        ),
        has_owner=any(context.owner for context in contexts),
    )


def _parse(path: str, text: str) -> _Content:
    """Read the file's text; the error names the setting and the path, and a fixed reason."""
    try:
        document = json.loads(text)
        if _document_has_data(document):
            raise _Invalid("a values or samples key carries data, which a context never holds")
        return _build(document)
    except (json.JSONDecodeError, RecursionError):
        reason = "it is not JSON"
    except _Invalid as exc:
        reason = str(exc)
    raise ValueError(f"{ENV_FILE}: {path!r} is not a valid context file ({reason})") from None


def _read(path: str) -> _Content:
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"{ENV_FILE}: cannot read {path!r} ({type(exc).__name__})") from None
    return _parse(path, text)


def _identity(path: str) -> tuple[int, int, int, int] | None:
    """What changes when the file is replaced, rewritten or resized; None when it is gone."""
    try:
        status = os.stat(path)
    except OSError:
        return None
    return (status.st_dev, status.st_ino, status.st_mtime_ns, status.st_size)


# ---- serving


def _serve_properties(
    properties: Mapping[str, Labelled], sets: frozenset[str]
) -> Mapping[str, Labelled]:
    return MappingProxyType({k: v for k, v in properties.items() if v.set in sets})


def _serve_table(
    context: TableContext, allowed: _Allowed, term_keys: frozenset[str]
) -> TableContext:
    return replace(
        context,
        owner=context.owner if allowed.owner else "",
        tags=tuple(tag for tag in context.tags if tag.set in allowed.classifications),
        terms=tuple(key for key in context.terms if key in term_keys),
        properties=_serve_properties(context.properties, allowed.properties),
        columns=MappingProxyType(
            {
                name: replace(
                    column,
                    classifications=tuple(
                        c for c in column.classifications if c.set in allowed.classifications
                    ),
                    terms=tuple(key for key in column.terms if key in term_keys),
                    properties=_serve_properties(column.properties, allowed.properties),
                )
                for name, column in context.columns.items()
            }
        ),
    )


def _names(env: Mapping[str, str], name: str) -> frozenset[str]:
    return frozenset(part.strip() for part in env.get(name, "").split(",") if part.strip())


class FileProvider:
    """Context from one JSON file, read again when it changes."""

    name = "file"
    context_api = CONTEXT_API

    def __init__(self, path: str, allowed: _Allowed) -> None:
        """Read the file now; raises ``ValueError`` when it cannot be served."""
        self._path = path
        self._allowed = allowed
        self._lock = threading.Lock()
        self._seen = _identity(path)
        self._content = _read(path)

    # -- the contract

    def maps(self, catalog: str) -> bool:
        """Say whether the file lists this Trino catalog.

        It looks at the file's stamp, never at its text: once the file has changed since
        the last read, any catalog may be new, so the answer is yes and the next read
        (``describe``, off the event loop) settles it.
        """
        return catalog.lower() in self._content.catalogs or _identity(self._path) != self._seen

    def served_sets(self) -> Mapping[str, frozenset[str]]:
        """Return what the file declares and the operator allows, as last read."""
        content, allowed = self._content, self._allowed
        return {
            GLOSSARY: content.glossaries & allowed.glossaries,
            CLASSIFICATION: content.classifications & allowed.classifications,
            PROPERTY: content.properties & allowed.properties,
            OWNER: frozenset({_OWNER_SET}) if allowed.owner and content.has_owner else frozenset(),
        }

    async def describe(
        self,
        refs: Sequence[TableRef],
        *,
        deadline: float,
        on_behalf_of: None = None,
    ) -> Mapping[TableRef, TableContext]:
        """Describe the tables the file holds, with only what is served."""
        content = await self._current(deadline)
        keys = self._served_term_keys(content)
        return {
            ref: _serve_table(content.tables[ref], self._allowed, keys)
            for ref in refs
            if ref in content.tables
        }

    async def terms(self, keys: Sequence[str], *, deadline: float) -> Mapping[str, Term]:
        """Read the terms of the served glossaries, once each."""
        content = await self._current(deadline)
        served = self._allowed.glossaries
        return {
            key: replace(term, relations=tuple(r for r in term.relations if r.glossary in served))
            for key in dict.fromkeys(keys)
            if (term := content.terms.get(key)) is not None and term.glossary in served
        }

    def check_lines(self) -> list[str]:
        """Return the lines of the configuration check: the file and the sets, never its text."""
        content, sets = self._content, self.served_sets()

        def names(kind: str) -> str:
            return ", ".join(sorted(sets[kind])) or "none"

        return [
            f"file: {self._path} ({len(content.tables)} tables, {len(content.terms)} terms)",
            f"file: glossaries: {names(GLOSSARY)}; classifications: {names(CLASSIFICATION)}; "
            f"properties: {names(PROPERTY)}; owner: " + ("served" if sets[OWNER] else "not served"),
        ]

    async def aclose(self) -> None:
        """Nothing is held open."""

    def credentials_stamp(self) -> str:
        """No secret comes from a file here."""
        return ""

    # -- reading again

    def _served_term_keys(self, content: _Content) -> frozenset[str]:
        return frozenset(
            key for key, term in content.terms.items() if term.glossary in self._allowed.glossaries
        )

    async def _current(self, deadline: float) -> _Content:
        if time.monotonic() >= deadline:
            raise ProviderUnavailable("the deadline has passed")
        return await asyncio.to_thread(self._refresh)

    def _refresh(self) -> _Content:
        with self._lock:
            seen = _identity(self._path)
            if seen == self._seen:
                return self._content
            self._seen = seen
            try:
                self._content = _read(self._path)
            except ValueError:
                log.warning(
                    "%s: the file %r cannot be read or is not valid after a change; "
                    "the last good content is still served",
                    ENV_FILE,
                    self._path,
                )
            return self._content


def from_env(env: Mapping[str, str]) -> FileProvider:
    """Build the provider from its settings; raises ``ValueError`` when it cannot serve them."""
    path = env.get(ENV_FILE, "")
    if not path:
        raise ValueError(f"{ENV_FILE} is not set")
    owner = env.get(ENV_SERVE_OWNER, "false").strip().lower()
    if owner not in ("true", "false"):
        raise ValueError(f"{ENV_SERVE_OWNER} must be true or false")
    allowed = _Allowed(
        glossaries=_names(env, ENV_GLOSSARIES),
        classifications=_names(env, ENV_CLASSIFICATIONS),
        properties=_names(env, ENV_PROPERTIES),
        owner=owner == "true",
    )
    return FileProvider(path, allowed)
