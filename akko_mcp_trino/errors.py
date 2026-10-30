"""What an error means for an agent: the kind that says what to do about it.

Every error a tool answers is ``{"error": message, "kind": …}``, plus ``identifier``
when Trino names the column, table, schema or catalog it could not resolve. The
message is Trino's, whole; the kind is what an agent can act on:

* a Trino user error keeps a kind of its own (``syntax``, ``column``, ``table``,
  ``schema``, ``catalog``, ``permission``, ``function``, ``type``) or ``other``;
* the server's own refusals are ``invalid_argument``, ``read_only`` and ``time_budget``;
* an engine that could not answer (internal error, connection lost) is ``unavailable``,
  which says nothing about the statement: it is neither valid nor invalid.

The audit is not affected: it keeps ``akko_mcp_trino.audit.error_code``.
"""

import re
from typing import Any, Mapping

from trino.exceptions import HttpError, TrinoConnectionError

INVALID_ARGUMENT = "invalid_argument"
READ_ONLY = "read_only"
TIME_BUDGET = "time_budget"
UNAVAILABLE = "unavailable"
OTHER = "other"

# Trino's error names (the error codes of its StandardErrorCode) to what an agent can act
# on. A user error outside this table is "other".
ERROR_KINDS: Mapping[str, str] = {
    "SYNTAX_ERROR": "syntax",
    "COLUMN_NOT_FOUND": "column",
    "MISSING_COLUMN_NAME": "column",
    "AMBIGUOUS_NAME": "column",
    "TABLE_NOT_FOUND": "table",
    "MISSING_TABLE": "table",
    "SCHEMA_NOT_FOUND": "schema",
    "MISSING_SCHEMA_NAME": "schema",
    "CATALOG_NOT_FOUND": "catalog",
    "MISSING_CATALOG_NAME": "catalog",
    "PERMISSION_DENIED": "permission",
    "FUNCTION_NOT_FOUND": "function",
    "TYPE_MISMATCH": "type",
    "INVALID_LITERAL": "type",
}
_NAMED = re.compile(r"\b(column|table|schema|catalog) '([^']+)'", re.I)
_CONNECTION_ERRORS = (OSError, HttpError, TrinoConnectionError)


def explain_error(exc: BaseException) -> dict:
    """``{"error", "kind"}`` for ``exc``, plus ``"identifier"`` when Trino names the object
    its kind is about. ``error`` is the exception's message, whole."""
    out: dict[str, Any] = {"error": str(exc)}
    name = getattr(exc, "error_name", None)
    if isinstance(exc, TimeoutError):  # CallTimeout is one
        out["kind"] = TIME_BUDGET
    elif name and getattr(exc, "error_type", None) == "USER_ERROR":
        kind = ERROR_KINDS.get(str(name), OTHER)
        out["kind"] = kind
        named = _NAMED.search(str(getattr(exc, "message", "") or exc))
        if named and named.group(1).lower() == kind:
            out["identifier"] = named.group(2)
    elif name or isinstance(exc, _CONNECTION_ERRORS):
        out["kind"] = UNAVAILABLE
    elif isinstance(exc, ValueError):
        out["kind"] = INVALID_ARGUMENT
    else:
        out["kind"] = OTHER
    return out
