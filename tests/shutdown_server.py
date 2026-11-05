"""The real server process, run by ``test_shutdown`` with one context provider.

The provider ``marker`` appends one line to the file ``SHUTDOWN_MARKER`` names each
time it is closed, so the test sees whether, and how often, a stop closed the
context. ``SHUTDOWN_READY`` names a file written once the server is about to serve
over stdio. Trino is never reached.
"""

from __future__ import annotations

import os
import sys

from akko_mcp_trino import __main__ as entry
from akko_mcp_trino.context import CONTEXT_API, loading


class _Marker:
    name = "marker"
    context_api = CONTEXT_API

    def maps(self, catalog):
        return False

    def served_sets(self):
        return {}

    async def describe(self, refs, *, deadline, on_behalf_of=None):
        return {}

    async def terms(self, keys, *, deadline):
        return {}

    def check_lines(self):
        return []

    async def aclose(self):
        with open(os.environ["SHUTDOWN_MARKER"], "a", encoding="utf-8") as out:
            out.write("closed\n")

    def credentials_stamp(self):
        return ""


class _EntryPoint:
    name = "marker"

    def load(self):
        return lambda env: _Marker()


loading._entry_points = lambda: [_EntryPoint()]

_serve_stdio = entry.run_stdio


def _ready_then_serve(*args, **kwargs):
    with open(os.environ["SHUTDOWN_READY"], "w", encoding="utf-8") as out:
        out.write("ready\n")
    return _serve_stdio(*args, **kwargs)


entry.run_stdio = _ready_then_serve

if __name__ == "__main__":
    sys.argv = ["akko-mcp-trino"]
    entry.main()
