"""akko-mcp-trino: a read-only MCP server for Trino.

Nine tools, writes refused on the SQL tree, every query run as the verified
caller (with a token for Trino obtained by token exchange, or impersonated by
the server's own account) or, when named, as one configured identity; Trino and
its policy engine decide what that identity may read.
"""

import os as _os

from .otel import drop_missing_tracer_provider as _drop_missing_tracer_provider

__version__ = "0.8.7"

# Before anything imports the MCP SDK, whose first import loads the tracer provider that
# OTEL_PYTHON_TRACER_PROVIDER names, and fails when none is installed (see `otel`).
OTEL_DROPPED = _drop_missing_tracer_provider(_os.environ)
