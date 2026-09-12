"""ASGI identity middleware: verifies the JWT on every request and sets the Principal.

Mounted on the transport app FastMCP serves — see `core.app`, which mounts it
on whichever transport is configured. Every tool call is an HTTP request
carrying an Authorization header. The middleware verifies it through the
AuthProvider, stores the Principal in the ContextVar the tools read, and clears
it afterwards. In strict mode (`require_auth`) an unauthenticated request is
rejected with 401.

IMPORTANT: this is a PURE ASGI middleware, not BaseHTTPMiddleware. The latter
runs the downstream in a separate task, which BREAKS ContextVar propagation to
the tools — the identity would never reach them.
"""
from __future__ import annotations

from typing import Any

from starlette.responses import JSONResponse

from .identity import reset_current_principal, set_current_principal


class AuthIdentityMiddleware:
    def __init__(self, app, auth_provider: Any = None, *, require_auth: bool = False):
        self.app = app
        self._auth = auth_provider
        self._require = require_auth

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope.get("headers", [])}
        principal = self._auth.verify(headers) if self._auth is not None else None
        if self._require and principal is None:
            await JSONResponse({"error": "unauthenticated"}, status_code=401)(scope, receive, send)
            return
        token = set_current_principal(principal)
        try:
            await self.app(scope, receive, send)
        finally:
            reset_current_principal(token)
