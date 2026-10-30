"""akko-mcp-trino: a read-only MCP server for Trino.

Eight tools, writes refused on the SQL tree, every query run as the verified
caller (with a token for Trino obtained by token exchange, or impersonated by
the server's own account) or, when named, as one configured identity; Trino and
its policy engine decide what that identity may read.
"""

__version__ = "0.6.1"
