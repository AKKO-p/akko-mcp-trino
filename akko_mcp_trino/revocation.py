"""Optional revocation check through RFC 7662 token introspection.

A JWT's signature and `exp` prove it *was* valid; only the issuer knows
whether it still is. When `MCP_INTROSPECTION_URL` is set, the guard asks the
issuer and refuses a token whose `active` is false. One call per request would
be too slow, so the verdict is cached by `jti` for a short TTL; a token without
`jti` is asked about every time. An issuer that cannot answer makes the guard
fail closed (503), because "unknown" is not "still valid".

`post` is injectable so the check is testable without a network; the default
uses httpx synchronously, which is fine at the rate a guard is called.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Mapping, Optional

import httpx


class IntrospectionError(RuntimeError):
    """The issuer could not be asked or did not answer with a verdict."""


def _default_post(url: str, *, data: dict, auth: tuple[str, str], timeout: float) -> dict:
    response = httpx.post(url, data=data, auth=auth, timeout=timeout)
    response.raise_for_status()
    return response.json()


class IntrospectionCheck:
    """Asks the issuer (RFC 7662) whether a token is still active; verdicts cached by ``jti``."""

    def __init__(
        self,
        url: str,
        client_id: str,
        client_secret: str,
        *,
        ttl_seconds: int = 30,
        timeout: float = 3.0,
        post: Callable[..., dict] = _default_post,
        clock: Callable[[], float] = time.monotonic,
    ):
        """Introspect at ``url`` with client credentials; ``post`` and ``clock`` are injectable for
        tests.
        """
        self._url = url
        self._auth = (client_id, client_secret)
        self._ttl = ttl_seconds
        self._timeout = timeout
        self._post = post
        self._clock = clock
        self._cache: dict[str, tuple[bool, float]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def from_env(env: Mapping[str, str]) -> Optional["IntrospectionCheck"]:
        """Build from ``MCP_INTROSPECTION_*``; None when no URL is set; raises when credentials are
        missing.
        """
        url = env.get("MCP_INTROSPECTION_URL", "").strip()
        if not url:
            return None
        client_id = env.get("MCP_INTROSPECTION_CLIENT_ID", "")
        secret = env.get("MCP_INTROSPECTION_CLIENT_SECRET", "")
        if not client_id or not secret:
            raise ValueError(
                "MCP_INTROSPECTION_URL is set but MCP_INTROSPECTION_CLIENT_ID or "
                "MCP_INTROSPECTION_CLIENT_SECRET is empty: the issuer would refuse every call"
            )
        return IntrospectionCheck(
            url, client_id, secret, ttl_seconds=int(env.get("MCP_INTROSPECTION_TTL_SECONDS", "30"))
        )

    def is_active(self, token: str, *, token_id: str) -> bool:
        """True if the issuer says the token is active; raises IntrospectionError when it cannot
        answer.
        """
        now = self._clock()
        if token_id:
            with self._lock:
                cached = self._cache.get(token_id)
            if cached is not None and cached[1] > now:
                return cached[0]
        try:
            verdict = self._post(
                self._url,
                data={"token": token, "token_type_hint": "access_token"},
                auth=self._auth,
                timeout=self._timeout,
            )
        except Exception as exc:  # noqa: BLE001 - any failure is "cannot answer"
            raise IntrospectionError(str(exc)) from exc
        active = bool(verdict.get("active", False))
        if token_id:
            with self._lock:
                self._cache[token_id] = (active, now + self._ttl)
        return active
