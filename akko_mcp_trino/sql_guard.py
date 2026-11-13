"""SQL guards: literal escaping, identifier validation, bounds, read-only classification.

Pure logic, no I/O. Read-only classification is done on the AST (sqlglot), not
by looking at the first keyword: it rejects multi-statement input, a WITH that
wraps a write, and side-effecting commands. Anything that cannot be parsed is
refused. Fail closed.

An agent's statement is bounded before it is parsed (``check_sql_bounds``), then parsed
once (``ParsedSQL``); the parse time grows with some nesting and repetition, and the parse
workers' deadline bounds it (``akko_mcp_trino.parse_pool``).
"""

from __future__ import annotations

import re
from typing import NamedTuple, Optional, Union

import sqlglot
from sqlglot import exp
from sqlglot.dialects.trino import Trino
from sqlglot.tokens import TokenType

# A Trino identifier: alphanumerics, underscore and hyphen only.
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")

# sqlglot expression types that are purely READS: a SELECT, a set operation of reads
# (UNION, EXCEPT, INTERSECT), a parenthesized read and VALUES. DESCRIBE goes by its words
# (see ``_describe_is_governed``).
_READ_TYPES = (exp.Select, exp.SetOperation, exp.Subquery, exp.Values)
# Node types that WRITE or have side effects, wherever they sit in the tree.
# Checking only the root once let ``WITH w AS (DELETE FROM t) SELECT 1``
# through (found by the live adversarial proof, 12 September 2026).
_WRITE_TYPES = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.TruncateTable,
    exp.Grant,
    exp.Command,
    exp.Set,
)
# The commands a read may use, as an allow-list: what an agent needs to find and read
# data. Anything else is refused, including a command a later Trino adds. Some reads,
# such as SHOW STATS or EXPLAIN, can show facts beyond a row filter or a column mask.
# They are refused unless the administrator names them in MCP_EXTRA_READ_COMMANDS.
_SHOW_ALLOWED = frozenset({"CATALOGS", "SCHEMAS", "TABLES", "COLUMNS", "FUNCTIONS"})
# What MCP_EXTRA_READ_COMMANDS may name, and the SHOW kinds each one opens.
EXTRA_READ_COMMANDS: dict[str, frozenset[str]] = {
    "EXPLAIN": frozenset(),
    "SHOW STATS": frozenset({"STATS"}),
    "SHOW CREATE": frozenset({"CREATE"}),
    "SHOW GRANTS": frozenset({"GRANTS"}),
    "SHOW ROLES": frozenset({"ROLES", "CURRENT", "ROLE"}),
    "SHOW SESSION": frozenset({"SESSION"}),
}
# EXPLAIN [ ( options ) ] [ANALYZE] [VERBOSE] <statement>. sqlglot keeps the
# whole thing as an opaque Command, so the statement inside is checked on its
# own. EXPLAIN ANALYZE *executes* the statement it explains and is refused.
_EXPLAIN_RE = re.compile(
    r"^\s*EXPLAIN\s*(?:\([^)]*\))?\s*(ANALYZE\s+)?(?:VERBOSE\s+)?(.*)$", re.I | re.S
)
# How many parts make the name after FROM/IN of a SHOW fully qualified: with
# fewer, Trino fills in the session catalog (and schema).
_SHOW_QUALIFIED_PARTS = {"SCHEMAS": 1, "TABLES": 2, "COLUMNS": 3}
_NAME_TOKENS = (TokenType.VAR, TokenType.IDENTIFIER)


# What the nesting scan stops at: a bracket, a quote, or the start of a comment.
_NESTING_MARKS = re.compile(r"[()\[\]'\"]|--|/\*")
_LINE_END = re.compile(r"[\r\n]")


class SQLBoundError(ValueError):
    """A statement past a bound of the server, refused before it is parsed; ``code`` names
    the bound for the audit, the message tells the agent what to change."""

    def __init__(self, code: str, message: str) -> None:
        """``code``: ``sql_too_long`` or ``sql_too_nested``."""
        super().__init__(message)
        self.code = code


def sql_nesting(sql: str, stop_above: Optional[int] = None) -> int:
    """How deep the round and square brackets of ``sql`` nest, in one linear pass, without
    parsing.

    Brackets inside a string literal (``'...'``, ``''`` inside it), a quoted identifier
    (``"..."``, ``""`` inside it), a line comment (``--`` to the end of the line) or a block
    comment (``/* ... */``) do not count. A closing bracket with none open is ignored, so
    the depth never goes below 0. A literal, identifier or comment left open hides the rest
    of the text, as it does from the parser, which refuses it. With ``stop_above``, the scan
    stops as soon as the depth passes it."""
    depth = deepest = 0
    i = 0
    while True:
        mark = _NESTING_MARKS.search(sql, i)
        if mark is None:
            return deepest
        token, i = mark.group(), mark.end()
        if token in ("(", "["):
            depth += 1
            if depth > deepest:
                deepest = depth
                if stop_above is not None and deepest > stop_above:
                    return deepest
        elif token in (")", "]"):
            depth = max(depth - 1, 0)
        elif token == "--":
            end = _LINE_END.search(sql, i)
            if end is None:
                return deepest
            i = end.end()
        elif token == "/*":
            close = sql.find("*/", i)
            if close < 0:
                return deepest
            i = close + 2
        else:  # a quote: the literal or identifier ends at a quote that is not doubled
            while True:
                close = sql.find(token, i)
                if close < 0:
                    return deepest
                if sql.startswith(token, close + 1):
                    i = close + 2
                    continue
                i = close + 1
                break


def check_sql_bounds(sql: str, *, max_chars: int, max_nesting: int) -> None:
    """Refuse ``sql`` before it is parsed when it is longer than ``max_chars`` characters
    (``MCP_MAX_SQL_CHARS``) or nests its brackets deeper than ``max_nesting``
    (``MCP_MAX_SQL_NESTING``); raises ``SQLBoundError``, a ValueError whose message is for
    the agent. One linear pass over the text, without sqlglot."""
    if len(sql) > max_chars:
        raise SQLBoundError(
            "sql_too_long",
            f"The statement is {len(sql)} characters long; this server reads {max_chars} at "
            "most. Write a shorter statement.",
        )
    if sql_nesting(sql, stop_above=max_nesting) > max_nesting:
        raise SQLBoundError(
            "sql_too_nested",
            f"The statement nests brackets more than {max_nesting} deep; this server reads "
            f"{max_nesting} levels at most. Write it with fewer nested levels, for example "
            "with a WITH clause.",
        )


class ParsedSQL:
    """A statement, parsed at most once.

    The read-only check, the tables the audit names and the advice on an unknown column
    read the same trees: no text is parsed twice. The statement an EXPLAIN explains is
    parsed once too, when first read."""

    def __init__(self, sql: str) -> None:
        """``sql``, stripped, is ``text``; nothing is parsed yet."""
        self.text = sql.strip()
        self._trees: dict[str, Optional[list[exp.Expr]]] = {}

    def trees(self, text: str) -> Optional[list[exp.Expr]]:
        """The statements of ``text`` (this statement, or a part of it, stripped), parsed
        on first use; None when it does not parse."""
        key = text.strip()
        if key not in self._trees:
            try:
                statements = sqlglot.parse(key, read="trino")
                self._trees[key] = [st for st in statements if st is not None]
            except Exception:  # noqa: BLE001 - unparseable: each reader fails closed
                self._trees[key] = None
        return self._trees[key]


def parse_once(sql: str) -> ParsedSQL:
    """``sql`` to read with ``is_read_only_sql``, ``referenced_tables`` and
    ``tables_for_column``, which then share its trees."""
    return ParsedSQL(sql)


def _parsed(sql: Union[str, ParsedSQL]) -> ParsedSQL:
    return sql if isinstance(sql, ParsedSQL) else ParsedSQL(sql)


def _is_system(name: str | None) -> bool:
    return (name or "").lower() == "system"


def parse_extra_reads(text: str) -> frozenset[str]:
    """The commands named in ``MCP_EXTRA_READ_COMMANDS``, comma-separated, case and spaces
    folded (``"show stats, EXPLAIN"``); raises ValueError on a name it does not know, so a
    typo never opens less, or more, than was meant."""
    names = set()
    for raw in text.split(","):
        name = " ".join(raw.split()).upper()
        if not name:
            continue
        if name not in EXTRA_READ_COMMANDS:
            raise ValueError(
                f"MCP_EXTRA_READ_COMMANDS: unknown command {raw.strip()!r}; "
                f"known: {', '.join(EXTRA_READ_COMMANDS)}"
            )
        names.add(name)
    return frozenset(names)


def _reads_outside_governed_tables(node: exp.Expression, session_catalog: str | None) -> bool:
    """True if the query reads through a table function or the ``system`` catalog.

    A table function (``TABLE(core_postgres.system.query(query => '...'))``) sends
    raw SQL to the source under the connector's account: Trino applies neither
    row filters nor column masks. The ``system`` catalog exposes engine state,
    such as other users' SQL in ``system.runtime.queries``. Both are refused.
    A name without catalog is read in ``session_catalog``, so when that is
    ``system`` such a name is refused too; a CTE is not a table.
    """
    ctes = {cte.alias_or_name.lower() for cte in node.find_all(exp.CTE)}
    for table in node.find_all(exp.Table):
        if not isinstance(table.this, exp.Identifier):  # TABLE(fn(...)), not a name
            return True
        catalog = table.catalog
        if not catalog and not table.db and table.name.lower() in ctes:
            continue
        if _is_system(catalog or session_catalog):
            return True
    return False


def _show_is_governed(
    rest: str, session_catalog: str | None, extra_reads: frozenset[str] = frozenset()
) -> bool:
    """Whether ``SHOW <rest>`` is a SHOW a read may use and stays out of the ``system``
    catalog.

    Only the kinds of ``_SHOW_ALLOWED`` pass, plus those ``extra_reads`` opens.

    sqlglot keeps SHOW as an opaque command, so its words are tokenized: a name
    ``system`` anywhere refuses it. When the session catalog is ``system``, only
    ``SHOW CATALOGS`` and a SHOW SCHEMAS / TABLES / COLUMNS whose FROM names a
    catalog are let through; any other SHOW would be read in ``system``.
    """
    tokens = Trino().tokenize(rest)  # the whole statement already parsed, so this tokenizes
    kind = tokens[0].text.upper() if tokens else ""
    allowed = _SHOW_ALLOWED.union(*(EXTRA_READ_COMMANDS[name] for name in extra_reads))
    if kind not in allowed:
        return False
    if any(t.token_type != TokenType.STRING and _is_system(t.text) for t in tokens):
        return False
    if not _is_system(session_catalog):
        return True
    if kind == "CATALOGS":
        return True
    wanted = _SHOW_QUALIFIED_PARTS.get(kind)
    if (
        wanted is None
        or len(tokens) < 3
        or tokens[1].token_type not in (TokenType.FROM, TokenType.IN)
    ):
        return False
    parts, i = 0, 2
    while i < len(tokens) and tokens[i].token_type in _NAME_TOKENS:
        parts += 1
        if i + 1 < len(tokens) and tokens[i + 1].token_type == TokenType.DOT:
            i += 2
            continue
        break
    return parts >= wanted


# The two views of Trino's own `system` catalog the metadata tools read, by statements the
# server builds: names of Trino's protocol, not settings. Trino filters both for each user,
# as it does information_schema. The agent's own SQL never reaches `system`.
JDBC_COLUMNS = "system.jdbc.columns"
TABLE_COMMENTS = "system.metadata.table_comments"


def safe_sql_string(text: str) -> str:
    """Escape a user string for safe inclusion in a Trino SQL literal.

    Doubles the quotes and drops NUL bytes; a backslash stays as it is, since Trino
    reads no backslash escape in a string literal. The caller wraps the result in
    single quotes: ``f"'{safe_sql_string(val)}'"``, which ``sql_literal`` does.
    """
    return text.replace("\0", "").replace("'", "''")


def sql_literal(text: str) -> str:
    """``text`` as a whole Trino string literal, quotes included: never more than a value."""
    return f"'{safe_sql_string(text)}'"


def normalize_sql(sql: str) -> str:
    """Trim the statement and drop one trailing semicolon.

    Models end SQL with ``;`` by habit and Trino refuses it with a syntax
    error. Only one trailing terminator is removed; stacked statements are
    still refused by ``is_read_only_sql``.
    """
    s = sql.strip()
    if s.endswith(";"):
        s = s[:-1].rstrip()
    return s


def validate_identifier(name: str, label: str = "identifier") -> str:
    """Validate and return a Trino SQL identifier, or raise ValueError.

    Only alphanumerics, underscores and hyphens are accepted, so a catalog,
    schema or table name can never carry an injection.
    """
    if not name or not _IDENTIFIER_RE.match(name):
        raise ValueError(
            f"Invalid {label}: {name!r}. "
            "Only alphanumeric characters, underscores, and hyphens are allowed."
        )
    return name


def governed_catalog(name: str) -> str:
    """A catalog name for the metadata tools, validated, or raise ValueError.

    The ``system`` catalog exposes engine state, such as other users' SQL in
    ``system.runtime.queries``, and the read-only guard refuses it in a statement;
    a tool that builds its own statement from a catalog name refuses it too.
    """
    validate_identifier(name, "catalog")
    if _is_system(name):
        raise ValueError("The system catalog exposes engine state, not governed data: refused")
    return name


def parse_error(sql: str) -> str | None:
    """Why ``sql`` does not parse as Trino SQL (first line of the parser's message), or None."""
    try:
        sqlglot.parse(sql.strip(), read="trino")
    except Exception as exc:  # noqa: BLE001 - any parser failure is the answer
        return str(exc).splitlines()[0] if str(exc) else type(exc).__name__
    return None


def _describe_is_governed(names: list, session_catalog: str | None) -> bool:
    """Whether the words after DESCRIBE (or DESC), as tokens, are one table name of one to
    three parts, outside the ``system`` catalog. ``DESCRIBE INPUT``, ``DESCRIBE OUTPUT``
    (a prepared statement, which a read never prepares) and any other form are refused."""
    if len(names) % 2 == 0 or len(names) > 5:  # name, or name.name, or name.name.name
        return False
    for i, token in enumerate(names):
        expected = _NAME_TOKENS if i % 2 == 0 else (TokenType.DOT,)
        if token.token_type not in expected:
            return False
    parts = (len(names) + 1) // 2
    catalog = names[0].text if parts == 3 else session_catalog
    return not _is_system(catalog)


def is_read_only_sql(
    sql: Union[str, ParsedSQL],
    session_catalog: str | None = None,
    extra_reads: frozenset[str] = frozenset(),
) -> bool:
    """True if and only if the query is purely a read, decided on the AST (sqlglot).

    Stricter and safer than a leading-keyword filter: rejects multi-statement
    input (``SELECT 1; DROP TABLE t``), a WITH that wraps a write, and
    side-effecting commands (GRANT, CALL, SET, ...). Fail closed: a query that
    cannot be parsed is refused. ``session_catalog`` is the catalog Trino reads
    a name without catalog in (``TRINO_CATALOG``); when it is ``system``, such
    names are refused like ``system.`` written out. Commands go by an allow-list:
    SELECT, DESCRIBE of a table, and SHOW CATALOGS, SCHEMAS, TABLES, COLUMNS and
    FUNCTIONS; ``extra_reads`` (from ``parse_extra_reads``) opens EXPLAIN and the other
    SHOW kinds of ``EXTRA_READ_COMMANDS``, one by one; an EXPLAIN of an EXPLAIN is refused.
    ``sql`` may be a ``ParsedSQL``, whose trees are then read rather than parsed again.
    """
    parsed = _parsed(sql)
    return _is_read_only(parsed, parsed.text, session_catalog, extra_reads)


def _is_read_only(
    parsed: ParsedSQL,
    text: str,
    session_catalog: str | None,
    extra_reads: frozenset[str],
    explained: bool = False,
) -> bool:
    s = text.strip()
    if not s:
        return False
    statements = parsed.trees(s)
    if statements is None:  # parse impossible: refused (fail closed)
        return False
    if len(statements) != 1:  # exactly one statement, never stacked queries
        return False
    st = statements[0]
    tokens = Trino().tokenize(s)
    # The first word, comments skipped: a comment may come before any statement.
    head = tokens[0].text.upper() if tokens else ""
    if head in ("DESCRIBE", "DESC"):
        return _describe_is_governed(tokens[1:], session_catalog)
    if isinstance(st, _READ_TYPES):
        # A read at the root is not enough: refuse any write nested anywhere.
        if any(isinstance(node, _WRITE_TYPES) for node in st.walk()):
            return False
        return not _reads_outside_governed_tables(st, session_catalog)
    if isinstance(st, exp.Command):  # SHOW and EXPLAIN, unstructured
        s = s[tokens[0].start :]
        if head == "EXPLAIN":
            # An EXPLAIN never explains an EXPLAIN.
            if explained or "EXPLAIN" not in extra_reads:
                return False
            m = _EXPLAIN_RE.match(s)
            if m is None or m.group(1):  # malformed, or ANALYZE: it would run the statement
                return False
            # the explained statement must itself be a read
            return _is_read_only(parsed, m.group(2), session_catalog, extra_reads, True)
        if head == "SHOW":
            rest = (s.split(None, 1) + [""])[1]
            return _show_is_governed(rest, session_catalog, extra_reads)
    return False  # Insert / Update / Delete / Create / Drop / Alter / Grant / …


def referenced_tables(sql: Union[str, ParsedSQL]) -> list[str]:
    """The tables a statement names, as written, in lower case, once each, sorted.

    For the audit record, not for access: Trino decides what may be read. A
    name defined by the statement itself (a CTE) is not a table; a table
    function is not a name. ``DESCRIBE`` and one ``EXPLAIN`` are seen through. A
    statement that does not parse names nothing. ``sql`` may be a ``ParsedSQL``.
    """
    parsed = _parsed(sql)
    return _referenced_tables(parsed, parsed.text)


def _referenced_tables(parsed: ParsedSQL, text: str, explained: bool = False) -> list[str]:
    s = text.strip()
    m = None if explained else _EXPLAIN_RE.match(s)  # one EXPLAIN is seen through, not two
    if m is not None:
        return _referenced_tables(parsed, m.group(2), True)
    statements = parsed.trees(s)
    if statements is None:  # unparseable: nothing named
        return []
    names: set[str] = set()
    for st in statements:
        ctes = {cte.alias_or_name.lower() for cte in st.find_all(exp.CTE)}
        for table in st.find_all(exp.Table):
            if not isinstance(table.this, exp.Identifier):
                continue
            parts = [p for p in (table.catalog, table.db, table.name) if p]
            if len(parts) == 1 and parts[0].lower() in ctes:
                continue
            names.add(".".join(parts).lower())
    return sorted(names)


class ColumnScope(NamedTuple):
    """What the advice on an unknown column needs of a statement, as plain values: the
    tables it reads, each as its parts (lower case) and its alias (lower case, or empty),
    and whether a column without a qualifier may belong to them (no CTE, no nested SELECT).
    Picklable, so a worker process can hand it back."""

    bare: bool
    tables: tuple[tuple[tuple[str, ...], str], ...]


class Analysis(NamedTuple):
    """Everything a tool call derives from an agent's statement, as plain picklable values:
    the read-only verdict, the tables the audit names, and the scope of the column advice."""

    read_only: bool
    tables: tuple[str, ...]
    scope: ColumnScope


def column_scope(sql: Union[str, ParsedSQL]) -> ColumnScope:
    """The ``ColumnScope`` of ``sql`` (see ``tables_for_column``); one EXPLAIN is seen
    through, and a statement that does not parse reads no table."""
    parsed = _parsed(sql)
    return _column_scope(parsed, parsed.text)


def _column_scope(parsed: ParsedSQL, text: str, explained: bool = False) -> ColumnScope:
    s = text.strip()
    m = None if explained else _EXPLAIN_RE.match(s)  # one EXPLAIN is seen through, not two
    if m is not None:
        return _column_scope(parsed, m.group(2), True)
    statements = parsed.trees(s)
    if statements is None:  # unparseable: nothing named
        return ColumnScope(False, ())
    bare = True
    tables: list[tuple[tuple[str, ...], str]] = []
    for st in statements:
        ctes = {cte.alias_or_name.lower() for cte in st.find_all(exp.CTE)}
        nested = any(s.find_ancestor(exp.Select) is not None for s in st.find_all(exp.Select))
        # A column without a qualifier may belong to a CTE or a nested SELECT (a subquery,
        # EXISTS, IN), not to a table read inside it: no advice rather than a guess. The
        # branches of a UNION are not nested.
        bare = bare and not (ctes or nested)
        for table in st.find_all(exp.Table):
            if not isinstance(table.this, exp.Identifier):
                continue
            parts = tuple(p.lower() for p in (table.catalog, table.db, table.name) if p)
            if len(parts) == 1 and parts[0] in ctes:
                continue
            tables.append((parts, table.alias.lower()))
    return ColumnScope(bare, tuple(tables))


def tables_in_scope(scope: ColumnScope, identifier: str) -> list[tuple[str, ...]]:
    """The tables of ``scope`` the column ``identifier`` may belong to, sorted (see
    ``tables_for_column``)."""
    qualifier = identifier.lower().rpartition(".")[0]
    if not qualifier and not scope.bare:
        return []
    names = {
        parts
        for parts, alias in scope.tables
        if not qualifier or qualifier in (alias, parts[-1], ".".join(parts))
    }
    return sorted(names)


def tables_for_column(
    sql: Union[str, ParsedSQL, Analysis], identifier: str
) -> list[tuple[str, ...]]:
    """The tables of ``sql`` a column it names may belong to, each as the parts the agent
    wrote (catalog, schema, table, those given), in lower case, sorted. A part keeps any dot
    it holds (``"x.y".t`` is the two parts ``x.y`` and ``t``), so a name is never read as
    another catalog or schema.

    For advice only, from the agent's own statement, never from Trino: a column written
    ``c.customer_id`` belongs to the table whose alias or name is ``c``; a column written
    without a qualifier may belong to any table the statement reads, unless the statement
    has a CTE or a nested SELECT (subquery, EXISTS, IN), which it may belong to instead:
    then nothing. A name defined by the statement (a CTE) is not a table. A statement that
    does not parse names nothing. ``sql`` may be a ``ParsedSQL``, or an ``Analysis``, whose
    scope is then read without any parse."""
    scope = sql.scope if isinstance(sql, Analysis) else column_scope(sql)
    return tables_in_scope(scope, identifier)


def analyze(
    sql: str, session_catalog: str | None, extra_reads: frozenset[str] = frozenset()
) -> Analysis:
    """Parse ``sql`` once and derive what a tool call needs of it (see ``Analysis``). It is
    what a parse worker runs; the server's own process never parses an agent's SQL."""
    parsed = ParsedSQL(sql)
    return Analysis(
        read_only=is_read_only_sql(parsed, session_catalog, extra_reads),
        tables=tuple(referenced_tables(parsed)),
        scope=column_scope(parsed),
    )
