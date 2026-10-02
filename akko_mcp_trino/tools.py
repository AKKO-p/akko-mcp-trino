"""The Trino tools, registered on an MCPServer instance through a registrar.

The tools are closures over a `TrinoClient`, so they are testable with a fake
client. Their docstrings are the descriptions MCP hosts show to the model, so
they are written for a model: what the tool is for, what it returns, and what
a masked value or a missing row means. The core serves the six tools an agent
needs to read Trino: list catalogs, schemas and tables, describe a table,
explain a statement, run a read-only query. A product built on this core adds
its own tools through the same registrar hook, or an extension (see
`akko_mcp_trino.extensions`).

Every tool, the listing ones included, runs in Trino under the identity the
identity mode gives (see `akko_mcp_trino.trino_client`): Trino decides what it
may list and read. Metadata is data.

Every tool call, a plugin's included, leaves one audit line (see
`akko_mcp_trino.audit`): the tool says what it did with ``note(...)`` and the
wrapper adds who, when, how long and with which outcome. The outcome is a
code, never a message: the agent reads Trino's message whole, the audit keeps
its type and name. A plugin receives the same machinery as a `ToolContext`.

A call may have a time budget (``MCP_CALL_TIMEOUT_SECONDS``): every Trino query
it runs draws on it, and so does a plugin's request to its own backend
(``time_allowed``). A call that runs out of it answers an error, never what it
had gathered so far, nor an answer that came past its deadline.
"""

import datetime as dt
import decimal
import functools
import inspect
import json
import time
import uuid
from typing import Any, Callable, Optional, Sequence

from mcp.types import ToolAnnotations

from .audit import (
    AuditEvent,
    bind_call,
    current_request_id,
    error_code,
    note,
    reset_call,
    reset_current_request_id,
    set_current_request_id,
)
from .identity import current_principal, current_subject
from .sql_guard import (
    is_read_only_sql,
    normalize_sql,
    referenced_tables,
    validate_identifier,
)
from .trino_client import TrinoClient

MAX_SAMPLE_ROWS = 20
# The audit code of an error a tool returns without naming it (``note(error=...)``).
UNNAMED_ERROR = "tool_error"

_GOVERNANCE_NOTE = (
    "Results are governed for the current user: values may come back masked "
    "(for example ***@domain) and rows or objects may be missing. That is the "
    "data access policy, not an error; report what is returned, verbatim, and "
    "do not retry the same call."
)


def _error_of(out: Any) -> Optional[str]:
    """The error a tool returned: ``{"error": …}`` as JSON text or as a dict, else None."""
    if isinstance(out, str):
        try:
            out = json.loads(out)
        except ValueError:
            return None
    if isinstance(out, dict) and "error" in out:
        return str(out["error"])
    return None


class _Call:
    """One tool call: request id (the edge's, or a fresh one), record bound with its time
    budget, clock started."""

    def __init__(self, audit: Any, tool: str, budget_seconds: float) -> None:
        self._audit, self._tool, self._budget = audit, tool, budget_seconds

    def __enter__(self) -> "_Call":
        self._start = time.perf_counter()
        self._rid = None if current_request_id() else set_current_request_id(uuid.uuid4().hex)
        self.call, self._token = bind_call(self._tool, self._budget)
        self.ok, self.error = True, ""
        return self

    def returned(self, out: Any) -> Any:
        """What the agent receives: the tool's answer, or an error once the call ran out of
        its budget, whether a query was stopped for it or the answer came past the deadline."""
        if self.call.over():
            late = self.call.timed_out()
            self.ok, self.error = False, error_code(late)
            return dumps(
                {
                    "error": f"{late}; nothing it gathered is returned: narrow the request "
                    "(a catalog, more precise words) or try again"
                }
            )
        if _error_of(out) is not None:
            self.ok, self.error = False, self.call.error or UNNAMED_ERROR
        return out

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc is not None:
            self.ok, self.error = False, error_code(exc)
        try:
            if self._audit is not None:
                principal = current_principal()
                self._audit.record(
                    AuditEvent(
                        request_id=current_request_id() or "",
                        tool=self._tool,
                        subject=principal.subject if principal else "",
                        ok=self.ok,
                        error=self.error,
                        duration_ms=int((time.perf_counter() - self._start) * 1000),
                        call=self.call,
                        principal=principal,
                    )
                )
        finally:
            reset_call(self._token)
            if self._rid is not None:
                reset_current_request_id(self._rid)


def audited(
    audit: Any,
    fn: Callable[..., Any],
    *,
    name: Optional[str] = None,
    budget_seconds: float = 0.0,
) -> Callable:
    """Wrap a tool so that each call is one audited call, due within ``budget_seconds``
    when above 0.

    The call gets a request id (the edge's, or a fresh one outside a request),
    a record the tool fills with ``note(...)``, and, when ``audit`` is given,
    one AuditEvent: outcome from the ``{"error": …}`` the tool returns, with the
    code it noted (``tool_error`` when it named none), or from the exception it
    raises (recorded by its code, then re-raised unchanged). A call that ran out
    of its budget, or answered past its deadline, answers an error instead of what it
    returned. Coroutine tools
    stay coroutines."""
    tool = name or fn.__name__
    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def awrapped(*args, **kwargs):
            with _Call(audit, tool, budget_seconds) as call:
                return call.returned(await fn(*args, **kwargs))

        return awrapped

    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        with _Call(audit, tool, budget_seconds) as call:
            return call.returned(fn(*args, **kwargs))

    return wrapped


def _plain(value: Any) -> Any:
    """What JSON cannot carry, rendered the way SQL would print it.

    Trino returns dates, timestamps, decimals, varbinary and UUIDs as Python
    objects. A date column must not make a whole result unserialisable."""
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return str(value)  # exact digits; a float would silently round money
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    if isinstance(value, uuid.UUID):
        return str(value)
    raise TypeError(f"unserialisable value of type {type(value).__name__}")


def dumps(obj: Any) -> str:
    """JSON for a tool result, with Trino's non-JSON types rendered by ``_plain``."""
    return json.dumps(obj, default=_plain)


def error_reply(exc: Exception) -> str:
    """The error for the agent, whole; for the audit, its code only."""
    note(error=error_code(exc))
    return dumps({"error": str(exc)})


def refusal_reply(code: str, message: str) -> str:
    """A refusal of the tool itself: ``message`` for the agent, ``code`` for the audit."""
    note(error=code)
    return dumps({"error": message})


def was_cut(res: dict) -> bool:
    """Whether the client's row cap (``TRINO_MAX_ROWS``) cut this answer."""
    return bool(res.get("truncated", False))


def listing_reply(key: str, items: list, truncated: bool) -> str:
    """A list for the agent, and whether the row cap cut it; the audit is told the same."""
    note(rows=len(items), truncated=truncated)
    return dumps({key: items, "truncated": truncated})


# A describer adds to what describe_table answers, from what Trino described:
# ``(catalog, schema, table, rows) -> dict``, merged into the answer.
Describer = Callable[[str, str, str, list], dict]


class ToolContext:
    """What a tool plugin receives from the server, besides the MCP server and the client.

    A registrar written ``(mcp, client, tools)`` gets one; a registrar written
    ``(mcp, client)`` keeps working, and its tools are audited all the same.

    * ``tool(title, …)`` registers a tool with MCP annotations and the audit;
    * ``run(sql)`` queries Trino through the identity mode, for the verified caller;
    * ``note(...)`` adds what the call did to its audit record;
    * ``add_describer(fn)`` adds to what ``describe_table`` answers;
    * ``audit`` is the sink, or None; ``read_only`` the server's mode.
    """

    def __init__(
        self,
        mcp: Any,
        client: Any,
        *,
        audit: Any,
        read_only: bool,
        call_timeout_seconds: float = 0.0,
    ) -> None:
        """Bind the server, the client and the shared machinery; every tool registered
        here is due within ``call_timeout_seconds`` when above 0."""
        self._mcp, self._client = mcp, client
        self.audit = audit
        self.read_only = read_only
        self.call_timeout_seconds = call_timeout_seconds
        self.note = note
        self.describers: list[Describer] = []

    def add_describer(self, describer: Describer) -> None:
        """Add ``describer`` to ``describe_table``: what it returns is merged into the
        answer. One that raises never hides the columns."""
        self.describers.append(describer)

    def run(self, sql: str, *, max_rows: int = 0) -> dict:
        """Run ``sql`` in Trino for the verified caller; ``max_rows`` lowers the row cap."""
        # The caller comes from the VERIFIED principal, never from a parameter the
        # agent supplies, which would be forgeable; the identity mode decides what
        # Trino sees of it. A client without a row cap of its own is asked only for
        # what it knows.
        lower = {"max_rows": max_rows} if max_rows > 0 else {}
        return self._client.query(sql, user=current_subject(), **lower)

    def tool(
        self,
        title: str,
        *,
        governed: bool = False,
        idempotent: bool = True,
        read_only: bool = True,
        open_world: bool = False,
    ) -> Callable[[Callable], Callable]:
        """Decorator: register an audited tool with its MCP annotations.

        ``governed`` appends to the description what a masked value or a
        missing row means, so the model reports it instead of retrying.
        """
        register = self._mcp.tool(
            annotations=ToolAnnotations(
                title=title,
                read_only_hint=read_only,
                destructive_hint=False,
                idempotent_hint=idempotent,
                open_world_hint=open_world,
            )
        )

        def deco(fn: Callable) -> Callable:
            if governed:  # the model must know what a masked value means
                fn.__doc__ = (fn.__doc__ or "").rstrip() + "\n\n" + _GOVERNANCE_NOTE + "\n"
            return register(audited(self.audit, fn, budget_seconds=self.call_timeout_seconds))

        return deco


def register_query_tools(
    mcp,
    client: TrinoClient,
    *,
    read_only: bool = True,
    audit: Any = None,
    call_timeout_seconds: float = 0.0,
    session_catalog: str | None = None,
    describe_notes: Sequence[str] = (),
) -> ToolContext:
    """Register the Trino tools on the MCPServer instance `mcp`; return the `ToolContext`
    the server hands to tool plugins.

    `audit`, when given, receives one AuditEvent per call (see `akko_mcp_trino.audit`).
    `call_timeout_seconds`, above 0, is the time budget of each call, all its Trino
    queries included. `session_catalog` is the catalog Trino reads a name without
    catalog in (`TRINO_CATALOG`): the read-only guard refuses such names when it is
    `system`. `describe_notes` are appended to the description of `describe_table`,
    one per describer an extension will add (see `ToolContext.add_describer`)."""
    tools = ToolContext(
        mcp,
        client,
        audit=audit,
        read_only=read_only,
        call_timeout_seconds=call_timeout_seconds,
    )
    # MCP tool annotations: every tool here only reads, none is destructive,
    # none reaches outside the engine. Hosts use them to skip confirmations.
    tool = tools.tool
    run = tools.run

    @tool("List catalogs")
    def list_catalogs() -> str:
        """List the Trino catalogs (data sources) the current user can see.

        Returns JSON {"catalogs": [...], "truncated": bool}. Start here, then
        list_schemas, list_tables and describe_table before writing SQL. Only
        catalogs the user is allowed to see are listed; truncated is true when
        the server's row cap cut the list.
        """
        try:
            res = run("SHOW CATALOGS")
            return listing_reply("catalogs", [r[0] for r in res["rows"]], was_cut(res))
        except Exception as e:  # noqa: BLE001 - surface the error to the agent
            return error_reply(e)

    @tool("List schemas")
    def list_schemas(catalog: str) -> str:
        """List the schemas of a catalog the current user can see.

        Args: catalog, a bare name from list_catalogs (no quotes, no dots).
        Returns JSON {"schemas": [...], "truncated": bool}; truncated is true
        when the server's row cap cut the list.
        """
        try:
            cat = validate_identifier(catalog, "catalog")
            res = run(f"SHOW SCHEMAS FROM {cat}")
            return listing_reply("schemas", [r[0] for r in res["rows"]], was_cut(res))
        except Exception as e:  # noqa: BLE001
            return error_reply(e)

    @tool("List tables")
    def list_tables(catalog: str, schema: str) -> str:
        """List the tables and views of a schema the current user can see.

        Args: catalog and schema, bare names (no quotes, no dots).
        Returns JSON {"tables": [...], "truncated": bool}. Fully qualify them
        as catalog.schema.table in SQL. truncated is true when the server's row
        cap cut the list.
        """
        try:
            cat = validate_identifier(catalog, "catalog")
            sch = validate_identifier(schema, "schema")
            res = run(f"SHOW TABLES FROM {cat}.{sch}")
            return listing_reply("tables", [r[0] for r in res["rows"]], was_cut(res))
        except Exception as e:  # noqa: BLE001
            return error_reply(e)

    def describe_table(catalog: str, schema: str, table: str, sample_rows: int = 0) -> str:
        """Describe a table: its columns, their types and comments, and optionally
        a few sample rows.

        Args: catalog, schema, table as bare names; sample_rows (0 to 20) adds
        that many rows of data. Returns JSON {"columns": [...], "rows": [...],
        "truncated": bool} where each row is [name, type, extra, comment] and
        truncated says the server's row cap cut the columns; plus {"sample": {...}}
        when sample_rows > 0.
        """
        try:
            cat = validate_identifier(catalog, "catalog")
            sch = validate_identifier(schema, "schema")
            tbl = validate_identifier(table, "table")
            fqn = f"{cat}.{sch}.{tbl}"
            note(tables=[fqn])
            described_rows = run(f"DESCRIBE {fqn}")
            out: dict[str, Any] = {
                "columns": described_rows["columns"],
                "rows": described_rows["rows"],
                "truncated": was_cut(described_rows),
            }
            note(rows=len(described_rows["rows"]), truncated=out["truncated"])
            for describer in tools.describers:
                try:
                    out.update(describer(cat, sch, tbl, described_rows["rows"]) or {})
                except Exception:  # noqa: BLE001 - a catalogue down must not hide the columns
                    pass
            n = max(0, min(int(sample_rows), MAX_SAMPLE_ROWS))
            if n:
                # Rows of data are read: the audit line names that statement and its rows.
                statement = f"SELECT * FROM {fqn} LIMIT {n}"
                note(sql=statement)
                out["sample"] = run(statement)
                note(rows=len(out["sample"]["rows"]))
            return dumps(out)
        except Exception as e:  # noqa: BLE001
            return error_reply(e)

    for extra in describe_notes:
        if extra:
            describe_table.__doc__ = (describe_table.__doc__ or "").rstrip() + "\n" + extra + "\n"
    tool("Describe table", governed=True)(describe_table)

    @tool("Explain query")
    def explain_query(sql: str) -> str:
        """Show how Trino would execute a read-only SQL query, without running it.

        Use it to check a query before execute_query and to see which connector
        receives which predicate. Args: sql, one read-only statement, no trailing semicolon needed.
        Returns JSON {"plan": "..."} or {"error": "..."} (a syntax or
        permission error from Trino).
        """
        try:
            sql = normalize_sql(sql)
            if not is_read_only_sql(sql, session_catalog):
                return refusal_reply("read_only", "Only a read-only statement can be explained")
            statement = f"EXPLAIN {sql}"
            note(sql=statement, tables=referenced_tables(sql))
            res = run(statement)
            note(rows=len(res["rows"]), truncated=was_cut(res))
            return dumps({"plan": "\n".join(str(r[0]) for r in res["rows"])})
        except Exception as e:  # noqa: BLE001
            return error_reply(e)

    @tool("Run a read-only query", governed=True)
    def execute_query(sql: str, max_rows: int = 0) -> str:
        """Run one read-only SQL statement on Trino and return the rows.

        Args: sql, Trino SQL with fully qualified tables (catalog.schema.table),
        one statement, no trailing semicolon needed. Only SELECT, SHOW, DESCRIBE
        and EXPLAIN are accepted; INSERT, UPDATE, DELETE, CREATE, DROP and the
        like are refused before reaching Trino, wherever they appear in the
        statement. max_rows, optional, asks for fewer rows than the server's
        cap (never more). Returns JSON {"columns": [...], "rows": [...],
        "row_count": n, "truncated": bool}; truncated is true when more rows
        existed than were returned: add LIMIT, ORDER BY or an aggregate rather
        than reading everything. A query that runs past the server's time
        budget is cancelled. On failure returns {"error": "..."} with Trino's
        message, which is usually enough to fix the SQL.
        """
        sql = normalize_sql(sql)
        if read_only and not is_read_only_sql(sql, session_catalog):
            return refusal_reply(
                "read_only", "Read-only mode: only SELECT/SHOW/DESCRIBE/EXPLAIN queries allowed"
            )
        note(sql=sql, tables=referenced_tables(sql))
        try:
            res = run(sql, max_rows=int(max_rows))
        except Exception as e:  # noqa: BLE001 - surface the error to the agent
            return error_reply(e)
        note(rows=res["row_count"], truncated=bool(res.get("truncated", False)))
        return dumps(res)

    return tools
