"""OAuth 2.0 Token Exchange (RFC 8693): a token for Trino, never the caller's own.

The MCP authorization specification forbids token passthrough: a token issued
for this server must not be sent on to another service. In the ``jwt`` identity
mode the server therefore acts as an OAuth client of the identity provider. It
presents the caller's verified token (``subject_token``) with its own client
credentials and asks for an access token whose audience is Trino; Trino verifies
that token and runs the query as the user it names. The caller's token never
leaves the server, and a token for Trino never reaches the caller.

Exchanged tokens are kept in memory, keyed by a hash of the caller's token,
until shortly before they expire, so a tool call that runs several queries asks
the identity provider once. Nothing here logs a token or puts one in an error.
"""

from __future__ import annotations

import hashlib
import threading
import time
from typing import Any, Callable, Optional, Union

import httpx

from .audit import time_allowed
from .config import Config

GRANT_TYPE = "urn:ietf:params:oauth:grant-type:token-exchange"
ACCESS_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"
# An exchanged token is used until this many seconds before it expires.
_EXPIRY_MARGIN_SECONDS = 30.0
# Exchanged tokens kept at most; the oldest goes first.
_CACHE_SIZE = 1024


def tls_verify(value: str) -> Union[bool, str]:
    """A verification setting as httpx takes it: True, False, or a CA bundle path."""
    v = value.strip()
    if v.lower() in ("true", "1", "yes"):
        return True
    if v.lower() in ("false", "0", "no"):
        return False
    return v


class TokenExchange:
    """Exchanges a caller's verified token for a token whose audience is Trino.

    ``post`` (``httpx.post`` by default) and ``clock`` are injectable, so the class is
    testable without a network."""

    def __init__(
        self,
        config: Config,
        *,
        post: Optional[Callable[..., Any]] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Keep the exchange settings; nothing is sent until ``token_for``."""
        self._config = config
        self._post = post or httpx.post
        self._clock = clock
        self._cache: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()

    def describe(self) -> list[str]:
        """Lines ``--check`` prints, never the secret; raises ValueError on a setting the
        exchange cannot work with, so the server refuses to start."""
        cfg = self._config
        missing = [
            name
            for name, value in (
                ("TRINO_TOKEN_EXCHANGE_URL", cfg.token_exchange_url),
                ("TRINO_TOKEN_EXCHANGE_CLIENT_ID", cfg.token_exchange_client_id),
                ("TRINO_TOKEN_EXCHANGE_CLIENT_SECRET", cfg.token_exchange_client_secret),
                ("TRINO_TOKEN_EXCHANGE_AUDIENCE", cfg.token_exchange_audience),
            )
            if not value
        ]
        if missing:
            raise ValueError(
                f"TRINO_IDENTITY_MODE=jwt obtains a token for Trino by token exchange "
                f"(RFC 8693) and needs {', '.join(missing)}; if the identity provider "
                f"cannot exchange tokens, set TRINO_IDENTITY_MODE=impersonation"
            )
        if not cfg.token_exchange_url.lower().startswith("https://"):
            raise ValueError("TRINO_TOKEN_EXCHANGE_URL must be https: it carries a client secret")
        scope = f", scope {cfg.token_exchange_scope}" if cfg.token_exchange_scope else ""
        return [
            f"token exchange: {cfg.token_exchange_url} as client {cfg.token_exchange_client_id} "
            f"(secret set), audience {cfg.token_exchange_audience}{scope}"
        ]

    def token_for(self, subject_token: str) -> str:
        """A token for Trino on behalf of the holder of ``subject_token``, from the cache
        while it is valid; raises PermissionError when the identity provider refuses."""
        key = hashlib.sha256(subject_token.encode()).hexdigest()
        now = self._clock()
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None and cached[1] > now:
                return cached[0]
        token, lifetime = self._exchange(subject_token)
        with self._lock:
            for k in [k for k, (_, until) in self._cache.items() if until <= now]:
                del self._cache[k]
            while len(self._cache) >= _CACHE_SIZE:
                del self._cache[next(iter(self._cache))]
            if lifetime > _EXPIRY_MARGIN_SECONDS:
                self._cache[key] = (token, now + lifetime - _EXPIRY_MARGIN_SECONDS)
        return token

    def _exchange(self, subject_token: str) -> tuple[str, float]:
        cfg = self._config
        data = {
            "grant_type": GRANT_TYPE,
            "subject_token": subject_token,
            "subject_token_type": ACCESS_TOKEN_TYPE,
            "requested_token_type": ACCESS_TOKEN_TYPE,
            "audience": cfg.token_exchange_audience,
        }
        if cfg.token_exchange_scope:
            data["scope"] = cfg.token_exchange_scope
        r = self._post(
            cfg.token_exchange_url,
            data=data,
            auth=(cfg.token_exchange_client_id, cfg.token_exchange_client_secret),
            verify=tls_verify(cfg.token_exchange_verify),
            timeout=time_allowed(cfg.trino_request_timeout) or None,
        )
        try:
            body = r.json()
        except ValueError:
            body = {}
        if r.status_code != 200 or not isinstance(body, dict) or not body.get("access_token"):
            error = body.get("error", "") if isinstance(body, dict) else ""
            raise PermissionError(
                f"the identity provider refused to exchange the caller's token for a Trino "
                f"token (HTTP {r.status_code}{', ' + str(error) if error else ''})"
            )
        try:
            lifetime = float(body.get("expires_in", 0))
        except (TypeError, ValueError):
            lifetime = 0.0
        return str(body["access_token"]), lifetime
