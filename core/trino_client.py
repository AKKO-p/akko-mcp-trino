"""Trino client: one connection, bounded execution, identity forwarded per query."""
from __future__ import annotations

import time
from typing import Any, Optional

import trino

from .config import Config


class TrinoClient:
    """Wraps the Trino connection. `query` returns {columns, rows, row_count},
    capped at `config.max_rows`. `user` forwards an identity (X-Trino-User) per
    query; when None, the service account `config.trino_user` is used.
    `metrics`, when given, records queries, errors and latency."""

    def __init__(self, config: Config, metrics: Any = None) -> None:
        self._config = config
        self._metrics = metrics

    def _connect(self, user: Optional[str] = None):
        return trino.dbapi.connect(
            host=self._config.trino_host,
            port=self._config.trino_port,
            user=user or self._config.trino_user,
            catalog=self._config.trino_catalog,
        )

    def _run(self, sql: str, user: Optional[str], params: Any) -> dict:
        conn = self._connect(user)
        cursor = conn.cursor()
        cursor.execute(sql, params)
        rows = cursor.fetchmany(self._config.max_rows)
        columns = [d[0] for d in cursor.description] if cursor.description else []
        return {"columns": columns, "rows": [list(r) for r in rows], "row_count": len(rows)}

    def query(self, sql: str, user: Optional[str] = None, params: Any = None) -> dict:
        if self._metrics is None:
            return self._run(sql, user, params)
        self._metrics.queries.inc()
        start = time.perf_counter()
        try:
            return self._run(sql, user, params)
        except Exception:
            self._metrics.errors.inc()
            raise
        finally:
            self._metrics.duration.observe(time.perf_counter() - start)
