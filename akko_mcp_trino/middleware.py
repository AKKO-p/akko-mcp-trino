"""ASGI identity middleware: binds the user and the agent product on every request.

Mounted on the transport app the SDK server serves — see `akko_mcp_trino.app`, which mounts it
on whichever transport is configured. Every tool call is an HTTP request
carrying an Authorization header. The middleware verifies it through the
AuthProvider, stores the Principal in the ContextVar the tools read, and clears
it afterwards. In strict mode (`require_auth`) an unauthenticated request is
rejected with 401.

Two more concerns live here because they are part of the same door:

* the agent product (`X-Agent-Key`) is resolved through the AgentRegistry when
  one is configured, and a missing or unknown key is refused before the user
  is even looked at — the agent is never a Trino user;
* RFC 9728 discovery: the metadata document is served without a token, and
  every 401 carries `WWW-Authenticate` pointing at it, so an MCP host knows
  where to authenticate instead of seeing a bare refusal;
* quotas per user and per agent product, checked after authentication and
  refused with 429 and `Retry-After` — see `akko_mcp_trino.ratelimit`;
* optional revocation through RFC 7662 introspection, refused with 401
  `revoked`, failing closed with 503 when the issuer cannot answer — see
  `akko_mcp_trino.revocation`.

Every request gets a request id — honoured from `X-Request-Id` when the edge
sent one, generated otherwise — echoed on the response and exposed to the
tools for the audit join. Every refusal carries an `X-Reason` header naming
the cause; the body never carries the token.

IMPORTANT: this is a PURE ASGI middleware, not BaseHTTPMiddleware. The latter
runs the downstream in a separate task, which BREAKS ContextVar propagation to
the tools — the identity would never reach them.
"""

from __future__ import annotations

import uuid
from typing import Any

from starlette.responses import JSONResponse

from .agents import AGENT_KEY_HEADER, AgentRegistry, reset_current_agent, set_current_agent
from .audit import REQUEST_ID_HEADER, reset_current_request_id, set_current_request_id
from .auth import extract_bearer_token
from .discovery import WELL_KNOWN_PATH, ProtectedResource
from .identity import (
    reset_current_bearer,
    reset_current_principal,
    set_current_bearer,
    set_current_principal,
)
from .ratelimit import RateLimiter
from .revocation import IntrospectionCheck, IntrospectionError


class AuthIdentityMiddleware:
    """The guard: agent key, user token, revocation, quotas, discovery and request id, on every HTTP
    request.
    """

    def __init__(
        self,
        app,
        auth_provider: Any = None,
        *,
        require_auth: bool = False,
        agents: AgentRegistry | None = None,
        discovery: ProtectedResource | None = None,
        limiter: RateLimiter | None = None,
        revocation: IntrospectionCheck | None = None,
    ):
        """Mount on ``app``; each guard is optional, off when its argument is None or disabled."""
        self.app = app
        self._auth = auth_provider
        self._require = require_auth
        self._agents = agents if agents is not None and agents.enabled else None
        self._discovery = discovery if discovery is not None and discovery.resource else None
        self._limiter = limiter if limiter is not None and limiter.enabled else None
        self._revocation = revocation

    async def __call__(self, scope, receive, send):
        """Handle one ASGI request: serve discovery, bind the request id, then run the guards."""
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        if self._discovery is not None and scope.get("path") == WELL_KNOWN_PATH:
            await JSONResponse(self._discovery.document())(scope, receive, send)
            return
        headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope.get("headers", [])}
        request_id = headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex
        send = _echoing(send, request_id)
        rid_token = set_current_request_id(request_id)
        try:
            await self._handle(scope, receive, send, headers)
        finally:
            reset_current_request_id(rid_token)

    async def _handle(self, scope, receive, send, headers) -> None:
        agent = None
        if self._agents is not None:
            presented = headers.get(AGENT_KEY_HEADER)
            if not presented:
                await self._refuse("agent_key_missing", scope, receive, send)
                return
            agent = self._agents.resolve(presented)
            if agent is None:
                await self._refuse("agent_key_unknown", scope, receive, send)
                return

        principal = self._auth.verify(headers) if self._auth is not None else None
        if self._require and principal is None:
            await self._refuse("unauthenticated", scope, receive, send)
            return

        # Revocation: the issuer may have disabled a token that still verifies.
        if self._revocation is not None and principal is not None:
            try:
                active = self._revocation.is_active(
                    extract_bearer_token(headers) or "", token_id=principal.token_id
                )
            except IntrospectionError:
                headers_out = {"X-Reason": "introspection_unavailable"}
                await JSONResponse(
                    {"error": "introspection_unavailable"}, status_code=503, headers=headers_out
                )(scope, receive, send)
                return
            if not active:
                await self._refuse("revoked", scope, receive, send)
                return

        # Quotas count authenticated requests only: a refused token must not
        # consume a slot that belongs to the person it impersonates.
        if self._limiter is not None:
            allowed, retry_after = self._limiter.check(
                subject=principal.subject if principal else None, agent=agent
            )
            if not allowed:
                headers = {"X-Reason": "rate_limited", "Retry-After": str(retry_after)}
                await JSONResponse({"error": "rate_limited"}, status_code=429, headers=headers)(
                    scope, receive, send
                )
                return

        token = set_current_principal(principal)
        agent_token = set_current_agent(agent)
        bearer_token = set_current_bearer(extract_bearer_token(headers) if principal else None)
        try:
            await self.app(scope, receive, send)
        finally:
            reset_current_bearer(bearer_token)
            reset_current_agent(agent_token)
            reset_current_principal(token)

    async def _refuse(self, reason: str, scope, receive, send) -> None:
        headers = {"X-Reason": reason}
        if self._discovery is not None:
            headers["WWW-Authenticate"] = self._discovery.www_authenticate()
        await JSONResponse({"error": reason}, status_code=401, headers=headers)(
            scope, receive, send
        )


def _echoing(send, request_id: str):
    """Wrap ``send`` so the response start carries ``X-Request-Id``."""

    async def wrapped(message):
        if message["type"] == "http.response.start":
            extra = [(REQUEST_ID_HEADER.encode(), request_id.encode())]
            message = {**message, "headers": list(message.get("headers", [])) + extra}
        await send(message)

    return wrapped
