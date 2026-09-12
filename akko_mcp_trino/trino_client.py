"""Trino client: one connection per query, bounded execution, identity forwarded.

Two identity modes, chosen by ``TRINO_IDENTITY_MODE``:

* ``impersonate`` — the server authenticates as ``TRINO_USER`` (Basic over
  https when ``TRINO_PASSWORD`` is set) and sets ``X-Trino-User`` to the
  caller. Trino's impersonation rules decide whether that is allowed.
* ``jwt`` — the caller's own bearer, already verified by the guard, is sent to
  Trino as its JWT. Trino verifies it again with its JWT authenticator and
  runs the query as that user. No impersonation right, no service password.

Both fail closed: a password never travels over plain http, and the ``jwt``
mode never falls back to the service account when a request has no bearer.
"""

from __future__ import annotations

import time
from typing import Any, Optional

import trino
import trino.auth

from .config import Config

_IDENTITY_MODES = ("impersonate", "jwt")


class TrinoClient:
    """Wraps the Trino connection. `query` returns {columns, rows, row_count},
    capped at `config.max_rows`. `user` forwards an identity (X-Trino-User) per
    query; when None, the service account `config.trino_user` is used.
    `metrics`, when given, records queries, errors and latency."""

    def __init__(self, config: Config, metrics: Any = None) -> None:
        """Keep ``config`` and an optional ``metrics``; nothing connects until ``query``."""
        self._config = config
        self._metrics = metrics

    def _auth(self, bearer: Optional[str]):
        cfg = self._config
        if cfg.trino_identity_mode not in _IDENTITY_MODES:
            raise ValueError(
                f"TRINO_IDENTITY_MODE {cfg.trino_identity_mode!r}: expected {_IDENTITY_MODES}"
            )
        if cfg.trino_identity_mode == "jwt":
            if cfg.trino_http_scheme != "https":
                raise ValueError("TRINO_IDENTITY_MODE=jwt requires TRINO_HTTP_SCHEME=https")
            if not bearer:
                raise PermissionError(
                    "TRINO_IDENTITY_MODE=jwt and the request carries no bearer: refusing to "
                    "fall back to the service account"
                )
            return trino.auth.JWTAuthentication(bearer)
        if cfg.trino_password:
            if cfg.trino_http_scheme != "https":
                raise ValueError("TRINO_PASSWORD is set but TRINO_HTTP_SCHEME is not https")
            return trino.auth.BasicAuthentication(cfg.trino_user, cfg.trino_password)
        return None

    def _verify(self):
        v = self._config.trino_verify.strip()
        if v.lower() in ("true", "1", "yes"):
            return True
        if v.lower() in ("false", "0", "no"):
            return False
        return v  # a CA bundle path

    def _connect(self, user: Optional[str] = None, bearer: Optional[str] = None):
        cfg = self._config
        return trino.dbapi.connect(
            host=cfg.trino_host,
            port=cfg.trino_port,
            user=user or cfg.trino_user,
            catalog=cfg.trino_catalog,
            http_scheme=cfg.trino_http_scheme,
            auth=self._auth(bearer),
            verify=self._verify(),
            request_timeout=cfg.trino_request_timeout,
        )

    def _run(self, sql: str, user: Optional[str], params: Any, bearer: Optional[str]) -> dict:
        conn = self._connect(user, bearer)
        cursor = conn.cursor()
        cursor.execute(sql, params)
        rows = cursor.fetchmany(self._config.max_rows)
        columns = [d[0] for d in cursor.description] if cursor.description else []
        return {"columns": columns, "rows": [list(r) for r in rows], "row_count": len(rows)}

    def query(
        self,
        sql: str,
        user: Optional[str] = None,
        params: Any = None,
        bearer: Optional[str] = None,
    ) -> dict:
        """Run ``sql`` as ``user`` (or the configured user); columns, rows capped at max_rows,
        row_count.
        """
        if self._metrics is None:
            return self._run(sql, user, params, bearer)
        self._metrics.queries.inc()
        start = time.perf_counter()
        try:
            return self._run(sql, user, params, bearer)
        except Exception:
            self._metrics.errors.inc()
            raise
        finally:
            self._metrics.duration.observe(time.perf_counter() - start)
