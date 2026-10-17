"""ASGI identity middleware: binds the verified caller on every request.

Mounted on the transport app the SDK server serves — see `akko_mcp_trino.app`, which mounts it
on whichever transport is configured. Every tool call is an HTTP request
carrying an Authorization header. The middleware verifies it through the
AuthProvider, stores the Principal in the ContextVar the tools read, binds the
verified bearer for the ``jwt`` identity mode, and clears both afterwards. In
strict mode (`require_auth`) an unauthenticated request is rejected with 401.

Extensions (see `akko_mcp_trino.extensions`) add to the same door, never in
place of it: documents served without a token, guards that refuse before the
token is looked at or once it is verified, and the challenge a refusal carries.
A guard can only refuse; the token is verified by the core whatever a guard
answers.

Without an extension, a refusal of the core says in its body what the client
should send, and when ``MCP_RESOURCE_URL`` names this server, the core publishes
its protected resource metadata (RFC 9728) and points to it in the challenge of
a 401, as the MCP authorization specification asks. An extension's documents
and challenges take precedence over the core's.

Every request gets a request id — honoured from `X-Request-Id` when the edge
sent a plain token (letters, digits, `.`, `_`, `:`, `-`, at most 128), generated
otherwise — echoed on the response and exposed to the tools for the audit.
Every refusal carries an `X-Reason` header naming the cause; the body never
carries the token.

IMPORTANT: this is a PURE ASGI middleware, not BaseHTTPMiddleware. The latter
runs the downstream in a separate task, which BREAKS ContextVar propagation to
the tools — the verified caller would never reach them.
"""

from __future__ import annotations

import re
import uuid
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

from starlette.responses import JSONResponse

from .audit import REQUEST_ID_HEADER, reset_current_request_id, set_current_request_id
from .auth import extract_bearer_token
from .extensions import Admission, Extension, Refusal
from .identity import (
    reset_current_bearer,
    reset_current_principal,
    set_current_bearer,
    set_current_principal,
)

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
UNAUTHENTICATED = "unauthenticated"
NO_TOKEN_MESSAGE = (
    "this server requires a bearer token issued for it by the identity provider: "
    "send Authorization: Bearer <access token>"
)
INVALID_TOKEN_MESSAGE = (
    "the bearer token is invalid, expired, or not issued for this server: "
    "ask the identity provider for a token whose audience is this server"
)
METADATA_PATH = "/.well-known/oauth-protected-resource"


def protected_resource(resource_url: str, issuer: str) -> tuple[dict[str, dict], str]:
    """The protected resource metadata (RFC 9728) of the server ``resource_url`` names, by
    the paths it is served at, and its URL; nothing when ``resource_url`` is empty."""
    if not resource_url:
        return {}, ""
    parts = urlsplit(resource_url)
    path = parts.path.rstrip("/")
    document = {
        "resource": resource_url,
        "authorization_servers": [issuer] if issuer else [],
        "bearer_methods_supported": ["header"],
    }
    paths = {METADATA_PATH: document}
    if path:
        paths[METADATA_PATH + path] = document
    return paths, f"{parts.scheme}://{parts.netloc}{METADATA_PATH}{path}"


def _request_id(sent: str | None) -> str:
    """The edge's id when it is a plain token, a fresh one otherwise."""
    return sent if sent and _REQUEST_ID_RE.match(sent) else uuid.uuid4().hex


class AuthIdentityMiddleware:
    """The guard: user token and request id on every HTTP request, and the guards of the
    extensions around them.
    """

    def __init__(
        self,
        app,
        auth_provider: Any = None,
        *,
        require_auth: bool = False,
        extensions: Sequence[Extension] = (),
        documents: Mapping[str, dict] = {},
        metadata_url: str = "",
    ):
        """Mount on ``app``; ``documents`` are the core's public documents and
        ``metadata_url`` the protected resource metadata a 401 points to; ``extensions``
        add their documents, guards and challenges, which take precedence."""
        self.app = app
        self._auth = auth_provider
        self._require = require_auth
        self._extensions = tuple(extensions)
        self._metadata_url = metadata_url
        self._documents: dict[str, dict] = dict(documents)
        for ext in self._extensions:
            self._documents.update(ext.public_documents())

    async def __call__(self, scope, receive, send):
        """Handle one ASGI request: serve public documents, bind the request id, then guard."""
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        document = self._documents.get(scope.get("path"))
        if document is not None:
            await JSONResponse(document)(scope, receive, send)
            return
        headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope.get("headers", [])}
        request_id = _request_id(headers.get(REQUEST_ID_HEADER))
        send = _echoing(send, request_id)
        rid_token = set_current_request_id(request_id)
        try:
            await self._handle(scope, receive, send, headers)
        finally:
            reset_current_request_id(rid_token)

    async def _handle(self, scope, receive, send, headers) -> None:
        bearer = extract_bearer_token(headers)
        admission = Admission(headers=headers, bearer=bearer)
        for ext in self._extensions:
            refusal = ext.before_auth(admission)
            if refusal is not None:
                await self._refuse(refusal, scope, receive, send)
                return

        principal = self._auth.verify(headers) if self._auth is not None else None
        if self._require and principal is None:
            # RFC 6750 §3.1: no error code when no token was sent at all.
            refusal = (
                Refusal(UNAUTHENTICATED, error="invalid_token", message=INVALID_TOKEN_MESSAGE)
                if bearer
                else Refusal(UNAUTHENTICATED, message=NO_TOKEN_MESSAGE)
            )
            await self._refuse(refusal, scope, receive, send)
            return

        admission.principal = principal
        for ext in self._extensions:
            refusal = ext.after_auth(admission)
            if refusal is not None:
                await self._refuse(refusal, scope, receive, send)
                return

        token = set_current_principal(principal)
        # The bearer is bound only once verified: an unverified one never reaches Trino.
        bearer_token = set_current_bearer(bearer if principal is not None else None)
        bound = [(var, var.set(value)) for var, value in admission.bindings]
        try:
            await self.app(scope, receive, send)
        finally:
            for var, var_token in reversed(bound):
                var.reset(var_token)
            reset_current_bearer(bearer_token)
            reset_current_principal(token)

    async def _refuse(self, refusal: Refusal, scope, receive, send) -> None:
        headers = {"X-Reason": refusal.reason, **refusal.headers}
        for ext in self._extensions:
            challenge = ext.challenge(refusal)
            if challenge is not None:
                headers["WWW-Authenticate"] = challenge
                break
        else:
            if self._metadata_url and refusal.reason == UNAUTHENTICATED:
                error = f', error="{refusal.error}"' if refusal.error else ""
                headers["WWW-Authenticate"] = (
                    f'Bearer resource_metadata="{self._metadata_url}"{error}'
                )
        body = {"error": refusal.reason}
        if refusal.message:
            body["message"] = refusal.message
        await JSONResponse(body, status_code=refusal.status, headers=headers)(scope, receive, send)


def _echoing(send, request_id: str):
    """Wrap ``send`` so the response start carries ``X-Request-Id``."""

    async def wrapped(message):
        if message["type"] == "http.response.start":
            extra = [(REQUEST_ID_HEADER.encode(), request_id.encode())]
            message = {**message, "headers": list(message.get("headers", [])) + extra}
        await send(message)

    return wrapped
