"""Context providers: what Trino cannot say about a table, from wherever it is kept.

``DESCRIBE`` gives columns and types. It does not say what ``segment`` means,
who owns the table, whether it is trustworthy, which column joins it to
another, or that ``email`` is personal data. A model without that guesses,
and guesses wrong.

The server knows no catalogue. It knows the ``ContextProvider`` interface and
a chain of providers fills it:

* ``trino-comments`` — the comments Trino already carries (``COMMENT ON``),
  read under the caller's identity; zero installation;
* ``file`` — a versioned JSON document kept with the code (grain, joins,
  business definitions, classifications);
* a product's catalogue (OpenMetadata, DataHub…) — a provider shipped as a
  separate package that implements the same interface. This package imports
  none of them.

A provider **describes**; it never decides access. Knowing a column is PII
changes nothing about the mask, which the engine applies. It only changes the
SQL the model writes and how often it picks the wrong table.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence

from .sql_guard import safe_sql_string

FILE_FORMAT_VERSION = 1


@dataclass(frozen=True)
class Join:
    """How a table joins another: these columns to those columns on the target."""

    columns: list[str]
    target: str
    target_columns: list[str]


@dataclass(frozen=True)
class TableContext:
    """What is known about a table beyond its columns. Every field is optional."""

    description: str = ""
    owner: str = ""
    tier: str = ""
    grain: list[str] = field(default_factory=list)
    joins: list[Join] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        """Only the fields that carry something, as plain JSON-ready values."""
        return {k: v for k, v in asdict(self).items() if v}


@dataclass(frozen=True)
class ColumnContext:
    """What is known about a column beyond its type. Every field is optional."""

    description: str = ""
    classification: list[str] = field(default_factory=list)
    values: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        """Only the fields that carry something."""
        return {k: v for k, v in asdict(self).items() if v}


class ContextProvider(Protocol):
    """The contract a catalogue fulfils. Return None when nothing is known."""

    def table(self, catalog: str, schema: str, table: str) -> Optional[TableContext]:
        """What is known about the table, or None."""
        ...

    def column(self, catalog: str, schema: str, table: str, column: str) -> Optional[ColumnContext]:
        """What is known about the column, or None."""
        ...


class NoContext:
    """Knows nothing; the default."""

    def table(self, catalog: str, schema: str, table: str) -> Optional[TableContext]:
        """Nothing."""
        return None

    def column(self, catalog: str, schema: str, table: str, column: str) -> Optional[ColumnContext]:
        """Nothing."""
        return None


class TrinoCommentsContext:
    """The comments Trino already carries, read under the caller's identity.

    ``query`` is the tool layer's ``run(sql)``: it forwards the identity, so a
    user sees the comments of the tables they may see, and nothing else. A
    refusal from Trino means "nothing known", never an error for the agent.
    """

    def __init__(self, query: Callable[[str], dict]) -> None:
        """Keep the governed ``run(sql)`` callable."""
        self._query = query

    def table(self, catalog: str, schema: str, table: str) -> Optional[TableContext]:
        """The table comment from ``system.metadata.table_comments``."""
        c, s, t = (safe_sql_string(x) for x in (catalog, schema, table))
        try:
            res = self._query(
                "SELECT comment FROM system.metadata.table_comments "
                f"WHERE catalog_name = '{c}' AND schema_name = '{s}' AND table_name = '{t}'"
            )
        except Exception:  # noqa: BLE001 - refused or unavailable: nothing known
            return None
        rows = [r for r in res.get("rows", []) if r and r[0]]
        return TableContext(description=str(rows[0][0])) if rows else None

    def column(self, catalog: str, schema: str, table: str, column: str) -> Optional[ColumnContext]:
        """The column comment from ``information_schema.columns``."""
        c, s, t, col = (safe_sql_string(x) for x in (catalog, schema, table, column))
        try:
            res = self._query(
                f"SELECT column_name, comment FROM {catalog}.information_schema.columns "
                f"WHERE table_schema = '{s}' AND table_name = '{t}' AND column_name = '{col}'"
            )
        except Exception:  # noqa: BLE001
            return None
        rows = [r for r in res.get("rows", []) if r and len(r) > 1 and r[1]]
        return ColumnContext(description=str(rows[0][1])) if rows else None


class FileContext:
    """A versioned JSON document kept with the code; re-read when it changes.

    Format ``version: 1``: ``{"version": 1, "tables": {"catalog.schema.table":
    {description, owner, tier, grain: [...], joins: [{columns, target,
    target_columns}], tags: [...], columns: {name: {description,
    classification: [...], values: [...]}}}}}``. Any pipeline that emits this
    shape is a valid producer; an unknown version is refused at start.
    """

    def __init__(self, path: str | Path) -> None:
        """Load ``path`` now; a missing file or an unknown version refuses to start."""
        self._path = Path(path)
        self._mtime = -1.0
        self._tables: dict[str, dict] = {}
        self._lock = threading.Lock()
        if not self._path.exists():
            raise FileNotFoundError(f"MCP_CONTEXT_FILE {self._path} does not exist")
        self._reload()

    def _reload(self) -> None:
        mtime = self._path.stat().st_mtime
        if mtime == self._mtime:
            return
        doc = json.loads(self._path.read_text(encoding="utf-8"))
        if doc.get("version") != FILE_FORMAT_VERSION:
            raise ValueError(
                f"{self._path}: context file version {doc.get('version')!r}, "
                f"expected {FILE_FORMAT_VERSION}"
            )
        self._tables = {str(k): v for k, v in (doc.get("tables") or {}).items()}
        self._mtime = mtime

    def _entry(self, catalog: str, schema: str, table: str) -> Optional[dict]:
        with self._lock:
            self._reload()
            return self._tables.get(f"{catalog}.{schema}.{table}")

    def table(self, catalog: str, schema: str, table: str) -> Optional[TableContext]:
        """The table's entry, if the file has one."""
        e = self._entry(catalog, schema, table)
        if e is None:
            return None
        joins = [
            Join(
                columns=list(j.get("columns", [])),
                target=str(j.get("target", "")),
                target_columns=list(j.get("target_columns", [])),
            )
            for j in e.get("joins", [])
        ]
        return TableContext(
            description=str(e.get("description", "")),
            owner=str(e.get("owner", "")),
            tier=str(e.get("tier", "")),
            grain=list(e.get("grain", [])),
            joins=joins,
            tags=list(e.get("tags", [])),
        )

    def column(self, catalog: str, schema: str, table: str, column: str) -> Optional[ColumnContext]:
        """The column's entry under the table, if the file has one."""
        e = self._entry(catalog, schema, table)
        c = (e or {}).get("columns", {}).get(column)
        if not c:
            return None
        return ColumnContext(
            description=str(c.get("description", "")),
            classification=list(c.get("classification", [])),
            values=[str(v) for v in c.get("values", [])],
        )


class ChainContext:
    """Several providers, merged field by field; the first that speaks wins."""

    def __init__(self, providers: Sequence[Any]) -> None:
        """Providers in priority order."""
        self._providers = list(providers)

    def table(self, catalog: str, schema: str, table: str) -> Optional[TableContext]:
        """Merge every provider's answer, earlier providers taking precedence."""
        answers = [a for p in self._providers if (a := p.table(catalog, schema, table))]
        if not answers:
            return None
        merged: dict = {}
        for a in answers:
            for k, v in asdict(a).items():
                if v and not merged.get(k):
                    merged[k] = v
        merged["joins"] = [Join(**j) for j in merged.get("joins", [])]
        return TableContext(**merged)

    def column(self, catalog: str, schema: str, table: str, column: str) -> Optional[ColumnContext]:
        """Merge every provider's answer for the column."""
        answers = [a for p in self._providers if (a := p.column(catalog, schema, table, column))]
        if not answers:
            return None
        merged: dict = {}
        for a in answers:
            for k, v in asdict(a).items():
                if v and not merged.get(k):
                    merged[k] = v
        return ColumnContext(**merged)


class _Cached:
    """Remember answers for ``ttl`` seconds: a catalogue is slow and models ask often."""

    def __init__(self, inner: Any, ttl_seconds: float, clock: Callable[[], float]) -> None:
        self._inner, self._ttl, self._clock = inner, ttl_seconds, clock
        self._store: dict[tuple, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def _get(self, key: tuple, compute: Callable[[], Any]) -> Any:
        now = self._clock()
        with self._lock:
            hit = self._store.get(key)
            if hit and hit[0] > now:
                return hit[1]
        value = compute()
        with self._lock:
            self._store[key] = (now + self._ttl, value)
        return value

    def table(self, catalog: str, schema: str, table: str) -> Optional[TableContext]:
        """Cached ``table``."""
        return self._get(
            ("t", catalog, schema, table), lambda: self._inner.table(catalog, schema, table)
        )

    def column(self, catalog: str, schema: str, table: str, column: str) -> Optional[ColumnContext]:
        """Cached ``column``."""
        return self._get(
            ("c", catalog, schema, table, column),
            lambda: self._inner.column(catalog, schema, table, column),
        )


def cached(
    provider: Any, *, ttl_seconds: float, clock: Callable[[], float] = time.monotonic
) -> Any:
    """Wrap ``provider`` in a TTL cache; a TTL of 0 returns it unwrapped."""
    return provider if ttl_seconds <= 0 else _Cached(provider, ttl_seconds, clock)


_BUILTIN = ("none", "trino-comments", "file")
ENTRY_POINT_GROUP = "akko_mcp_trino.context"


def _entry_points() -> Sequence[Any]:
    """The providers other packages register under ``akko_mcp_trino.context``."""
    return list(entry_points(group=ENTRY_POINT_GROUP))


def build_context(env: Mapping[str, str], query: Callable[[str], dict]) -> Any:
    """Build the provider chain from ``MCP_CONTEXT_PROVIDERS`` (comma-separated, priority order).

    Built-in names: ``trino-comments``, ``file`` (needs ``MCP_CONTEXT_FILE``),
    ``none``. Any other name is looked up among the entry points other packages
    register under ``akko_mcp_trino.context``; the factory found is called as
    ``factory(env, query)``. An unknown name refuses to start and lists what is
    installed. A product may also pass a provider directly to
    ``build_server(context=...)``.
    """
    names = [n.strip() for n in env.get("MCP_CONTEXT_PROVIDERS", "").split(",") if n.strip()]
    plugins = {ep.name: ep for ep in _entry_points()}
    providers: list[Any] = []
    for name in names:
        if name == "none":
            continue
        if name == "trino-comments":
            providers.append(TrinoCommentsContext(query))
        elif name == "file":
            path = env.get("MCP_CONTEXT_FILE", "")
            if not path:
                raise ValueError(
                    "MCP_CONTEXT_PROVIDERS includes 'file' but MCP_CONTEXT_FILE is empty"
                )
            providers.append(FileContext(path))
        elif name in plugins:
            providers.append(plugins[name].load()(env, query))
        else:
            raise ValueError(
                f"unknown context provider {name!r}; built-in: {_BUILTIN}; "
                f"installed plugins: {sorted(plugins) or 'none'}"
            )
    if not providers:
        return NoContext()
    ttl = float(env.get("MCP_CONTEXT_TTL_SECONDS", os.environ.get("MCP_CONTEXT_TTL_SECONDS", "60")))
    return cached(ChainContext(providers), ttl_seconds=ttl)
