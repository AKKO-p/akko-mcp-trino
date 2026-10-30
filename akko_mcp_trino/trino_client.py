"""Trino client: one connection per query, bounded execution, the identity of a mode.

Who the server is for Trino is an identity mode, chosen by ``TRINO_IDENTITY_MODE``.
The core has four:

* ``jwt``, the default: every query runs as the verified caller. The server
  exchanges the caller's token, issued for this server, for a token whose
  audience is Trino (OAuth 2.0 Token Exchange, RFC 8693, see
  `akko_mcp_trino.token_exchange`). Trino verifies that token with its JWT
  authenticator and its access control (Ranger, OPA, file rules) decides for the
  caller. The caller's own token never leaves the server: the MCP authorization
  specification forbids passing it through.
* ``impersonation``, for an identity provider that cannot exchange tokens: the
  server authenticates to Trino as its own account (``TRINO_USER`` with
  ``TRINO_PASSWORD``, or a client certificate), and asks Trino to run the query
  as the verified caller. Trino allows it under an impersonation rule that only
  this account has. The caller's name comes from their verified token, never from
  a header they wrote.
* ``service``, only when named: every query runs as ``TRINO_USER``, the same for
  every caller. The server says so at start and in its instructions to hosts.
* ``oauth2``, stdio only: Trino authenticates the local user through its browser
  login and names the user, never ``TRINO_USER``. One authentication object reuses
  the driver's token cache and handles reauthentication. The first call that is not
  discovery verifies the identity with ``SELECT current_user``
  (`TrinoClient.current_user`), the one query that runs before a caller is bound;
  every other query needs that verified caller, as in ``jwt`` and ``impersonation``.

The ``jwt`` and ``impersonation`` modes refuse to start unless the door requires
a verified token, and refuse a query without a verified caller: never run as
anyone else. A password, a token or a certificate never travels over plain http.
An extension may add other modes (see `akko_mcp_trino.extensions`); an unknown
mode refuses to start.

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
import logging
import threading
import time
import webbrowser
from typing import TYPE_CHECKING, Any, Callable, Optional, Protocol, Sequence, Union

import httpx
import trino
import trino.auth

from .audit import ToolCall, current_call, time_allowed
from .config import Config
from .identity import current_bearer
from .token_exchange import TokenExchange

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

    def connection(self, user: Optional[str]) -> tuple[Optional[str], Any]:
        """The Trino user and the authentication of one query run for ``user`` (the
        verified caller's subject, or None); a Trino user of None sends no user, and
        Trino runs the query as the principal it authenticated. Raises PermissionError
        when the mode cannot run it."""
        ...  # pragma: no cover - protocol

    def ping(self, client: "TrinoClient") -> None:  # pragma: no cover - protocol
        """Readiness: raise when Trino cannot serve this mode now."""
        ...


def _server_account(cfg: Config) -> tuple[str, Any]:
    """The line ``--check`` prints and the authentication of the server's own Trino
    account: a client certificate (mTLS), a password, or nothing for a local Trino.
    Refuses a secret over plain http, and a certificate without its key."""
    if bool(cfg.trino_client_cert) != bool(cfg.trino_client_key):
        raise ValueError("TRINO_CLIENT_CERT and TRINO_CLIENT_KEY go together")
    if (cfg.trino_password or cfg.trino_client_cert) and cfg.trino_http_scheme != "https":
        raise ValueError(
            "TRINO_PASSWORD or TRINO_CLIENT_CERT is set but TRINO_HTTP_SCHEME is not https"
        )
    if cfg.trino_client_cert:
        return "client certificate", trino.auth.CertificateAuthentication(
            cfg.trino_client_cert, cfg.trino_client_key
        )
    if cfg.trino_password:
        return "password set", trino.auth.BasicAuthentication(cfg.trino_user, cfg.trino_password)
    return "no password", None


def _require_verified_callers(cfg: Config) -> None:
    """A mode that runs every query as the caller needs a door that refuses a request
    without a verified token (401), rather than a query that fails later."""
    if not (cfg.auth_enabled and cfg.auth_required):
        raise ValueError(
            f"TRINO_IDENTITY_MODE={cfg.trino_identity_mode} runs every query as the verified "
            f"caller and needs MCP_AUTH_ENABLED=true and MCP_AUTH_REQUIRED=true; to run every "
            f"query as TRINO_USER instead, set TRINO_IDENTITY_MODE=service"
        )


def _verified_caller(cfg: Config, user: Optional[str]) -> str:
    if not user:
        raise PermissionError(
            f"TRINO_IDENTITY_MODE={cfg.trino_identity_mode} and the request has no verified "
            f"caller: refusing to run the query as anyone else"
        )
    return user


class ServiceIdentity:
    """``service``: every query runs as ``TRINO_USER``, the identity the administrator
    configured; a client certificate or ``TRINO_PASSWORD`` when one is set."""

    def __init__(self, config: Config) -> None:
        """Keep the core's settings."""
        self._config = config

    def describe(self) -> list[str]:
        """The configured user and how it authenticates, never the secret; refuses a
        secret over plain http."""
        cfg = self._config
        secret, _ = _server_account(cfg)
        return [f"trino identity: {cfg.trino_user} ({secret}), the same for every caller"]

    def connection(self, user: Optional[str]) -> tuple[str, Any]:
        """The configured user, whoever the caller."""
        cfg = self._config
        return cfg.trino_user, _server_account(cfg)[1]

    def ping(self, client: "TrinoClient") -> None:
        """A bounded ``SELECT 1`` as the configured identity."""
        client.query("SELECT 1")


class OAuth2Identity:
    """``oauth2``: one local user's native Trino browser login, cached by the driver."""

    def __init__(self, config: Config) -> None:
        """Keep the core's settings and one authentication object for all connections,
        created only on first use."""
        self._config = config
        self._auth: Any = None

    def describe(self) -> list[str]:
        """Refuse shared HTTP transports and unencrypted authentication."""
        if self._config.transport != "stdio":
            raise ValueError("TRINO_IDENTITY_MODE=oauth2 requires MCP_TRANSPORT=stdio")
        if self._config.trino_http_scheme != "https":
            raise ValueError("TRINO_IDENTITY_MODE=oauth2 requires TRINO_HTTP_SCHEME=https")
        return [
            "trino identity: native OAuth2 browser login, verified by SELECT current_user "
            "(TRINO_USER is ignored)"
        ]

    def connection(self, user: Optional[str]) -> tuple[Optional[str], Any]:
        """The connection of a query run for ``user``, the subject Trino named at sign-in;
        refuses a query without it, so nothing reaches Trino outside an admission."""
        _verified_caller(self._config, user)
        return self.sign_in()

    def sign_in(self) -> tuple[None, Any]:
        """The connection of the identity check alone (`TrinoClient.current_user`): no
        Trino user, so Trino names the user it authenticated, never ``TRINO_USER``; one
        authentication object, so cached tokens are reused."""
        if self._auth is None:
            self._auth = trino.auth.OAuth2Authentication(
                redirect_auth_url_handler=trino.auth.CompositeRedirectHandler([_oauth2_redirect])
            )
        return None, self._auth

    def ping(self, client: "TrinoClient") -> None:
        """The identity check, ``SELECT current_user``, as the user who signed in."""
        client.current_user()


def _oauth2_redirect(url: str) -> None:
    """Open the login URL and log it to stderr, never the stdio protocol channel."""
    logging.getLogger(__name__).warning("Open this URL to authenticate with Trino: %s", url)
    webbrowser.open_new(url)


class ImpersonationIdentity(ServiceIdentity):
    """``impersonation``: the server authenticates as ``TRINO_USER`` and Trino runs each
    query as the verified caller, under an impersonation rule only that account has."""

    def describe(self) -> list[str]:
        """The server's account and the rule Trino needs; refuses to start without https,
        without a credential for the account, or with a door that admits unverified callers."""
        cfg = self._config
        _require_verified_callers(cfg)
        if cfg.trino_http_scheme != "https":
            raise ValueError("TRINO_IDENTITY_MODE=impersonation requires TRINO_HTTP_SCHEME=https")
        secret, auth = _server_account(cfg)
        if auth is None:
            raise ValueError(
                "TRINO_IDENTITY_MODE=impersonation needs the server's own Trino account to "
                "authenticate: set TRINO_PASSWORD, or TRINO_CLIENT_CERT and TRINO_CLIENT_KEY"
            )
        return [
            f"trino identity: the verified caller, impersonated by {cfg.trino_user} ({secret}); "
            f"Trino must allow only {cfg.trino_user} to impersonate"
        ]

    def connection(self, user: Optional[str]) -> tuple[str, Any]:
        """The verified caller, with the server account's authentication."""
        cfg = self._config
        return _verified_caller(cfg, user), _server_account(cfg)[1]

    def ping(self, client: "TrinoClient") -> None:
        """A bounded ``SELECT 1`` as the server's own account, impersonating no one."""
        client.query("SELECT 1", user=self._config.trino_user)


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
    """``jwt``: Trino receives, for the verified caller, a token the server obtained by
    token exchange; never the caller's own."""

    def __init__(self, config: Config, exchange: Optional[TokenExchange] = None) -> None:
        """Keep the core's settings and the token exchange (built from them by default)."""
        self._config = config
        self._exchange = exchange if exchange is not None else TokenExchange(config)

    def trino_token(self, bearer: str) -> str:
        """The token Trino receives for the caller: the caller's verified bearer, exchanged
        at the identity provider for one whose audience is Trino. An extension may override
        it to obtain that token another way; it must never return ``bearer`` itself."""
        return self._exchange.token_for(bearer)

    def describe(self) -> list[str]:
        """What the mode sends Trino; refuses to start over plain http, without the token
        exchange's settings, or with a door that admits unverified callers."""
        cfg = self._config
        _require_verified_callers(cfg)
        if cfg.trino_http_scheme != "https":
            raise ValueError("TRINO_IDENTITY_MODE=jwt requires TRINO_HTTP_SCHEME=https")
        return [
            "trino identity: the verified caller, with a token for Trino obtained by token "
            "exchange; the caller's own token never leaves the server",
            *self._exchange.describe(),
        ]

    def connection(self, user: Optional[str]) -> tuple[str, Any]:
        """The caller and a token for Trino; refused over http or without a verified caller."""
        cfg = self._config
        bearer = current_bearer()
        if cfg.trino_http_scheme != "https":
            raise ValueError("TRINO_IDENTITY_MODE=jwt requires TRINO_HTTP_SCHEME=https")
        if not bearer:
            raise PermissionError(
                "TRINO_IDENTITY_MODE=jwt and the request carries no verified bearer: "
                "refusing to fall back to TRINO_USER"
            )
        caller = _verified_caller(cfg, user)
        return caller, trino.auth.JWTAuthentication(self.trino_token(bearer))

    def ping(self, client: "TrinoClient") -> None:
        """No identity of its own to query with: ask Trino's public ``/v1/info``."""
        trino_info(self._config, verify_setting(self._config))


def resolve_identity(config: Config, extensions: Sequence["Extension"] = ()) -> IdentityMode:
    """The identity mode ``TRINO_IDENTITY_MODE`` names, checked: the core's ``jwt``,
    ``impersonation``, ``oauth2`` and ``service``, or an extension's. Raises ValueError, naming the
    modes on offer, when none matches."""
    modes: dict[str, IdentityMode] = {
        "service": ServiceIdentity(config),
        "jwt": JwtIdentity(config),
        "impersonation": ImpersonationIdentity(config),
        "oauth2": OAuth2Identity(config),
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

    def _connect(self, user: Optional[str] = None, connection: Optional[tuple] = None):
        cfg = self._config
        trino_user, auth = connection if connection is not None else self._identity.connection(user)
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

    def _time_budget(self, timeout: float = 0.0) -> tuple[float, Optional[ToolCall]]:
        """Seconds the next query may run (0: Trino's own limit), and the tool call whose
        deadline sets them when what is left of it is shorter than the query's own budget:
        ``TRINO_QUERY_TIMEOUT_SECONDS``, lowered by ``timeout`` when above 0. Past the
        call's deadline, no query starts."""
        budget = self._config.query_timeout_seconds
        if timeout > 0:
            budget = min(budget, timeout) if budget > 0 else timeout
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

    def _run(
        self,
        sql: str,
        user: Optional[str],
        params: Any,
        limit: int,
        timeout: float,
        connection: Optional[tuple] = None,
    ) -> dict:
        budget, call = self._time_budget(timeout)
        conn = self._connect(user, connection)
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
        row_limit: int = 0,
        timeout: float = 0.0,
    ) -> dict:
        """Run ``sql`` for ``user`` through the identity mode: columns, rows, row_count,
        and truncated when more rows existed. ``max_rows`` lowers the server's cap, never raises it.

        ``row_limit``, above 0, replaces the server's cap for this query: it is meant for
        the metadata reads the server builds itself, never for an agent's SQL. ``timeout``,
        above 0, lowers this query's time budget: the shortest of it,
        ``TRINO_QUERY_TIMEOUT_SECONDS`` and what is left of the call wins. A query stopped
        by its own ``timeout`` raises a plain ``TimeoutError`` and leaves the call alive; one
        stopped by the call's deadline raises the call's ``CallTimeout``.
        """
        cap = row_limit if row_limit > 0 else self._config.max_rows
        limit = min(max_rows, cap) if max_rows > 0 else cap
        if self._metrics is None:
            return self._run(sql, user, params, limit, timeout)
        self._metrics.queries.inc()
        start = time.perf_counter()
        try:
            return self._run(sql, user, params, limit, timeout)
        except Exception:
            self._metrics.errors.inc()
            raise
        finally:
            self._metrics.duration.observe(time.perf_counter() - start)

    def current_user(self) -> str:
        """The user Trino authenticated, by ``SELECT current_user`` on the sign-in
        connection of ``oauth2``: the one query that runs before a caller is bound.

        Raises ValueError for any other identity mode, which knows its caller before
        Trino does, and PermissionError when Trino names nobody."""
        if not isinstance(self._identity, OAuth2Identity):
            raise ValueError(
                f"TRINO_IDENTITY_MODE={self._config.trino_identity_mode} does not sign in "
                f"to Trino: only oauth2 asks Trino who the user is"
            )
        rows = self._run("SELECT current_user", None, None, 1, 0.0, self._identity.sign_in())
        subject = rows["rows"][0][0] if rows["rows"] and rows["rows"][0] else None
        if not isinstance(subject, str) or not subject.strip():
            raise PermissionError("Trino OAuth2 returned no authenticated user")
        return subject
