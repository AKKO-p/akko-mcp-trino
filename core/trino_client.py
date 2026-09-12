"""Client Trino — vendor-neutre. Connexion + exécution bornée (P1 : à l'identique)."""
from __future__ import annotations

import time
from typing import Any, Optional

import trino

from .config import Config


class TrinoClient:
    """Encapsule la connexion Trino. `query` renvoie {columns, rows, row_count},
    borné à `config.max_rows`. `user` permet la propagation d'identité (X-Trino-User)
    — P1 garde le défaut = compte de service de la config (P2 câblera l'identité réelle).
    `metrics` (optionnel) instrumente requêtes/erreurs/latence (Prometheus)."""

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
