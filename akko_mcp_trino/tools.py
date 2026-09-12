"""The Trino tools, registered on a FastMCP instance through a registrar.

The tools are closures over a `TrinoClient`, so they are testable with a fake
client. Their docstrings are the descriptions MCP hosts show to the model, so
they are written for a model: what the tool is for, what it returns, and what
a masked value or a missing row means. A product built on this core adds its
own tools through the same registrar hook.

Every tool, discovery included, runs in Trino under the caller's identity.
Metadata is data: a user who may not read a schema must not list it either.
"""

import datetime as dt
import decimal
import functools
import json
import uuid
from typing import Any, Callable

from mcp.types import ToolAnnotations

from .agents import current_agent
from .audit import AuditEvent, current_request_id
from .context import NoContext
from .identity import current_bearer, current_principal, current_subject
from .sql_guard import is_read_only_sql, normalize_sql, safe_sql_string, validate_identifier
from .trino_client import TrinoClient

MAX_SAMPLE_ROWS = 20

_GOVERNANCE_NOTE = (
    "Results are governed for the current user: values may come back masked "
    "(for example ***@domain) and rows or objects may be missing. That is the "
    "data access policy, not an error; report what is returned, verbatim, and "
    "do not retry the same call."
)


def _audited(audit: Any, fn: Callable[..., str]) -> Callable[..., str]:
    """Record one AuditEvent per call: outcome from the JSON the tool returns
    (`{"error": …}` is a failure) or from the exception it raises."""
    if audit is None:
        return fn

    @functools.wraps(fn)
    def wrapped(*args, **kwargs) -> str:
        ok, error = True, ""
        try:
            out = fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - recorded, then re-raised unchanged
            ok, error = False, str(exc)
            raise
        else:
            parsed = json.loads(out)
            if isinstance(parsed, dict) and "error" in parsed:
                ok, error = False, str(parsed["error"])
            return out
        finally:
            principal = current_principal()
            audit.record(
                AuditEvent(
                    request_id=current_request_id() or "",
                    tool=fn.__name__,
                    subject=principal.subject if principal else "",
                    agent=current_agent() or "",
                    token_id=principal.token_id if principal else "",
                    ok=ok,
                    error=error,
                )
            )

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


def _error(exc: Exception) -> str:
    return dumps({"error": str(exc)})


def register_query_tools(
    mcp,
    client: TrinoClient,
    *,
    read_only: bool = True,
    audit: Any = None,
    context: Any = None,
) -> None:
    """Register the Trino tools on the FastMCP instance `mcp`.

    `audit`, when given, receives one AuditEvent per call (see `akko_mcp_trino.audit`).
    `context`, when given, is a ContextProvider (see `akko_mcp_trino.context`) that
    enriches `describe_table` and answers `explain_table`; a provider that fails
    never breaks a tool, the agent simply gets Trino alone."""
    context = context if context is not None else NoContext()

    def tool(title: str, governed: bool = False, idempotent: bool = True):
        # MCP tool annotations: every tool here only reads, none is destructive,
        # none reaches outside the engine. Hosts use them to skip confirmations.
        register = mcp.tool(
            annotations=ToolAnnotations(
                title=title,
                readOnlyHint=True,
                destructiveHint=False,
                idempotentHint=idempotent,
                openWorldHint=False,
            )
        )

        def deco(fn):
            if governed:  # the model must know what a masked value means
                fn.__doc__ = (fn.__doc__ or "").rstrip() + "\n\n" + _GOVERNANCE_NOTE + "\n"
            return register(_audited(audit, fn))

        return deco

    def run(sql: str) -> dict:
        # Identity comes from the VERIFIED Principal (X-Trino-User), never from a
        # parameter the agent supplies — that would be forgeable. None falls back
        # to the service account (auth disabled).
        return client.query(sql, user=current_subject(), bearer=current_bearer())

    @tool("List catalogs")
    def list_catalogs() -> str:
        """List the Trino catalogs (data sources) the current user can see.

        Returns a JSON array of catalog names. Start here, then list_schemas,
        list_tables and describe_table before writing SQL. Only catalogs the
        user is allowed to see are listed.
        """
        try:
            return dumps([r[0] for r in run("SHOW CATALOGS")["rows"]])
        except Exception as e:  # noqa: BLE001 - surface the error to the agent
            return _error(e)

    @tool("List schemas")
    def list_schemas(catalog: str) -> str:
        """List the schemas of a catalog the current user can see.

        Args: catalog, a bare name from list_catalogs (no quotes, no dots).
        Returns a JSON array of schema names.
        """
        try:
            cat = validate_identifier(catalog, "catalog")
            return dumps([r[0] for r in run(f"SHOW SCHEMAS FROM {cat}")["rows"]])
        except Exception as e:  # noqa: BLE001
            return _error(e)

    @tool("List tables")
    def list_tables(catalog: str, schema: str) -> str:
        """List the tables and views of a schema the current user can see.

        Args: catalog and schema, bare names (no quotes, no dots).
        Returns a JSON array of table names. Fully qualify them as
        catalog.schema.table in SQL.
        """
        try:
            cat = validate_identifier(catalog, "catalog")
            sch = validate_identifier(schema, "schema")
            return dumps([r[0] for r in run(f"SHOW TABLES FROM {cat}.{sch}")["rows"]])
        except Exception as e:  # noqa: BLE001
            return _error(e)

    @tool("Describe table", governed=True)
    def describe_table(catalog: str, schema: str, table: str, sample_rows: int = 0) -> str:
        """Describe a table: its columns, their types and comments, and optionally
        a few sample rows.

        Args: catalog, schema, table as bare names; sample_rows (0 to 20) adds
        that many rows of data. Returns JSON {"columns": [...], "rows": [...]}
        where each row is [name, type, extra, comment]; plus "table" (description,
        owner, tier, grain, joins, tags) and "column_context" (description,
        classification such as PII, known values) when the platform's catalogue
        knows the table; plus {"sample": {...}} when sample_rows > 0.
        """
        try:
            cat = validate_identifier(catalog, "catalog")
            sch = validate_identifier(schema, "schema")
            tbl = validate_identifier(table, "table")
            described = run(f"DESCRIBE {cat}.{sch}.{tbl}")
            out: dict[str, Any] = {"columns": described["columns"], "rows": described["rows"]}
            try:
                table_ctx = context.table(cat, sch, tbl)
                if table_ctx is not None:
                    out["table"] = table_ctx.as_dict()
                column_ctx = {}
                for row in described["rows"]:
                    col = str(row[0])
                    c = context.column(cat, sch, tbl, col)
                    if c is not None:
                        column_ctx[col] = c.as_dict()
                if column_ctx:
                    out["column_context"] = column_ctx
            except Exception:  # noqa: BLE001 - a catalogue down must not hide the columns
                pass
            n = max(0, min(int(sample_rows), MAX_SAMPLE_ROWS))
            if n:
                out["sample"] = run(f"SELECT * FROM {cat}.{sch}.{tbl} LIMIT {n}")
            return dumps(out)
        except Exception as e:  # noqa: BLE001
            return _error(e)

    @tool("Search columns", governed=True)
    def search_columns(pattern: str, catalog: str = "") -> str:
        """Find tables that have a column whose name matches a pattern.

        Args: pattern, a SQL LIKE pattern on the column name, case-insensitive
        (use % as wildcard, e.g. "%email%"); catalog, optional bare name to
        search a single catalog (much faster). Without a catalog, every catalog
        the user can see is searched. Returns a JSON array of
        {"catalog", "schema", "table", "column", "type"}.
        """
        try:
            like = safe_sql_string(pattern.strip().lower())
            if not like:
                return dumps({"error": "pattern is required, for example '%email%'"})
            catalogs = (
                [validate_identifier(catalog, "catalog")]
                if catalog
                else [r[0] for r in run("SHOW CATALOGS")["rows"]]
            )
            found = []
            for cat in catalogs:
                res = run(
                    f"SELECT table_schema, table_name, column_name, data_type "
                    f"FROM {cat}.information_schema.columns "
                    f"WHERE lower(column_name) LIKE '{like}' "
                    f"AND table_schema <> 'information_schema'"
                )
                found += [
                    {"catalog": cat, "schema": r[0], "table": r[1], "column": r[2], "type": r[3]}
                    for r in res["rows"]
                ]
            return dumps(found)
        except Exception as e:  # noqa: BLE001
            return _error(e)

    @tool("Profile table", governed=True)
    def profile_table(catalog: str, schema: str, table: str) -> str:
        """Profile a table with the statistics Trino keeps: row count, and per
        column the data size, number of distinct values, fraction of nulls,
        low and high values.

        Args: catalog, schema, table as bare names. Returns JSON
        {"columns": [...], "rows": [...]} from SHOW STATS; the row whose
        column_name is null carries the table row count. Statistics may be
        absent (null) when the connector does not collect them.
        """
        try:
            cat = validate_identifier(catalog, "catalog")
            sch = validate_identifier(schema, "schema")
            tbl = validate_identifier(table, "table")
            return dumps(run(f"SHOW STATS FOR {cat}.{sch}.{tbl}"))
        except Exception as e:  # noqa: BLE001
            return _error(e)

    @tool("Explain table")
    def explain_table(catalog: str, schema: str, table: str) -> str:
        """What a table means, beyond its columns: description, owner, tier
        (how trustworthy it is), grain (what makes a row unique), joins (which
        columns lead to which table) and tags. Comes from the platform's
        catalogue or from a context file, when one is configured.

        Args: catalog, schema, table as bare names. Returns JSON with the known
        fields, or {"known": false} when nothing is known about this table.
        """
        try:
            cat = validate_identifier(catalog, "catalog")
            sch = validate_identifier(schema, "schema")
            tbl = validate_identifier(table, "table")
            known = context.table(cat, sch, tbl)
            return dumps(known.as_dict() if known is not None else {"known": False})
        except Exception as e:  # noqa: BLE001
            return _error(e)

    @tool("Explain query")
    def explain_query(sql: str) -> str:
        """Show how Trino would execute a read-only SQL query, without running it.

        Use it to validate a query before execute_query, or to see which
        connector receives which predicate. Args: sql, one read-only statement,
        no trailing semicolon needed. Returns JSON {"plan": "..."} or
        {"error": "..."} (a syntax or permission error from Trino).
        """
        try:
            sql = normalize_sql(sql)
            if not is_read_only_sql(sql):
                return dumps({"error": "Only a read-only statement can be explained"})
            res = run(f"EXPLAIN {sql}")
            return dumps({"plan": "\n".join(str(r[0]) for r in res["rows"])})
        except Exception as e:  # noqa: BLE001
            return _error(e)

    @tool("Run a read-only query", governed=True)
    def execute_query(sql: str) -> str:
        """Run one read-only SQL statement on Trino and return the rows.

        Args: sql, Trino SQL with fully qualified tables (catalog.schema.table),
        one statement, no trailing semicolon needed. Only SELECT, SHOW, DESCRIBE
        and EXPLAIN are accepted; INSERT, UPDATE, DELETE, CREATE, DROP and the
        like are refused before reaching Trino, wherever they appear in the
        statement. Returns JSON {"columns": [...], "rows": [...], "row_count": n},
        capped at the server's row limit; add LIMIT and ORDER BY yourself for
        large tables. On failure returns {"error": "..."} with Trino's message,
        which is usually enough to fix the SQL.
        """
        sql = normalize_sql(sql)
        if read_only and not is_read_only_sql(sql):
            return dumps(
                {"error": "Read-only mode: only SELECT/SHOW/DESCRIBE/EXPLAIN queries allowed"}
            )
        try:
            return dumps(run(sql))
        except Exception as e:  # noqa: BLE001 - surface the error to the agent
            return _error(e)
