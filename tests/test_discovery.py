"""RFC 9728: tell MCP hosts where to authenticate, in the format they read.

Cursor, Claude and VS Code look for protected-resource metadata at
`/.well-known/oauth-protected-resource`, and expect a `401` to carry
`WWW-Authenticate: Bearer` with a `resource_metadata` URL. Without both, a host
cannot even start an auth flow; it just sees a refused request.
"""
from __future__ import annotations

import json

import anyio

from core.auth import Principal
from core.config import Config
from core.discovery import ProtectedResource, WELL_KNOWN_PATH
from core.middleware import AuthIdentityMiddleware


def _config(**over) -> Config:
    base = dict(
        trino_host="h", trino_port=8080, trino_user="u", trino_catalog="c",
        max_rows=100, read_only=True, auth_enabled=True, server_name="trino-mcp",
        health_port=3001, jwks_url="https://idp/certs", oidc_issuer="https://idp/realms/data",
        oidc_audience="data", transport="sse", mcp_port=3000, auth_required=True,
        resource_url="https://mcp.example.com",
    )
    base.update(over)
    return Config(**base)


class _Reject:
    def verify(self, headers):
        return None


class _Down:
    async def __call__(self, scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


async def _call_async(app, path="/mcp", headers=None):
    sent = []

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(m):
        sent.append(m)

    scope = {"type": "http", "method": "GET", "path": path,
             "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]}
    await app(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], dict((k.decode(), v.decode()) for k, v in start.get("headers", [])), body


def _call(app, path="/mcp", headers=None):
    return anyio.run(_call_async, app, path, headers)


def test_metadata_document_names_the_issuer_and_the_resource():
    pr = ProtectedResource.from_config(_config())
    doc = pr.document()
    assert doc["resource"] == "https://mcp.example.com"
    assert doc["authorization_servers"] == ["https://idp/realms/data"]
    assert doc["bearer_methods_supported"] == ["header"]


def test_metadata_has_no_authorization_server_when_issuer_is_unknown():
    """Never invent an IdP: an empty list is honest, a guess is a trap."""
    doc = ProtectedResource.from_config(_config(oidc_issuer="")).document()
    assert doc["authorization_servers"] == []


def test_well_known_is_served_without_a_token():
    app = AuthIdentityMiddleware(_Down(), auth_provider=_Reject(), require_auth=True,
                                 discovery=ProtectedResource.from_config(_config()))
    status, headers, body = _call(app, WELL_KNOWN_PATH)
    assert status == 200
    assert headers.get("content-type", "").startswith("application/json")
    assert json.loads(body)["resource"] == "https://mcp.example.com"


def test_401_carries_www_authenticate_with_resource_metadata():
    app = AuthIdentityMiddleware(_Down(), auth_provider=_Reject(), require_auth=True,
                                 discovery=ProtectedResource.from_config(_config()))
    status, headers, _ = _call(app, "/mcp")
    assert status == 401
    www = headers.get("www-authenticate", "")
    assert www.startswith("Bearer ")
    assert 'realm="trino"' in www
    assert f'resource_metadata="https://mcp.example.com{WELL_KNOWN_PATH}"' in www
    assert headers.get("x-reason") == "unauthenticated"


def test_401_without_discovery_still_says_why():
    """Discovery is optional; the reason header is not."""
    app = AuthIdentityMiddleware(_Down(), auth_provider=_Reject(), require_auth=True)
    status, headers, _ = _call(app, "/mcp")
    assert status == 401
    assert headers.get("x-reason") == "unauthenticated"
    assert "www-authenticate" not in headers
