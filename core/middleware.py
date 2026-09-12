"""Middleware ASGI d'identité — vérifie le JWT par requête et pose le Principal.

Branché sur l'app Streamable HTTP de FastMCP : chaque appel d'outil est une requête
HTTP portant l'en-tête Authorization. Le middleware le vérifie (AuthProvider), pose le
Principal dans le ContextVar (lu par les outils → X-Trino-User), et nettoie après. En
mode strict (`require_auth`), une requête non authentifiée est rejetée (401).

IMPORTANT : middleware ASGI PUR (pas BaseHTTPMiddleware) — ce dernier exécute la suite
dans une TÂCHE séparée, ce qui CASSE la propagation du ContextVar jusqu'aux outils.
Vendor-neutre, 100% testable.
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
