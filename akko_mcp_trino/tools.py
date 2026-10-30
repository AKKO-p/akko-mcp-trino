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
from .errors import INVALID_ARGUMENT, READ_ONLY, TIME_BUDGET, explain_error
from .identity import current_principal, current_subject
from .sql_guard import (
    governed_catalog,
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
                    "(a catalog, more precise words) or try again",
                    "kind": TIME_BUDGET,
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
    """The error for the agent, whole, with its kind (see ``akko_mcp_trino.errors``); for the
    audit, its code only."""
    note(error=error_code(exc))
    return dumps(explain_error(exc))


def refusal_reply(code: str, message: str, kind: str = INVALID_ARGUMENT) -> str:
    """A refusal of the tool itself: ``message`` and ``kind`` for the agent, ``code`` for
    the audit."""
    note(error=code)
    return dumps({"error": message, "kind": kind})


def was_cut(res: dict) -> bool:
    """Whether the client's row cap (``TRINO_MAX_ROWS``) cut this answer."""
    return bool(res.get("truncated", False))


def listing_reply(key: str, items: list, next_offset: Optional[int]) -> str:
    """One page of a list for the agent: the ``offset`` of the next page, or None on the last
    one, and ``truncated``, true exactly when there is a next page; the audit is told the same.

    Before 0.7 the third argument was ``truncated``: a bool is refused, so a plugin written
    for that never answers that a complete list was cut, or the reverse."""
    if isinstance(next_offset, bool):
        raise TypeError("listing_reply takes the next page's offset, or None, not a bool")
    truncated = next_offset is not None
    note(rows=len(items), truncated=truncated)
    return dumps({key: items, "next_offset": next_offset, "truncated": truncated})


def check_offset(offset: int) -> int:
    """``offset`` as the start of a page: a whole number from 0, or raise ValueError."""
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError(f"Invalid offset: {offset!r}. An offset is a whole number from 0.")
    return offset


def _next_offset(offset: int, items: list, more: bool) -> Optional[int]:
    """Where the page after ``items``, read from ``offset``, starts; None when none follows."""
    return offset + len(items) if more else None


# The description of execute_query, written for a model; ``rule`` says whether it may write.
_EXECUTE_DOC = """Run {what} on Trino and return the rows.

        Args: sql, Trino SQL with fully qualified tables (catalog.schema.table),
        one statement, no trailing semicolon needed. {rule} max_rows, optional,
        asks for fewer rows than the server's cap (never more). Returns JSON
        {{"columns": [...], "rows": [...], "row_count": n, "truncated": bool}};
        truncated is true when more rows existed than were returned: add LIMIT,
        ORDER BY or an aggregate rather than reading everything. A query that
        runs past the server's time budget is cancelled. On failure returns
        {{"error": "...", "kind": "..."}} with Trino's message, which is usually enough
        to fix the SQL; kind says what to fix (syntax, column, table, schema, catalog,
        permission, function, type, invalid_argument, read_only, time_budget, other),
        and "unavailable" means Trino did not answer.
        """
_READ_ONLY_RULE = (
    "Only SELECT, SHOW, DESCRIBE and EXPLAIN are accepted; INSERT, UPDATE, DELETE, "
    "CREATE, DROP and the like are refused before reaching Trino, wherever they "
    "appear in the statement."
)
_WRITE_RULE = (
    "The statement may write (INSERT, UPDATE, DELETE, CREATE, DROP and the like), "
    "as far as Trino's access control allows the identity it runs as."
)

# A describer adds to what describe_table answers, from what Trino described:
# ``(catalog, schema, table, rows) -> dict``, merged into the answer.
Describer = Callable[[str, str, str, list], dict]


class ToolContext:
    """What a tool plugin receives from the server, besides the MCP server and the client.

    A registrar written ``(mcp, client, tools)`` gets one; a registrar written
    ``(mcp, client)`` keeps working, and its tools are audited all the same.

    * ``tool(title, …)`` registers a tool with MCP annotations and the audit;
    * ``run(sql)`` queries Trino through the identity mode, for the verified caller;
    * ``run_whole(sql, bound=…, what=…)`` reads every row of a statement the tool builds
      itself, past the row cap, up to ``bound`` rows;
    * ``page_size`` is the size of one page of a list, the server's row cap;
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
        page_size: int = 100,
    ) -> None:
        """Bind the server, the client and the shared machinery; every tool registered
        here is due within ``call_timeout_seconds`` when above 0, and a page of a list
        holds ``page_size`` entries (``TRINO_MAX_ROWS``)."""
        self._mcp, self._client = mcp, client
        self.audit = audit
        self.read_only = read_only
        self.call_timeout_seconds = call_timeout_seconds
        self.page_size = page_size
        self.note = note
        self.describers: list[Describer] = []

    def add_describer(self, describer: Describer) -> None:
        """Add ``describer`` to ``describe_table``: what it returns is merged into the
        answer. One that raises never hides the columns."""
        self.describers.append(describer)

    def run(self, sql: str, *, max_rows: int = 0, row_limit: int = 0, timeout: float = 0.0) -> dict:
        """Run ``sql`` in Trino for the verified caller; ``max_rows`` lowers the row cap,
        ``row_limit`` replaces it (for a statement the tool builds, never the agent's SQL),
        ``timeout`` lowers the query's time budget (see ``TrinoClient.query``)."""
        # The caller comes from the VERIFIED principal, never from a parameter the
        # agent supplies, which would be forgeable; the identity mode decides what
        # Trino sees of it. A client that knows only ``query(sql, user, params)`` is
        # asked only for what it knows: a keyword left at 0 is not passed.
        given = {"max_rows": max_rows, "row_limit": row_limit, "timeout": timeout}
        keywords = {name: value for name, value in given.items() if value > 0}
        return self._client.query(sql, user=current_subject(), **keywords)

    def run_whole(self, sql: str, *, bound: int, what: str) -> list:
        """Every row of ``sql``, read past the row cap up to ``bound`` rows; more than
        ``bound`` raises ValueError naming ``what``: never a list cut in silence."""
        res = self.run(sql, row_limit=bound)
        if was_cut(res):
            raise ValueError(f"more than {bound} {what} are visible; raise the server's limit")
        return res["rows"]

    def tool(
        self,
        title: str,
        *,
        governed: bool = False,
        idempotent: bool = True,
        read_only: bool = True,
        open_world: bool = False,
        destructive: bool = False,
    ) -> Callable[[Callable], Callable]:
        """Decorator: register an audited tool with its MCP annotations.

        ``governed`` appends to the description what a masked value or a
        missing row means, so the model reports it instead of retrying.
        ``read_only`` and ``destructive`` must say what the tool can really do:
        a host skips its confirmation on a tool annotated read-only.
        """
        register = self._mcp.tool(
            annotations=ToolAnnotations(
                title=title,
                read_only_hint=read_only,
                destructive_hint=destructive,
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
    max_rows: int = 100,
    max_catalogs: int = 10000,
) -> ToolContext:
    """Register the Trino tools on the MCPServer instance `mcp`; return the `ToolContext`
    the server hands to tool plugins.

    `audit`, when given, receives one AuditEvent per call (see `akko_mcp_trino.audit`).
    `call_timeout_seconds`, above 0, is the time budget of each call, all its Trino
    queries included. `session_catalog` is the catalog Trino reads a name without
    catalog in (`TRINO_CATALOG`): the read-only guard refuses such names when it is
    `system`. `describe_notes` are appended to the description of `describe_table`,
    one per describer an extension will add (see `ToolContext.add_describer`).
    `max_rows` is the server's row cap (`TRINO_MAX_ROWS`), the size of one page of a
    list; `max_catalogs` bounds what `list_catalogs` reads (`MCP_MAX_CATALOGS`)."""
    tools = ToolContext(
        mcp,
        client,
        audit=audit,
        read_only=read_only,
        call_timeout_seconds=call_timeout_seconds,
        page_size=max_rows,
    )
    # MCP tool annotations: the listing, describing and explaining tools only read,
    # whatever the settings; execute_query reads only while TRINO_READ_ONLY is true,
    # and is annotated as it is. None reaches outside the engine. Hosts use the
    # annotations to skip confirmations, so they must never claim less than the tool does.
    tool = tools.tool
    run = tools.run

    @tool("List catalogs")
    def list_catalogs(offset: int = 0) -> str:
        """List the Trino catalogs (data sources) the current user can see, one page at a time.

        Args: offset, optional, where the page starts (0, the default, is the first page).
        Returns JSON {"catalogs": [...], "next_offset": n or null, "truncated": bool},
        sorted by name. When next_offset is not null, more catalogs follow: call again
        with offset=next_offset for the next page; truncated is true exactly then.
        Start here, then list_schemas, list_tables and describe_table before writing
        SQL. Only catalogs the user is allowed to see are listed, never the engine's
        own system catalog.
        """
        try:
            start = check_offset(offset)
            # SHOW CATALOGS works under a policy that refuses all of `system`: it is read
            # whole, up to its own bound, and paged here.
            rows = tools.run_whole("SHOW CATALOGS", bound=max_catalogs, what="catalogs")
            names = sorted(str(r[0]) for r in rows if str(r[0]).lower() != "system")
            page = names[start : start + tools.page_size]
            more = start + len(page) < len(names)
            return listing_reply("catalogs", page, _next_offset(start, page, more))
        except Exception as e:  # noqa: BLE001 - surface the error to the agent
            return error_reply(e)

    @tool("List schemas")
    def list_schemas(catalog: str, offset: int = 0) -> str:
        """List the schemas of a catalog the current user can see, one page at a time.

        Args: catalog, a bare name from list_catalogs (no quotes, no dots); offset,
        optional, where the page starts (0, the default, is the first page).
        Returns JSON {"schemas": [...], "next_offset": n or null, "truncated": bool},
        sorted by name. When next_offset is not null, more schemas follow: call again
        with offset=next_offset for the next page; truncated is true exactly then.
        """
        try:
            cat = governed_catalog(catalog)
            start = check_offset(offset)
            res = run(
                f"SELECT schema_name FROM {cat}.information_schema.schemata "
                f"ORDER BY schema_name OFFSET {start}"
            )
            names = [r[0] for r in res["rows"]]
            return listing_reply("schemas", names, _next_offset(start, names, was_cut(res)))
        except Exception as e:  # noqa: BLE001
            return error_reply(e)

    @tool("List tables")
    def list_tables(catalog: str, schema: str, offset: int = 0) -> str:
        """List the tables and views of a schema the current user can see, one page at a time.

        Args: catalog and schema, bare names (no quotes, no dots); offset, optional,
        where the page starts (0, the default, is the first page).
        Returns JSON {"tables": [...], "next_offset": n or null, "truncated": bool},
        sorted by name. When next_offset is not null, more tables follow: call again
        with offset=next_offset for the next page; truncated is true exactly then.
        Fully qualify them as catalog.schema.table in SQL.
        """
        try:
            cat = governed_catalog(catalog)
            sch = validate_identifier(schema, "schema")
            start = check_offset(offset)
            # A validated name carries no quote. Trino folds an unquoted name to lower
            # case, as SHOW TABLES FROM did: the schema is compared folded the same way.
            res = run(
                f"SELECT table_name FROM {cat}.information_schema.tables "
                f"WHERE table_schema = '{sch.lower()}' ORDER BY table_name OFFSET {start}"
            )
            names = [r[0] for r in res["rows"]]
            return listing_reply("tables", names, _next_offset(start, names, was_cut(res)))
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
            cat = governed_catalog(catalog)
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
        Returns JSON {"plan": "..."} or {"error": "...", "kind": "..."} (a syntax or
        permission error from Trino); kind says what to fix (syntax, column, table,
        schema, catalog, permission, function, type), and "unavailable" means Trino
        did not answer, so the query is neither valid nor invalid.
        """
        try:
            sql = normalize_sql(sql)
            if not is_read_only_sql(sql, session_catalog):
                return refusal_reply(
                    "read_only", "Only a read-only statement can be explained", kind=READ_ONLY
                )
            statement = f"EXPLAIN {sql}"
            note(sql=statement, tables=referenced_tables(sql))
            res = run(statement)
            note(rows=len(res["rows"]), truncated=was_cut(res))
            return dumps({"plan": "\n".join(str(r[0]) for r in res["rows"])})
        except Exception as e:  # noqa: BLE001
            return error_reply(e)

    def execute_query(sql: str, max_rows: int = 0) -> str:
        sql = normalize_sql(sql)
        if read_only and not is_read_only_sql(sql, session_catalog):
            return refusal_reply(
                "read_only",
                "Read-only mode: only SELECT/SHOW/DESCRIBE/EXPLAIN queries allowed",
                kind=READ_ONLY,
            )
        note(sql=sql, tables=referenced_tables(sql))
        try:
            res = run(sql, max_rows=int(max_rows))
        except Exception as e:  # noqa: BLE001 - surface the error to the agent
            return error_reply(e)
        note(rows=res["row_count"], truncated=bool(res.get("truncated", False)))
        return dumps(res)

    execute_query.__doc__ = _EXECUTE_DOC.format(
        what="one read-only SQL statement" if read_only else "one SQL statement",
        rule=_READ_ONLY_RULE if read_only else _WRITE_RULE,
    )
    if read_only:
        tool("Run a read-only query", governed=True)(execute_query)
    else:
        # TRINO_READ_ONLY=false: the statement may write, and the tool says it may.
        tool(
            "Run a query (writes allowed)",
            governed=True,
            read_only=False,
            destructive=True,
            idempotent=False,
        )(execute_query)
    return tools
