"""ASGI identity middleware: binds the user and the agent product on every request.

Mounted on the transport app FastMCP serves — see `core.app`, which mounts it
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
  where to authenticate instead of seeing a bare refusal.

Every refusal carries an `X-Reason` header naming the cause; the body never
carries the token.

IMPORTANT: this is a PURE ASGI middleware, not BaseHTTPMiddleware. The latter
runs the downstream in a separate task, which BREAKS ContextVar propagation to
the tools — the identity would never reach them.
"""
from __future__ import annotations

from typing import Any

from starlette.responses import JSONResponse

from .agents import AGENT_KEY_HEADER, AgentRegistry, reset_current_agent, set_current_agent
from .discovery import WELL_KNOWN_PATH, ProtectedResource
from .identity import reset_current_principal, set_current_principal


class AuthIdentityMiddleware:
    def __init__(
        self,
        app,
        auth_provider: Any = None,
        *,
        require_auth: bool = False,
        agents: AgentRegistry | None = None,
        discovery: ProtectedResource | None = None,
    ):
        self.app = app
        self._auth = auth_provider
        self._require = require_auth
        self._agents = agents if agents is not None and agents.enabled else None
        self._discovery = discovery if discovery is not None and discovery.resource else None

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        if self._discovery is not None and scope.get("path") == WELL_KNOWN_PATH:
            await JSONResponse(self._discovery.document())(scope, receive, send)
            return
        headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope.get("headers", [])}

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

        token = set_current_principal(principal)
        agent_token = set_current_agent(agent)
        try:
            await self.app(scope, receive, send)
        finally:
            reset_current_agent(agent_token)
            reset_current_principal(token)

    async def _refuse(self, reason: str, scope, receive, send) -> None:
        headers = {"X-Reason": reason}
        if self._discovery is not None:
            headers["WWW-Authenticate"] = self._discovery.www_authenticate()
        await JSONResponse({"error": reason}, status_code=401, headers=headers)(scope, receive, send)
