"""Assemble the ASGI application that is actually served.

There is exactly one code path that builds the transport app and mounts the
identity guard on it, whatever the transport. That is deliberate.

The guard once lived only on the ``streamable-http`` branch, while the default
transport was ``sse``: the branch that ran in production had no guard at all,
and every call reached the tools without a verified caller, strict mode or not. A
control that exists in the code, is tested, and is never reached is worse than
no control, because it looks like one. This module makes that shape impossible:
choosing a transport never decides whether the guard is present.
"""

from __future__ import annotations

from typing import Any, Sequence

from .auth import Principal
from .config import Config
from .extensions import Admission, Extension
from .identity import set_current_bearer, set_current_principal
from .middleware import AuthIdentityMiddleware, protected_resource
from .transport_security import transport_security_for

_HTTP_TRANSPORTS = ("sse", "streamable-http")
_TRANSPORTS = _HTTP_TRANSPORTS + ("stdio",)


def transports_supported() -> tuple[str, ...]:
    """Every transport the server can serve: the two HTTP ones and stdio."""
    return _TRANSPORTS


def run_stdio(
    config: Config,
    mcp: Any,
    *,
    auth_provider: Any,
    extensions: Sequence[Extension] = (),
    client: Any = None,
) -> None:
    """Serve over stdio for a local host, as one user.

    There is no request, hence no header: the caller is ``MCP_USER_TOKEN``,
    verified by the same provider a bearer header would be, and bound to the
    whole process; once verified, the token is also the bearer the ``jwt``
    identity mode sends to Trino. In strict mode a missing or invalid token
    refuses to start, which is the stdio equivalent of a 401. The extensions'
    guards hold here too, once, for the whole process. Nothing here writes to
    stdout: stdout is the protocol channel.
    """
    principal = None
    if auth_provider is not None and config.user_token:
        principal = auth_provider.verify({"authorization": f"Bearer {config.user_token}"})
    if config.auth_required and principal is None:
        raise PermissionError(
            "MCP_AUTH_REQUIRED=true and MCP_USER_TOKEN is missing or invalid: "
            "refusing to serve over stdio without a verified identity"
        )

    bearer = config.user_token if principal is not None else None
    if config.trino_identity_mode == "oauth2":
        if client is None:
            raise ValueError("TRINO_IDENTITY_MODE=oauth2 requires the Trino client before serving")
        rows = client.query("SELECT current_user")["rows"]
        subject = rows[0][0] if rows and rows[0] else None
        if not isinstance(subject, str) or not subject.strip():
            raise PermissionError("Trino OAuth2 returned no authenticated user")
        principal = Principal(subject=subject)

    admission = Admission(
        headers={}, bearer=config.user_token or None, principal=principal, stdio=True
    )
    for ext in extensions:
        refusal = ext.before_auth(admission) or ext.after_auth(admission)
        if refusal is not None:
            raise PermissionError(f"MCP_USER_TOKEN refused: {refusal.reason}")
    set_current_principal(principal)
    set_current_bearer(bearer)
    for var, value in admission.bindings:
        var.set(value)
    mcp.run(transport="stdio")


def build_asgi_app(
    config: Config, mcp: Any, *, auth_provider: Any, extensions: Sequence[Extension] = ()
) -> Any:
    """Return the transport app for ``config.transport`` with the guard mounted.

    ``mcp`` is anything exposing ``sse_app(transport_security=)`` and
    ``streamable_http_app(transport_security=)`` —
    a real MCPServer in production, a stand-in under test. An unknown transport
    raises rather than falling back: a typo in configuration must not silently
    pick a transport. The extensions' routes are added to the app, behind the
    same guard as the protocol.
    """
    # Left unset, the SDK assumes a localhost server and answers 421 to any
    # other Host header: a pod reached by its service name would refuse every
    # call. The setting is always explicit here (see `transport_security`).
    security = transport_security_for(config.allowed_hosts)
    if config.transport == "sse":
        app = mcp.sse_app(transport_security=security)
    elif config.transport == "streamable-http":
        app = mcp.streamable_http_app(transport_security=security)
    else:
        raise ValueError(
            f"no ASGI app for MCP_TRANSPORT {config.transport!r}; "
            f"expected one of {_HTTP_TRANSPORTS} (stdio uses run_stdio)"
        )
    for ext in extensions:
        routes = ext.routes()
        if routes:
            app.routes.extend(routes)
    documents, metadata_url = protected_resource(config.resource_url, config.oidc_issuer)
    app.add_middleware(
        AuthIdentityMiddleware,
        auth_provider=auth_provider,
        require_auth=config.auth_required,
        extensions=tuple(extensions),
        documents=documents,
        metadata_url=metadata_url,
    )
    return app
