"""What Trino lists to a caller, asked without a catalogue's names, cached per caller.

A name a catalogue cites (the target of a join) is kept only once Trino has listed
it to the caller. The proof never puts that name in a statement: the schemas of a
listed catalog are read without a name filter, then the tables of a listed schema
by ``WHERE table_schema = '<schema>'``, and the name is looked up in memory. A
catalog or a schema enters a statement only after Trino has listed it.

Every statement goes through the ``run`` this cache is given, which the caller's
wiring tags (``mcp-gate``) and binds to the verified caller. An entry is kept per
caller, under everything that changes what Trino allows: the identity mode, the
subject, the active roles and the catalog. A failed statement is never kept and
proves nothing. Nothing logged names a caller or an object.
"""

import logging
import threading
import time
from collections.abc import Callable, Collection, Hashable, Iterable, Sequence
from dataclasses import dataclass, replace
from typing import Any

from ..config import Config
from ..sql_guard import sql_literal
from .caches import ByteCache, serialized_size
from .types import TableRef

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class VisibilityKey:
    """Everything the core sends Trino that changes what a caller is allowed to see."""

    mode: str
    subject: str
    roles: tuple[str, ...]
    catalog: str


def key_for(config: Config, catalog: str, subject: str) -> VisibilityKey:
    """Return the key of ``subject`` in ``catalog`` under the configured identity mode.

    The core sends no role to Trino, so ``roles`` is empty.
    """
    return VisibilityKey(config.trino_identity_mode, subject, (), catalog)


def _ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _size(value: Any) -> int:
    if isinstance(value, frozenset):
        return serialized_size(sorted(value))
    return serialized_size(value)


class Visibility:
    """Per-caller proofs of what Trino lists, bounded in bytes and in time."""

    def __init__(
        self,
        run: Callable[..., dict],
        *,
        ttl_seconds: float,
        max_bytes: int,
        max_names: int,
        canary: str,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Ask through ``run``, keep answers ``ttl_seconds`` within ``max_bytes``.

        A schema of more than ``max_names`` tables proves none of them. A list of
        catalogs or schemas cut at ``max_names`` keeps whatever Trino returned first,
        so above the bound proofs may vary between calls, never beyond what Trino
        lists. ``canary`` is the ``catalog.schema.table`` no caller may see, or
        empty; it is folded to lower case, as Trino folds an unquoted name.
        """
        self._run = run
        self._max_names = max_names
        self._canary = tuple(canary.lower().split(".")) if canary else ()
        self._cache = ByteCache(max_bytes, ttl_seconds, clock=clock)
        self._lock = threading.Lock()

    @property
    def bytes(self) -> int:
        """Return the sum of the sizes of the entries held."""
        return self._cache.bytes

    def _get(self, key: Hashable) -> tuple[bool, Any]:
        with self._lock:
            return self._cache.get(key)

    def _put(self, key: Hashable, value: Any) -> None:
        with self._lock:
            self._cache.put(key, value, _size(value))

    def _names(self, sql: str) -> tuple[frozenset[str], bool]:
        """Return the first column of ``sql``, and whether more than the bound came."""
        res = self._run(sql, row_limit=self._max_names + 1)
        rows = res.get("rows", [])
        cut = bool(res.get("truncated")) or len(rows) > self._max_names
        return frozenset(str(r[0]) for r in rows[: self._max_names]), cut

    def catalogs(self, key: VisibilityKey) -> frozenset[str]:
        """Return the catalogs Trino lists to the caller (``SHOW CATALOGS``)."""
        entry = ("catalogs", replace(key, catalog=""))
        hit, value = self._get(entry)
        if hit:
            return value
        names, _ = self._names("SHOW CATALOGS")
        self._put(entry, names)
        return names

    def schemas(self, key: VisibilityKey) -> frozenset[str]:
        """Return the schemas of ``key.catalog`` Trino lists, none for an unlisted catalog."""
        entry = ("schemas", key)
        hit, value = self._get(entry)
        if hit:
            return value
        if key.catalog not in self.catalogs(key):
            return frozenset()
        names, _ = self._names(
            f"SELECT schema_name FROM {_ident(key.catalog)}.information_schema.schemata"
        )
        self._put(entry, names)
        return names

    def tables(self, key: VisibilityKey, schema: str) -> frozenset[str] | None:
        """Return the tables of a listed ``schema``, or ``None`` past the name bound.

        A schema Trino does not list is never queried and has no table.
        """
        entry = ("tables", key, schema)
        hit, value = self._get(entry)
        if hit:
            return value
        if schema not in self.schemas(key):
            return frozenset()
        names, cut = self._names(
            f"SELECT table_name FROM {_ident(key.catalog)}.information_schema.tables "
            f"WHERE table_schema = {sql_literal(schema)}"
        )
        found = None if cut else names
        self._put(entry, found)
        return found

    def listed(self, key: VisibilityKey, refs: Iterable[TableRef]) -> None:
        """Record tables Trino has just listed or described to the caller.

        Such a table is proven whatever an older entry says, for the TTL: with a
        TTL of 0 nothing is kept.
        """
        caller = replace(key, catalog="")
        for ref in refs:
            self._put(("listed", caller, ref), True)

    def proven(self, key: VisibilityKey, ref: TableRef) -> bool:
        """Return whether Trino lists ``ref`` to the caller; a failed read proves nothing."""
        if self._get(("listed", replace(key, catalog=""), ref))[0]:
            return True
        try:
            names = self.tables(replace(key, catalog=ref.catalog), ref.schema)
        except Exception as exc:
            _log.warning("a visibility read failed (%s): nothing is proven", type(exc).__name__)
            return False
        return names is not None and ref.table in names

    def canary_clear(self, key: VisibilityKey) -> bool:
        """Return whether the configured canary is hidden from the caller.

        Without a canary there is nothing to check. A canary the caller sees means
        Trino does not filter its listings: no context is served to that caller.
        A failed check is not clear and is not kept.
        """
        if not self._canary:
            return True
        caller = replace(key, catalog="")
        entry = ("canary", caller)
        hit, value = self._get(entry)
        if hit:
            return value
        catalog, schema, table = self._canary
        try:
            if catalog not in self.catalogs(caller):
                clear = True
            else:
                res = self._run(
                    f"SELECT table_name FROM {_ident(catalog)}.information_schema.tables "
                    f"WHERE table_schema = {sql_literal(schema)} "
                    f"AND table_name = {sql_literal(table)}",
                    row_limit=1,
                )
                clear = not res.get("rows")
        except Exception as exc:
            _log.warning("the canary check failed (%s): no context is served", type(exc).__name__)
            return False
        if not clear:
            _log.warning(
                "the context canary is listed to a caller: Trino does not filter its "
                "listings, so no context is served to that caller"
            )
        self._put(entry, clear)
        return clear

    def selectable(
        self,
        key: VisibilityKey,
        ref: TableRef,
        columns: Sequence[str],
        *,
        listed: Collection[str],
    ) -> bool:
        """Return whether Trino validates a read of ``columns`` of ``ref`` by the caller.

        ``listed`` are the columns Trino has just listed for ``ref`` (``DESCRIBE``):
        only the columns of ``columns`` also in ``listed`` are named, and none left
        means not selectable, without a statement. Only a proven table is named.
        ``EXPLAIN (TYPE VALIDATE)`` returns no plan; a refusal or a failure is not kept.
        """
        validated = tuple(c for c in columns if c in listed)
        if not validated or not self.proven(key, ref):
            return False
        entry = ("select", replace(key, catalog=""), ref, validated)
        if self._get(entry)[0]:
            return True
        names = ", ".join(_ident(c) for c in validated)
        table = ".".join(_ident(n) for n in (ref.catalog, ref.schema, ref.table))
        try:
            self._run(f"EXPLAIN (TYPE VALIDATE) SELECT {names} FROM {table}")
        except Exception as exc:
            _log.warning("a select check failed (%s): context is not served", type(exc).__name__)
            return False
        self._put(entry, True)
        return True
