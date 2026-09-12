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

# A Trino identifier: alphanumerics, underscore and hyphen only.
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")

# sqlglot expression types that are purely READS.
_READ_TYPES = (exp.Select, exp.Union, exp.Describe, exp.Pragma)
# SHOW/EXPLAIN/DESC parse as a generic `Command` (sqlglot does not structure
# them); they are allowed by leading keyword, reads only.
_COMMAND_READ_HEADS = ("SHOW", "EXPLAIN", "DESCRIBE", "DESC")


def safe_sql_string(text: str) -> str:
    """Escape a user string for safe inclusion in a Trino SQL literal.

    Handles quotes, backslashes and NUL bytes. The caller wraps the result in
    single quotes: ``f"'{safe_sql_string(val)}'"``.
    """
    text = text.replace("\0", "")
    text = text.replace("\\", "\\\\")
    text = text.replace("'", "''")
    return text


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


def is_read_only_sql(sql: str) -> bool:
    """True if and only if the query is purely a read, decided on the AST (sqlglot).

    Stricter and safer than a leading-keyword filter: rejects multi-statement
    input (``SELECT 1; DROP TABLE t``), a WITH that wraps a write, and
    side-effecting commands (GRANT, CALL, SET, ...). Fail closed: a query that
    cannot be parsed is refused.
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
        return True
    if isinstance(st, exp.Command):  # SHOW / EXPLAIN / DESC, unstructured
        head = s.upper().split(None, 1)[0]
        return head in _COMMAND_READ_HEADS
    return False  # Insert / Update / Delete / Create / Drop / Alter / Grant / …
