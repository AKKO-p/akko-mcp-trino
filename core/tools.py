"""The five generic Trino tools, registered on a FastMCP instance through a registrar.

The tools are closures over a `TrinoClient`, so they are testable with a fake
client. Their docstrings are the descriptions MCP hosts show to the model. A
product built on this core adds its own tools through the same registrar hook.
"""

import functools
import json
from typing import Any, Callable

from .agents import current_agent
from .audit import AuditEvent, current_request_id
from .identity import current_principal, current_subject
from .sql_guard import is_read_only_sql, validate_identifier
from .trino_client import TrinoClient


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
            audit.record(AuditEvent(
                request_id=current_request_id() or "",
                tool=fn.__name__,
                subject=principal.subject if principal else "",
                agent=current_agent() or "",
                token_id=principal.token_id if principal else "",
                ok=ok,
                error=error,
            ))

    return wrapped


def register_query_tools(
    mcp, client: TrinoClient, *, read_only: bool = True, audit: Any = None
) -> None:
    """Register the five generic Trino tools on the FastMCP instance `mcp`.

    `audit`, when given, receives one AuditEvent per call (see `core.audit`)."""

    def tool():
        register = mcp.tool()

        def deco(fn):
            return register(_audited(audit, fn))
        return deco

    @tool()
    def list_catalogs() -> str:
        """List all available Trino catalogs (data sources)."""
        result = client.query("SHOW CATALOGS")
        return json.dumps([r[0] for r in result["rows"]])

    @tool()
    def list_schemas(catalog: str) -> str:
        """List schemas in a Trino catalog."""
        cat = validate_identifier(catalog, "catalog")
        result = client.query(f"SHOW SCHEMAS FROM {cat}")
        return json.dumps([r[0] for r in result["rows"]])

    @tool()
    def list_tables(catalog: str, schema: str) -> str:
        """List tables in a Trino catalog.schema."""
        cat = validate_identifier(catalog, "catalog")
        sch = validate_identifier(schema, "schema")
        result = client.query(f"SHOW TABLES FROM {cat}.{sch}")
        return json.dumps([r[0] for r in result["rows"]])

    @tool()
    def describe_table(catalog: str, schema: str, table: str) -> str:
        """Describe columns of a Trino table."""
        cat = validate_identifier(catalog, "catalog")
        sch = validate_identifier(schema, "schema")
        tbl = validate_identifier(table, "table")
        result = client.query(f"DESCRIBE {cat}.{sch}.{tbl}")
        return json.dumps({"columns": result["columns"], "rows": result["rows"]})

    @tool()
    def execute_query(sql: str) -> str:
        """Execute a SQL query on Trino and return results (max 100 rows).

        In read-only mode (default), only SELECT/SHOW/DESCRIBE/EXPLAIN are allowed.
        """
        # Identity comes from the VERIFIED Principal (X-Trino-User), never from a
        # parameter the agent supplies — that would be forgeable. None falls back
        # to the service account.
        if read_only and not is_read_only_sql(sql):
            return json.dumps({"error": "Read-only mode: only SELECT/SHOW/DESCRIBE/EXPLAIN queries allowed"})
        try:
            return json.dumps(client.query(sql, user=current_subject()))
        except Exception as e:  # noqa: BLE001 - surface the error to the agent
            return json.dumps({"error": str(e)})
