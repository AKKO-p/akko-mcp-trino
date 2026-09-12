"""Outils MCP Trino GÉNÉRIQUES — enregistrés dans un FastMCP via un registre.

P1 refactor pur : mêmes noms, signatures et docstrings (= description MCP) que
server.py. Les outils sont des closures sur un `TrinoClient` → testables avec un
client factice. La couche AKKO enregistre ses outils en plus via le même mécanisme.
"""

import json

from .identity import current_subject
from .sql_guard import is_read_only_sql, validate_identifier
from .trino_client import TrinoClient


def register_query_tools(mcp, client: TrinoClient, *, read_only: bool = True) -> None:
    """Enregistre les 5 outils Trino génériques sur l'instance FastMCP `mcp`."""

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
        # L'identité vient du Principal AUTHENTIFIÉ (X-Trino-User), pas d'un paramètre
        # fourni par l'agent (anti-usurpation). None → repli compte de service.
        if read_only and not is_read_only_sql(sql):
            return json.dumps({"error": "Read-only mode: only SELECT/SHOW/DESCRIBE/EXPLAIN queries allowed"})
        try:
            return json.dumps(client.query(sql, user=current_subject()))
        except Exception as e:  # noqa: BLE001 — renvoyer l'erreur à l'agent, comme avant
            return json.dumps({"error": str(e)})
