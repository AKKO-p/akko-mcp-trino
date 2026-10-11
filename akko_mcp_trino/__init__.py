"""akko-mcp-trino: a read-only MCP server for Trino.

Six tools, writes refused on the SQL tree, every query run as the one Trino
identity the administrator configured; Trino and its policy engine decide what
that identity may read.
"""

__version__ = "0.5.2"
