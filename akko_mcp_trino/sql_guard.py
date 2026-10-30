"""SQL guards: literal escaping, identifier validation, read-only classification.

Pure logic, no I/O. Read-only classification is done on the AST (sqlglot), not
by looking at the first keyword: it rejects multi-statement input, a WITH that
wraps a write, and side-effecting commands. Anything that cannot be parsed is
refused. Fail closed.
"""

from __future__ import annotations

import re

import sqlglot
from sqlglot import exp
from sqlglot.dialects.trino import Trino
from sqlglot.tokens import TokenType

# A Trino identifier: alphanumerics, underscore and hyphen only.
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")

# sqlglot expression types that are purely READS.
_READ_TYPES = (exp.Select, exp.Union, exp.Describe, exp.Pragma)
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
# SHOW/EXPLAIN/DESC parse as a generic `Command` (sqlglot does not structure
# them); they are allowed by leading keyword, reads only.
_COMMAND_READ_HEADS = ("SHOW", "DESCRIBE", "DESC")
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


def _is_system(name: str | None) -> bool:
    return (name or "").lower() == "system"


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


def _show_is_governed(rest: str, session_catalog: str | None) -> bool:
    """Whether ``SHOW <rest>`` stays out of the ``system`` catalog.

    sqlglot keeps SHOW as an opaque command, so its words are tokenized: a name
    ``system`` anywhere refuses it. When the session catalog is ``system``, only
    ``SHOW CATALOGS`` and a SHOW SCHEMAS / TABLES / COLUMNS whose FROM names a
    catalog are let through; any other SHOW would be read in ``system``.
    """
    tokens = Trino().tokenize(rest)  # the whole statement already parsed, so this tokenizes
    if any(t.token_type != TokenType.STRING and _is_system(t.text) for t in tokens):
        return False
    if not _is_system(session_catalog):
        return True
    kind = tokens[0].text.upper() if tokens else ""
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


def is_read_only_sql(sql: str, session_catalog: str | None = None) -> bool:
    """True if and only if the query is purely a read, decided on the AST (sqlglot).

    Stricter and safer than a leading-keyword filter: rejects multi-statement
    input (``SELECT 1; DROP TABLE t``), a WITH that wraps a write, and
    side-effecting commands (GRANT, CALL, SET, ...). Fail closed: a query that
    cannot be parsed is refused. ``session_catalog`` is the catalog Trino reads
    a name without catalog in (``TRINO_CATALOG``); when it is ``system``, such
    names are refused like ``system.`` written out.
    """
    s = sql.strip()
    if not s:
        return False
    try:
        statements = [st for st in sqlglot.parse(s, read="trino") if st is not None]
    except Exception:  # noqa: BLE001 — parse impossible → refus (fail-closed)
        return False
    if len(statements) != 1:  # exactly one statement, never stacked queries
        return False
    st = statements[0]
    if isinstance(st, _READ_TYPES):
        # A read at the root is not enough: refuse any write nested anywhere.
        if any(isinstance(node, _WRITE_TYPES) for node in st.walk()):
            return False
        return not _reads_outside_governed_tables(st, session_catalog)
    if isinstance(st, exp.Command):  # SHOW / EXPLAIN / DESC, unstructured
        head, rest = (s.split(None, 1) + [""])[:2]
        head = head.upper()
        if head == "EXPLAIN":
            m = _EXPLAIN_RE.match(s)
            if m is None or m.group(1):  # malformed, or ANALYZE: it would run the statement
                return False
            # the explained statement must itself be a read
            return is_read_only_sql(m.group(2), session_catalog)
        if head == "SHOW":
            return _show_is_governed(rest, session_catalog)
        return head in _COMMAND_READ_HEADS
    return False  # Insert / Update / Delete / Create / Drop / Alter / Grant / …


def referenced_tables(sql: str) -> list[str]:
    """The tables a statement names, as written, in lower case, once each, sorted.

    For the audit record, not for access: Trino decides what may be read. A
    name defined by the statement itself (a CTE) is not a table; a table
    function is not a name. ``DESCRIBE`` and ``EXPLAIN`` are seen through. A
    statement that does not parse names nothing.
    """
    s = sql.strip()
    m = _EXPLAIN_RE.match(s)
    if m is not None:
        return referenced_tables(m.group(2))
    try:
        statements = [st for st in sqlglot.parse(s, read="trino") if st is not None]
    except Exception:  # noqa: BLE001 - unparseable: nothing named
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
