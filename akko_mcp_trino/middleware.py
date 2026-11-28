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

Every refusal also leaves one audit line (``event: "refused"``, see
`akko_mcp_trino.audit.RefusalEvent`) and counts in ``mcp_trino_request_refusals_total``,
by a stable reason: the door's own (``missing_token``, ``invalid_token``, ``wrong_audience``,
``token_expired``... from the verification of the token), an extension's (``client_not_allowed``,
``insufficient_scope``), and those the transport answers behind the door
(``host_not_allowed``, ``payload_too_large``, ``session_invalid``... by their HTTP status,
see ``TRANSPORT_REFUSALS``). The line names the request id and the client's address: the
peer's, or, when the peer is one of ``MCP_TRUSTED_PROXIES``, the first address of
``X-Forwarded-For`` that is not, read from the right. It never holds the token, nor a claim
of a token that was not verified.

IMPORTANT: this is a PURE ASGI middleware, not BaseHTTPMiddleware. The latter
runs the downstream in a separate task, which BREAKS ContextVar propagation to
the tools — the verified caller would never reach them.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import uuid
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

from starlette.responses import JSONResponse

from .audit import (
    REQUEST_ID_HEADER,
    RefusalEvent,
    current_client_ip,
    current_request_id,
    reset_current_client_ip,
    reset_current_request_id,
    set_current_client_ip,
    set_current_request_id,
)
from .auth import INVALID_TOKEN, Principal, TokenVerdict, extract_bearer_token
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
MISSING_TOKEN = "missing_token"
# The refusals the transport answers behind the door, by HTTP status: the reason a line and
# the counter give them. A 404 is a session the server does not know when the request named
# one, a path it does not serve otherwise.
TRANSPORT_REFUSALS = {
    400: "bad_request",
    403: "origin_not_allowed",
    405: "method_not_allowed",
    406: "not_acceptable",
    409: "conflict",
    413: "payload_too_large",
    415: "unsupported_media_type",
    421: "host_not_allowed",
}
SESSION_INVALID = "session_invalid"
NOT_FOUND = "not_found"
TRANSPORT_REFUSED = "transport_refused"
# An extension names its refusals; a reason that is not a short word in lower case is
# counted as this one, so that the counter's labels stay few.
EXTENSION_REFUSED = "extension_refused"
_REASON_RE = re.compile(r"[a-z][a-z0-9_]{0,39}")
_log = logging.getLogger(__name__)


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


def _trusted(address: str, networks: Sequence[Any]) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return any(ip in net for net in networks)


def client_address(scope: Mapping[str, Any], networks: Sequence[Any] = ()) -> str:
    """The address of the client of a request: the peer's, unless the peer is a trusted
    proxy; then the first address of ``X-Forwarded-For`` that is not one, read from the
    right (the hops a trusted proxy appended). A malformed hop stops the reading at the last
    trusted one: an address a client wrote is never taken past a proxy that did not vouch
    for it."""
    client = scope.get("client") or ("",)
    peer = str(client[0] or "")
    if not networks or not _trusted(peer, networks):
        return peer
    hops = [
        hop.strip()
        for name, value in scope.get("headers", [])
        if name.decode("latin-1").lower() == "x-forwarded-for"
        for hop in value.decode("latin-1").split(",")
        if hop.strip()
    ]
    address = peer
    for hop in reversed(hops):
        try:
            ip = ipaddress.ip_address(hop)
        except ValueError:
            return address
        address = str(ip)
        if not any(ip in net for net in networks):
            return address
    return address


def refusal_reason(text: str) -> str:
    """An extension's refusal reason as a line and the counter keep it: as it is when it is a
    short word in lower case, ``extension_refused`` otherwise."""
    return text if _REASON_RE.fullmatch(text) else EXTENSION_REFUSED


def _transport_reason(status: int, scope: Mapping[str, Any], headers: Mapping[str, str]) -> str:
    """The reason of a refusal the transport answered with ``status`` (400 to 499)."""
    if status == 404:
        named = "mcp-session-id" in headers or b"session_id=" in scope.get("query_string", b"")
        return SESSION_INVALID if named else NOT_FOUND
    return TRANSPORT_REFUSALS.get(status, TRANSPORT_REFUSED)


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
        audit: Any = None,
        metrics: Any = None,
        trusted_proxies: Sequence[Any] = (),
    ):
        """Mount on ``app``; ``documents`` are the core's public documents and
        ``metadata_url`` the protected resource metadata a 401 points to; ``extensions``
        add their documents, guards and challenges, which take precedence. ``audit``, when
        given, receives one `RefusalEvent` per refused request, and ``metrics`` count them;
        ``trusted_proxies`` are the networks whose ``X-Forwarded-For`` is read."""
        self.app = app
        self._auth = auth_provider
        self._require = require_auth
        self._extensions = tuple(extensions)
        self._metadata_url = metadata_url
        self._audit = audit
        self._metrics = metrics
        self._proxies = tuple(trusted_proxies)
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
        ip_token = set_current_client_ip(client_address(scope, self._proxies))
        try:
            await self._handle(scope, receive, send, headers)
        finally:
            reset_current_client_ip(ip_token)
            reset_current_request_id(rid_token)

    async def _verdict(self, headers: Mapping[str, str], bearer: str | None) -> TokenVerdict:
        """The verification of the request's token, in a worker thread, with the reason of a
        refusal: the provider's when it says it (``check``), ``invalid_token`` otherwise."""
        if self._auth is None:
            return TokenVerdict(None)
        check = getattr(self._auth, "check", None)
        if check is not None:
            return await asyncio.to_thread(check, headers)
        principal = await asyncio.to_thread(self._auth.verify, headers)
        return TokenVerdict(principal, INVALID_TOKEN if principal is None and bearer else "")

    def _record(self, reason: str, status: int, principal: Principal | None = None) -> None:
        """Write the audit line of a refused request and count it; a failing sink is said in
        the log and never changes the answer."""
        if self._metrics is not None:
            self._metrics.refusals.labels(reason=reason).inc()
        if self._audit is None:
            return
        try:
            self._audit.record(
                RefusalEvent(
                    request_id=current_request_id() or "",
                    reason=reason,
                    status=status,
                    client_ip=current_client_ip(),
                    subject=principal.subject if principal is not None else "",
                    client_id=principal.client_id if principal is not None else "",
                )
            )
        except Exception as exc:  # noqa: BLE001 - the refusal stands whatever the sink does
            _log.error("the audit line of a refusal was not written: %s", type(exc).__name__)

    async def _handle(self, scope, receive, send, headers) -> None:
        bearer = extract_bearer_token(headers)
        admission = Admission(headers=headers, bearer=bearer)
        for ext in self._extensions:
            refusal = ext.before_auth(admission)
            if refusal is not None:
                await self._refuse(refusal, scope, receive, send, refusal_reason(refusal.reason))
                return

        # Verification may read the issuer's JWKS over the network: it runs in a worker
        # thread, so a slow or unreachable identity provider never stalls the event loop.
        verdict = await self._verdict(headers, bearer)
        principal = verdict.principal
        if self._require and principal is None:
            # RFC 6750 §3.1: no error code when no token was sent at all.
            if bearer:
                refusal = Refusal(
                    UNAUTHENTICATED, error="invalid_token", message=INVALID_TOKEN_MESSAGE
                )
                reason = verdict.reason or INVALID_TOKEN
            else:
                refusal = Refusal(UNAUTHENTICATED, message=NO_TOKEN_MESSAGE)
                reason = MISSING_TOKEN
            await self._refuse(refusal, scope, receive, send, reason)
            return

        admission.principal = principal
        for ext in self._extensions:
            refusal = ext.after_auth(admission)
            if refusal is not None:
                reason = refusal_reason(refusal.reason)
                if refusal.reason == UNAUTHENTICATED and principal is None:
                    # An extension that refuses, as the core would, a request the door let
                    # through without a verified token: the audit says why, as the core does.
                    reason = verdict.reason or (INVALID_TOKEN if bearer else MISSING_TOKEN)
                await self._refuse(refusal, scope, receive, send, reason, principal)
                return

        token = set_current_principal(principal)
        # The bearer is bound only once verified: an unverified one never reaches Trino.
        bearer_token = set_current_bearer(bearer if principal is not None else None)
        bound = [(var, var.set(value)) for var, value in admission.bindings]
        status: list[int] = []
        try:
            await self.app(scope, receive, _status_of(send, status))
        finally:
            if status and 400 <= status[0] < 500:
                self._record(_transport_reason(status[0], scope, headers), status[0], principal)
            for var, var_token in reversed(bound):
                var.reset(var_token)
            reset_current_bearer(bearer_token)
            reset_current_principal(token)

    async def _refuse(
        self,
        refusal: Refusal,
        scope,
        receive,
        send,
        reason: str,
        principal: Principal | None = None,
    ) -> None:
        self._record(reason, refusal.status, principal)
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


def _status_of(send, status: list[int]):
    """Wrap ``send`` so that ``status`` receives the status of the response it starts."""

    async def wrapped(message):
        if message["type"] == "http.response.start":
            status.append(int(message["status"]))
        await send(message)

    return wrapped


def _echoing(send, request_id: str):
    """Wrap ``send`` so the response start carries ``X-Request-Id``."""

    async def wrapped(message):
        if message["type"] == "http.response.start":
            extra = [(REQUEST_ID_HEADER.encode(), request_id.encode())]
            message = {**message, "headers": list(message.get("headers", [])) + extra}
        await send(message)

    return wrapped
