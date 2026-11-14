"""The real server process with Trino faked at the driver, run by ``test_parse_process``.

Every query answers one row ``(1,)``. The parse pool the server builds writes the process
ids of its workers to the file ``PARSE_PIDS`` names after each statement; with
``PARSE_WARM=1`` it starts them at once, before serving, so a test can stop the server
with its workers running. Nothing is written to stdout, the protocol channel over stdio.
"""

from __future__ import annotations

import os
import threading

import trino.dbapi

from akko_mcp_trino import __main__ as entry
from akko_mcp_trino import parse_pool, server


class _Cursor:
    description, query_id = [("n",)], "q"

    def execute(self, sql, params=None):
        pass

    def fetchmany(self, n):
        return [(1,)][:n]

    def cancel(self):
        pass


trino.dbapi.connect = lambda **kw: type("C", (), {"cursor": lambda self: _Cursor()})()


def _write_pids(pool):
    with open(os.environ["PARSE_PIDS"], "w", encoding="utf-8") as out:
        out.write(" ".join(str(pid) for pid in pool.pids()))


class _Pool(parse_pool.ParsePool):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if os.environ.get("PARSE_WARM") == "1":
            threading.Thread(target=self._warm, daemon=True).start()

    def _warm(self):
        self.analyze("SELECT 1", None, frozenset(), "warm")

    def analyze(self, *args, **kwargs):
        try:
            return super().analyze(*args, **kwargs)
        finally:
            _write_pids(self)


server.ParsePool = _Pool

if __name__ == "__main__":
    import sys

    sys.argv = ["akko-mcp-trino"]
    entry.main()
