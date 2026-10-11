"""Trino client: one connection per query, bounded execution, the identity of a mode.

Who the server is for Trino is an identity mode, chosen by ``TRINO_IDENTITY_MODE``.
The core has two:

* ``service``, the default: every query runs as the identity the administrator
  configured, ``TRINO_USER``, authenticated by ``TRINO_PASSWORD`` when it is set.
  Trino's access control decides for that identity, the same for every caller:
  give it the rights every caller of this server may have, no more.
* ``jwt``: the caller's own bearer, already verified at the door, is sent to
  Trino as its JWT. Trino verifies it again with its JWT authenticator and runs
  the query as that user, so its access control (Ranger, OPA, file rules)
  decides for the caller: no service account, no password, no right to act
  for another user. A request without a verified bearer is refused, never run as anyone.

A password or a bearer never travels over plain http. An extension may add other
modes (see `akko_mcp_trino.extensions`); an unknown mode refuses to start.

Every query tells Trino where it comes from: the source is the server name
(``TRINO_SOURCE`` overrides it). Results are bounded twice: one row more than
the cap is read to say whether the answer was cut (``truncated``), and a query
that outlives ``TRINO_QUERY_TIMEOUT_SECONDS`` is cancelled in Trino. Within a
tool call that has a time budget (``MCP_CALL_TIMEOUT_SECONDS``), a query also
gets no more than what is left of it: past the call's deadline none starts,
and the one running is cancelled.
"""

from __future__ import annotations

import contextlib
import threading
import time
from typing import TYPE_CHECKING, Any, Callable, Optional, Protocol, Sequence, Union

import httpx
import trino
import trino.auth

from .audit import ToolCall, current_call, time_allowed
from .config import Config
from .identity import current_bearer

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .extensions import Extension

# Until Trino answers the POST of a statement, the query has no name and a cancel stops
# nothing; past its budget, the cancel is tried again this often until the query has one.
_RECANCEL_SECONDS = 0.05


def verify_setting(config: Config) -> Union[bool, str]:
    """``TRINO_VERIFY`` as the Trino client takes it: True, False, or a CA bundle path."""
    v = config.trino_verify.strip()
    if v.lower() in ("true", "1", "yes"):
        return True
    if v.lower() in ("false", "0", "no"):
        return False
    return v  # a CA bundle path


class IdentityMode(Protocol):
    """Who the server is for Trino, query by query."""

    def describe(self) -> list[str]:  # pragma: no cover - protocol
        """Lines ``--check`` prints, never a secret; raises ValueError when the mode
        cannot serve its settings, so the server refuses to start."""
        ...

    def connection(self, user: Optional[str]) -> tuple[str, Any]:
        """The Trino user and the authentication of one query run for ``user`` (the
        verified caller's subject, or None); raises PermissionError when the mode
        cannot run it."""
        ...  # pragma: no cover - protocol

    def ping(self, client: "TrinoClient") -> None:  # pragma: no cover - protocol
        """Readiness: raise when Trino cannot serve this mode now."""
        ...


class ServiceIdentity:
    """``service``: every query runs as ``TRINO_USER``, the identity the administrator
    configured; Basic authentication with ``TRINO_PASSWORD`` when it is set."""

    def __init__(self, config: Config) -> None:
        """Keep the core's settings."""
        self._config = config

    def describe(self) -> list[str]:
        """The configured user and whether a password is set, never the password; refuses
        a password over plain http."""
        cfg = self._config
        if cfg.trino_password and cfg.trino_http_scheme != "https":
            raise ValueError("TRINO_PASSWORD is set but TRINO_HTTP_SCHEME is not https")
        secret = "password set" if cfg.trino_password else "no password"
        return [f"trino identity: {cfg.trino_user} ({secret}), the same for every caller"]

    def connection(self, user: Optional[str]) -> tuple[str, Any]:
        """The configured user, whoever the caller."""
        cfg = self._config
        self.describe()
        if cfg.trino_password:
            return cfg.trino_user, trino.auth.BasicAuthentication(
                cfg.trino_user, cfg.trino_password
            )
        return cfg.trino_user, None

    def ping(self, client: "TrinoClient") -> None:
        """A bounded ``SELECT 1`` as the configured identity."""
        client.query("SELECT 1")


def trino_info(config: Config, verify: Any) -> None:
    """Ask Trino's public ``/v1/info`` (no identity needed) whether it has finished starting."""
    r = httpx.get(
        f"{config.trino_http_scheme}://{config.trino_host}:{config.trino_port}/v1/info",
        verify=verify,
        timeout=5,
    )
    if r.status_code != 200:
        raise RuntimeError(f"Trino /v1/info: HTTP {r.status_code}")
    if r.json().get("starting", False):
        raise RuntimeError("Trino is starting")


class JwtIdentity:
    """``jwt``: the caller's verified bearer goes to Trino, which verifies it again."""

    def __init__(self, config: Config) -> None:
        """Keep the core's settings."""
        self._config = config

    def trino_token(self, bearer: str) -> str:
        """The token Trino receives for the caller: their own verified bearer."""
        return bearer

    def describe(self) -> list[str]:
        """What the mode sends Trino; refuses to start over plain http."""
        if self._config.trino_http_scheme != "https":
            raise ValueError("TRINO_IDENTITY_MODE=jwt requires TRINO_HTTP_SCHEME=https")
        return ["trino identity: the caller's own token (jwt), verified again by Trino"]

    def connection(self, user: Optional[str]) -> tuple[str, Any]:
        """The caller and their bearer; refused over http or without a verified bearer."""
        cfg = self._config
        bearer = current_bearer()
        if cfg.trino_http_scheme != "https":
            raise ValueError("TRINO_IDENTITY_MODE=jwt requires TRINO_HTTP_SCHEME=https")
        if not bearer:
            raise PermissionError(
                "TRINO_IDENTITY_MODE=jwt and the request carries no verified bearer: "
                "refusing to fall back to TRINO_USER"
            )
        return user or cfg.trino_user, trino.auth.JWTAuthentication(self.trino_token(bearer))

    def ping(self, client: "TrinoClient") -> None:
        """No identity of its own to query with: ask Trino's public ``/v1/info``."""
        trino_info(self._config, verify_setting(self._config))


def resolve_identity(config: Config, extensions: Sequence["Extension"] = ()) -> IdentityMode:
    """The identity mode ``TRINO_IDENTITY_MODE`` names, checked: the core's ``service`` and
    ``jwt``, or an extension's. Raises ValueError, naming the modes on offer, when none
    matches."""
    modes: dict[str, IdentityMode] = {
        "service": ServiceIdentity(config),
        "jwt": JwtIdentity(config),
    }
    for ext in extensions:
        modes.update(ext.identity_modes())
    mode = modes.get(config.trino_identity_mode)
    if mode is None:
        raise ValueError(
            f"TRINO_IDENTITY_MODE {config.trino_identity_mode!r}: expected one of "
            f"{tuple(sorted(modes))}"
        )
    mode.describe()
    return mode


def every_row(query: Callable[[str], dict], sql: str, limit: int = 0) -> list:
    """Every row of the ordered statement ``sql``, or its first ``limit`` rows when ``limit`` > 0.

    The row cap (``TRINO_MAX_ROWS``) bounds what an agent reads, not what the server
    needs to know: an extension reading every column of a wide table, or every comment
    of a schema. Such a reader goes on from the next row (``OFFSET``) as long as
    ``query`` says a page was cut (``truncated``), so no row is lost to the cap, and it
    never asks past ``limit``. ``sql`` must be ordered (``ORDER BY``) for each page to
    follow the last; a cut page that brings nothing ends the reading.
    """
    rows: list = []
    read = 0
    while True:
        clauses = [f"OFFSET {read}"] if read else []
        if limit > 0:
            clauses.append(f"LIMIT {limit - read}")
        res = query(" ".join([sql, *clauses]))
        page = res.get("rows", [])
        read += len(page)
        rows += [r for r in page if r]
        if not (res.get("truncated") and page) or 0 < limit <= read:
            return rows


class TrinoClient:
    """Wraps the Trino connection. `query` returns {columns, rows, row_count,
    truncated}, capped at `config.max_rows`. `user` is the verified caller's subject,
    handed to the identity mode, which decides who Trino sees; `metrics`, when given,
    records queries, errors and latency."""

    def __init__(
        self,
        config: Config,
        metrics: Any = None,
        *,
        identity: Optional[IdentityMode] = None,
        tags: Optional[Callable[[], list[str]]] = None,
    ) -> None:
        """Keep ``config``, an optional ``metrics``, the ``identity`` mode (by default
        the one ``TRINO_IDENTITY_MODE`` names among the core's, checked here, so a
        server that could not serve it never starts) and ``tags``, the client tags of
        a query run now. Nothing connects until ``query``."""
        self._config = config
        self._metrics = metrics
        self._identity = identity if identity is not None else resolve_identity(config)
        self._tags = tags

    def ping(self) -> None:
        """Readiness, as the identity mode checks it."""
        self._identity.ping(self)

    def _connect(self, user: Optional[str] = None):
        cfg = self._config
        trino_user, auth = self._identity.connection(user)
        tags = self._tags() if self._tags is not None else []
        return trino.dbapi.connect(
            host=cfg.trino_host,
            port=cfg.trino_port,
            user=trino_user,
            catalog=cfg.trino_catalog,
            http_scheme=cfg.trino_http_scheme,
            auth=auth,
            verify=verify_setting(cfg),
            request_timeout=cfg.trino_request_timeout,
            source=cfg.trino_source or cfg.server_name,
            client_tags=tags or None,
        )

    def _time_budget(self) -> tuple[float, Optional[ToolCall]]:
        """Seconds the next query may run (0: Trino's own limit), and the tool call whose
        deadline sets them when what is left of it is shorter than the query budget.
        Past the call's deadline, no query starts."""
        budget = self._config.query_timeout_seconds
        allowed = time_allowed(budget)
        return allowed, (current_call() if allowed != budget else None)

    def _fetch(
        self,
        cursor: Any,
        sql: str,
        params: Any,
        limit: int,
        budget: float,
        call: Optional[ToolCall],
    ) -> list:
        """Execute and read up to ``limit`` rows, within ``budget`` seconds when above 0;
        ``call`` is the tool call whose deadline sets that budget, if it does.

        Past the budget there is no answer, however the query ended: cancelled while it ran
        (the fetch fails), or not yet named by Trino when the budget ran out (a cancel then
        has nothing to stop, so it is tried again until Trino names the query, which the
        Trino client's ``execute`` waits on until the first rows), or its last page read
        just as the budget ran out."""
        if budget <= 0:
            cursor.execute(sql, params)
            return cursor.fetchmany(limit)
        expired, done = threading.Event(), threading.Event()

        def expire() -> None:
            expired.set()
            # DELETE on the query: Trino stops it, the fetch below fails. A query not yet
            # named cannot be stopped: try again until it is, or until the statement is over.
            while not done.is_set():
                named = cursor.query_id is not None
                cursor.cancel()
                if named:
                    return
                done.wait(_RECANCEL_SECONDS)

        def over() -> TimeoutError:
            if call is not None:
                return call.timed_out()
            return TimeoutError(
                f"the query exceeded its time budget of {budget:g} s and was cancelled"
            )

        timer = threading.Timer(budget, expire)
        timer.daemon = True
        timer.start()
        try:
            cursor.execute(sql, params)
            rows = [] if expired.is_set() else cursor.fetchmany(limit)
        except Exception as exc:
            if expired.is_set():
                raise over() from exc
            raise
        finally:
            done.set()
            timer.cancel()
        if expired.is_set():
            with contextlib.suppress(Exception):
                cursor.cancel()  # Trino has named the query by now: stop what is left of it
            raise over()
        return rows

    def _run(self, sql: str, user: Optional[str], params: Any, limit: int) -> dict:
        budget, call = self._time_budget()
        conn = self._connect(user)
        cursor = conn.cursor()
        rows = self._fetch(cursor, sql, params, limit + 1, budget, call)
        truncated = len(rows) > limit
        if truncated:
            # The caller gets `limit` rows; the rest of the query is not left running.
            with contextlib.suppress(Exception):
                cursor.cancel()
            rows = rows[:limit]
        columns = [d[0] for d in cursor.description] if cursor.description else []
        return {
            "columns": columns,
            "rows": [list(r) for r in rows],
            "row_count": len(rows),
            "truncated": truncated,
        }

    def query(
        self,
        sql: str,
        user: Optional[str] = None,
        params: Any = None,
        *,
        max_rows: int = 0,
    ) -> dict:
        """Run ``sql`` for ``user`` through the identity mode: columns, rows, row_count,
        and truncated when more rows existed. ``max_rows`` lowers the server's cap, never raises it.
        """
        cap = self._config.max_rows
        limit = min(max_rows, cap) if max_rows > 0 else cap
        if self._metrics is None:
            return self._run(sql, user, params, limit)
        self._metrics.queries.inc()
        start = time.perf_counter()
        try:
            return self._run(sql, user, params, limit)
        except Exception:
            self._metrics.errors.inc()
            raise
        finally:
            self._metrics.duration.observe(time.perf_counter() - start)
