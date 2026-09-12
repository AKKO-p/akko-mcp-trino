"""The five generic Trino tools, registered on a FastMCP instance through a registrar.

The tools are closures over a `TrinoClient`, so they are testable with a fake
client. Their docstrings are the descriptions MCP hosts show to the model. A
product built on this core adds its own tools through the same registrar hook.
"""

import json

from .identity import current_subject
from .sql_guard import is_read_only_sql, validate_identifier
from .trino_client import TrinoClient


def register_query_tools(mcp, client: TrinoClient, *, read_only: bool = True) -> None:
    """Register the five generic Trino tools on the FastMCP instance `mcp`."""

    @mcp.tool()
    def list_catalogs() -> str:
        """List all available Trino catalogs (data sources)."""
        result = client.query("SHOW CATALOGS")
        return json.dumps([r[0] for r in result["rows"]])

    @mcp.tool()
    def list_schemas(catalog: str) -> str:
        """List schemas in a Trino catalog."""
        cat = validate_identifier(catalog, "catalog")
        result = client.query(f"SHOW SCHEMAS FROM {cat}")
        return json.dumps([r[0] for r in result["rows"]])

    @mcp.tool()
    def list_tables(catalog: str, schema: str) -> str:
        """List tables in a Trino catalog.schema."""
        cat = validate_identifier(catalog, "catalog")
        sch = validate_identifier(schema, "schema")
        result = client.query(f"SHOW TABLES FROM {cat}.{sch}")
        return json.dumps([r[0] for r in result["rows"]])

    @mcp.tool()
    def describe_table(catalog: str, schema: str, table: str) -> str:
        """Describe columns of a Trino table."""
        cat = validate_identifier(catalog, "catalog")
        sch = validate_identifier(schema, "schema")
        tbl = validate_identifier(table, "table")
        result = client.query(f"DESCRIBE {cat}.{sch}.{tbl}")
        return json.dumps({"columns": result["columns"], "rows": result["rows"]})

    @mcp.tool()
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
