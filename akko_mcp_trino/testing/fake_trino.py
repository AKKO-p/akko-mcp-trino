"""A Trino that answers the core's own statements from each user's rights, and records them.

:class:`FakeTrino` stands in for ``trino.dbapi.connect`` (``install(monkeypatch)``). Each
user sees what :class:`Visible` gives them: tables, and the columns Trino lists of each.
A table or a column a user does not see does not exist for them, as a Trino whose access
control filters its listings shows it. It answers the fixed statements of the core:
``SHOW CATALOGS``, ``information_schema.schemata`` and ``.tables``, ``DESCRIBE``,
``system.jdbc.columns``, ``system.metadata.table_comments`` and
``EXPLAIN (TYPE VALIDATE)``. Any other statement fails and is kept in ``unanswered``.

Every statement is recorded with the user it runs as and its client tags
(:class:`Statement`), and with ``unlisted``: the names it holds that Trino had not listed
to that user before it. Names are read where the core writes them, as quoted identifiers,
string literals and dotted references, folded to lower case as Trino folds them. The
words of Trino's own metadata (``information_schema``, ``system.jdbc.columns``...) and
the names given as ``configured`` (a canary) are never counted as unlisted.
"""

import re
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..context.types import TableRef

# The words of Trino's own metadata the core names in its statements.
_METADATA = frozenset(
    {
        "information_schema",
        "schemata",
        "tables",
        "system",
        "jdbc",
        "columns",
        "metadata",
        "table_comments",
    }
)
_TOKEN = re.compile(
    r"\"(?P<quoted>(?:[^\"]|\"\")*)\"|'(?P<literal>(?:[^']|'')*)'"
    r"|(?P<bare>[A-Za-z_][\w-]*(?:\s*\.\s*(?:\"(?:[^\"]|\"\")*\"|[A-Za-z_][\w-]*))+)"
)
_PART = re.compile(r"\"((?:[^\"]|\"\")*)\"|([A-Za-z_][\w-]*)")
_NAME = r"(?:\"(?:[^\"]|\"\")*\"|[A-Za-z_][\w-]*)"
_REF = rf"{_NAME}\s*\.\s*{_NAME}\s*\.\s*{_NAME}"
_LITERAL = r"'((?:[^']|'')*)'"
_OFFSET = re.compile(r"\bOFFSET\s+(\d+)", re.IGNORECASE)
_LIMIT = re.compile(r"\bLIMIT\s+(\d+)", re.IGNORECASE)

_DESCRIBE = ("Column", "Type", "Extra", "Comment")


@dataclass(frozen=True)
class Visible:
    """What one user sees: each table, with the columns Trino lists of it, in order.

    Names are written as Trino lists them, in lower case (``TableRef.of`` folds them).
    """

    tables: Mapping[TableRef, tuple[str, ...]]


@dataclass(frozen=True)
class Statement:
    """One statement as Trino received it: who ran it, its text and its client tags.

    ``unlisted`` holds the names in the text that Trino had not listed to ``user`` before.
    """

    user: str
    sql: str
    client_tags: tuple[str, ...]
    unlisted: tuple[str, ...]


def _names(text: str) -> list[str]:
    """The names of a reference or a list, unquoted and folded to lower case."""
    return [
        (quoted.replace('""', '"') if quoted else bare).lower()
        for quoted, bare in _PART.findall(text)
    ]


def _unquote(text: str) -> str:
    return _names(text)[0]


def _ref(text: str) -> TableRef:
    return TableRef(*_names(text))


def _literal(text: str) -> str:
    return text.replace("''", "'").lower()


def names_in(sql: str) -> list[str]:
    """Return the names ``sql`` holds, in order, folded: identifiers, literals, references."""
    found: list[str] = []
    for match in _TOKEN.finditer(sql):
        if match.group("quoted") is not None:
            found.append(match.group("quoted").replace('""', '"').lower())
        elif match.group("literal") is not None:
            found.append(_literal(match.group("literal")))
        else:
            found.extend(_names(match.group("bare")))
    return found


class _Refused(Exception):
    """A statement this Trino does not answer; the caller receives Trino's error instead."""


def _trino_error(name: str, message: str) -> Exception:
    """Trino's user error ``name`` with ``message``, as the Trino client raises it."""
    from trino.exceptions import TrinoUserError

    return TrinoUserError(
        {"errorName": name, "errorType": "USER_ERROR", "message": message}, "fake-query"
    )


class FakeTrino:
    """Answers the core's statements from ``rights``, by user; records each statement."""

    def __init__(self, rights: Mapping[str, Visible], *, configured: Iterable[str] = ()) -> None:
        """Serve ``rights``; never count the names of ``configured`` as unlisted."""
        self._rights = dict(rights)
        self._configured = frozenset(name.lower() for name in configured)
        self._listed: dict[str, set[str]] = {}
        self._lock = threading.Lock()
        self.statements: list[Statement] = []
        self.unanswered: list[Statement] = []

    def install(self, monkeypatch: Any) -> None:
        """Replace ``trino.dbapi.connect`` with this Trino for the test."""
        import trino.dbapi

        monkeypatch.setattr(trino.dbapi, "connect", self.connect)

    def connect(self, **settings: Any) -> "_Connection":
        """A connection for ``settings["user"]`` and its ``client_tags``, as the client opens."""
        tags = tuple(settings.get("client_tags") or ())
        return _Connection(self, str(settings.get("user") or ""), tags)

    # ---- the statements

    def _tables(self, user: str) -> Mapping[TableRef, tuple[str, ...]]:
        visible = self._rights.get(user)
        return visible.tables if visible is not None else {}

    def run(self, user: str, sql: str, tags: tuple[str, ...]) -> tuple[Sequence[str], list]:
        """Record ``sql`` run by ``user``, then answer it: column names and rows."""
        text = " ".join(sql.split())
        with self._lock:
            listed = self._listed.setdefault(user, set())
            unlisted = [n for n in names_in(text) if n not in listed | _METADATA | self._configured]
            statement = Statement(user, sql, tags, tuple(dict.fromkeys(unlisted)))
            self.statements.append(statement)
        try:
            columns, rows, seen = self._answer(user, text)
        except _Refused as refused:
            with self._lock:
                self.unanswered.append(statement)
            raise _trino_error("NOT_SUPPORTED", str(refused)) from None
        with self._lock:
            listed.update(name.lower() for name in seen)
        return columns, rows

    def _answer(self, user: str, sql: str) -> tuple[Sequence[str], list, list[str]]:
        tables = self._tables(user)
        catalogs = sorted({ref.catalog for ref in tables})
        for pattern, answer in self._patterns():
            match = re.fullmatch(pattern, sql, re.IGNORECASE)
            if match is not None:
                return answer(match, tables, catalogs)
        raise _Refused(f"the fake Trino does not answer: {sql}")

    def _patterns(self) -> list[tuple[str, Callable[..., tuple[Sequence[str], list, list[str]]]]]:
        return [
            (r"SHOW CATALOGS", self._catalogs),
            (
                rf"SELECT schema_name FROM ({_NAME})\.information_schema\.schemata"
                rf"(?: WHERE schema_name = {_LITERAL})?",
                self._schemata,
            ),
            (
                rf"SELECT table_name FROM ({_NAME})\.information_schema\.tables "
                rf"WHERE table_schema = {_LITERAL}(?: AND table_name = {_LITERAL})?",
                self._table_names,
            ),
            (
                rf"SELECT table_name, table_type FROM ({_NAME})\.information_schema\.tables "
                rf"WHERE table_schema = {_LITERAL} ORDER BY table_name(.*)",
                self._page,
            ),
            (r"SELECT \* FROM \((SELECT table_name, column_name, .*)\) ORDER BY .*", self._jdbc),
            (r"SELECT \* FROM \((SELECT table_name, comment .*)\) ORDER BY [^)]*", self._comments),
            (rf"DESCRIBE ({_REF})", self._describe),
            (rf"EXPLAIN \(TYPE VALIDATE\) SELECT (.+) FROM ({_REF})", self._validate),
        ]

    @staticmethod
    def _catalogs(match, tables, catalogs):
        names = sorted({"system", *catalogs})
        return ["Catalog"], [[n] for n in names], names

    @staticmethod
    def _schemata(match, tables, catalogs):
        catalog, wanted = _unquote(match.group(1)), match.group(2)
        schemas = sorted({ref.schema for ref in tables if ref.catalog == catalog})
        if wanted is not None:
            schemas = [s for s in schemas if s == _literal(wanted)]
        return ["schema_name"], [[s] for s in schemas], schemas

    @staticmethod
    def _table_names(match, tables, catalogs):
        catalog, schema = _unquote(match.group(1)), _literal(match.group(2))
        names = sorted(r.table for r in tables if (r.catalog, r.schema) == (catalog, schema))
        if match.group(3) is not None:
            names = [n for n in names if n == _literal(match.group(3))]
        return ["table_name"], [[n] for n in names], names

    @staticmethod
    def _page(match, tables, catalogs):
        catalog, schema = _unquote(match.group(1)), _literal(match.group(2))
        names = sorted(r.table for r in tables if (r.catalog, r.schema) == (catalog, schema))
        names = _window(names, match.group(3))
        return ["table_name", "table_type"], [[n, "BASE TABLE"] for n in names], names

    @staticmethod
    def _jdbc(match, tables, catalogs):
        branches = match.group(1)
        catalog = _literal(re.search(rf"table_cat = {_LITERAL}", branches).group(1))
        schema = _literal(re.search(rf"table_schem = {_LITERAL}", branches).group(1))
        rows = []
        for name in sorted(
            {_literal(n) for n in re.findall(rf"table_name = {_LITERAL}", branches)}
        ):
            for position, column in enumerate(tables.get(TableRef(catalog, schema, name), ())):
                rows.append([name, column, "varchar", None, position + 1])
        rows = _window(rows, match.group(0)[match.end(1) :])
        seen = [name for row in rows for name in row[:2]]
        return ["table_name", "column_name", "type_name", "remarks", "ordinal_position"], rows, seen

    @staticmethod
    def _comments(match, tables, catalogs):
        branches = match.group(1)
        catalog = _literal(re.search(rf"catalog_name = {_LITERAL}", branches).group(1))
        schema = _literal(re.search(rf"schema_name = {_LITERAL}", branches).group(1))
        names = sorted({_literal(n) for n in re.findall(rf"table_name = {_LITERAL}", branches)})
        names = [n for n in names if TableRef(catalog, schema, n) in tables]
        return ["table_name", "comment"], [[n, None] for n in names], names

    @staticmethod
    def _describe(match, tables, catalogs):
        ref = _ref(match.group(1))
        if ref not in tables:
            raise _trino_error(
                "TABLE_NOT_FOUND", f"line 1:1: Table '{_dotted(ref)}' does not exist"
            )
        columns = tables[ref]
        return _DESCRIBE, [[c, "varchar", "", ""] for c in columns], [*_parts(ref), *columns]

    @staticmethod
    def _validate(match, tables, catalogs):
        ref = _ref(match.group(2))
        if ref not in tables:
            raise _trino_error(
                "TABLE_NOT_FOUND", f"line 1:1: Table '{_dotted(ref)}' does not exist"
            )
        for column in _names(match.group(1)):
            if column not in tables[ref]:
                raise _trino_error(
                    "COLUMN_NOT_FOUND", f"line 1:1: Column '{column}' cannot be resolved"
                )
        return ["Valid"], [[True]], []


def _parts(ref: TableRef) -> tuple[str, str, str]:
    return ref.catalog, ref.schema, ref.table


def _dotted(ref: TableRef) -> str:
    return ".".join(_parts(ref))


def _window(rows: list, tail: str) -> list:
    """``rows`` from the last ``OFFSET`` and up to the last ``LIMIT`` of ``tail``, if any."""
    offsets, limits = _OFFSET.findall(tail), _LIMIT.findall(tail)
    rows = rows[int(offsets[-1]) if offsets else 0 :]
    return rows[: int(limits[-1])] if limits else rows


class _Connection:
    def __init__(self, trino: FakeTrino, user: str, tags: tuple[str, ...]) -> None:
        self._trino, self._user, self._tags = trino, user, tags

    def cursor(self) -> "_Cursor":
        return _Cursor(self._trino, self._user, self._tags)


class _Cursor:
    """What the core reads of a Trino cursor: execute, fetchmany, description, cancel."""

    query_id = "fake-query"

    def __init__(self, trino: FakeTrino, user: str, tags: tuple[str, ...]) -> None:
        self._trino, self._user, self._tags = trino, user, tags
        self._rows: list = []
        self.description: list[tuple[str]] | None = None

    def execute(self, sql: str, params: Any = None) -> None:
        columns, rows = self._trino.run(self._user, sql, self._tags)
        self.description = [(name,) for name in columns]
        self._rows = [list(row) for row in rows]

    def fetchmany(self, size: int) -> list:
        taken, self._rows = self._rows[:size], self._rows[size:]
        return taken

    def cancel(self) -> None:
        self._rows = []
