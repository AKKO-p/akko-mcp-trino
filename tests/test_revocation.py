"""Optional revocation check: a token can be disabled before it expires.

Signature and `exp` say a token *was* valid; only the issuer knows whether it
still is. RFC 7662 introspection asks. It costs a call per request, so the
verdict is cached by `jti` for a short TTL, and it fails closed: an issuer that
cannot answer means the request is refused, not waved through. Off unless
`MCP_INTROSPECTION_URL` is set.
"""
from __future__ import annotations

import anyio
import pytest

from core.auth import Principal
from core.middleware import AuthIdentityMiddleware
from core.revocation import IntrospectionCheck, IntrospectionError


class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class _Issuer:
    """Stands in for the introspection endpoint: records calls, answers `active`."""

    def __init__(self, active=True, fail=False):
        self.active, self.fail, self.calls = active, fail, []

    def __call__(self, url, *, data, auth, timeout):
        self.calls.append((url, data, auth))
        if self.fail:
            raise ConnectionError("issuer down")
        return {"active": self.active}


def _check(issuer, ttl=30, clock=None):
    return IntrospectionCheck("https://idp/introspect", "mcp", "s3cret", ttl_seconds=ttl,
                              post=issuer, clock=clock or _Clock())


def test_from_env_is_disabled_without_url():
    assert IntrospectionCheck.from_env({}) is None


def test_from_env_requires_client_credentials():
    with pytest.raises(ValueError):
        IntrospectionCheck.from_env({"MCP_INTROSPECTION_URL": "https://idp/introspect"})


def test_active_token_is_accepted_and_the_issuer_is_asked_once_per_ttl():
    issuer = _Issuer(active=True)
    clock = _Clock()
    check = _check(issuer, ttl=30, clock=clock)
    assert check.is_active("tok", token_id="jti-1") is True
    assert check.is_active("tok", token_id="jti-1") is True
    assert len(issuer.calls) == 1
    clock.now += 31
    assert check.is_active("tok", token_id="jti-1") is True
    assert len(issuer.calls) == 2


def test_issuer_is_called_with_the_token_and_client_credentials():
    issuer = _Issuer()
    _check(issuer).is_active("tok", token_id="jti-1")
    url, data, auth = issuer.calls[0]
    assert url == "https://idp/introspect"
    assert data == {"token": "tok", "token_type_hint": "access_token"}
    assert auth == ("mcp", "s3cret")


def test_revoked_token_is_refused_and_cached():
    issuer = _Issuer(active=False)
    check = _check(issuer)
    assert check.is_active("tok", token_id="jti-1") is False
    assert check.is_active("tok", token_id="jti-1") is False
    assert len(issuer.calls) == 1


def test_a_token_without_jti_is_asked_every_time():
    issuer = _Issuer()
    check = _check(issuer)
    check.is_active("tok", token_id="")
    check.is_active("tok", token_id="")
    assert len(issuer.calls) == 2


def test_issuer_failure_raises_so_the_guard_fails_closed():
    with pytest.raises(IntrospectionError):
        _check(_Issuer(fail=True)).is_active("tok", token_id="jti-1")


# ---- through the guard ----

class _Auth:
    def verify(self, headers):
        return Principal(subject="alice", token_id="jti-1") if "authorization" in headers else None


class _Down:
    def __init__(self):
        self.hits = 0

    async def __call__(self, scope, receive, send):
        self.hits += 1
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


async def _call_async(app, headers=None):
    sent = []

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(m):
        sent.append(m)

    scope = {"type": "http", "method": "POST", "path": "/mcp",
             "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]}
    await app(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    return start["status"], dict((k.decode(), v.decode()) for k, v in start.get("headers", []))


def _call(app, headers=None):
    return anyio.run(_call_async, app, headers)


def test_guard_refuses_a_revoked_token_with_401_revoked():
    down = _Down()
    app = AuthIdentityMiddleware(down, auth_provider=_Auth(), require_auth=True,
                                 revocation=_check(_Issuer(active=False)))
    status, headers = _call(app, {"Authorization": "Bearer tok"})
    assert status == 401 and headers["x-reason"] == "revoked"
    assert down.hits == 0


def test_guard_passes_an_active_token():
    down = _Down()
    app = AuthIdentityMiddleware(down, auth_provider=_Auth(), require_auth=True,
                                 revocation=_check(_Issuer(active=True)))
    assert _call(app, {"Authorization": "Bearer tok"})[0] == 200 and down.hits == 1


def test_guard_fails_closed_when_the_issuer_is_down():
    down = _Down()
    app = AuthIdentityMiddleware(down, auth_provider=_Auth(), require_auth=True,
                                 revocation=_check(_Issuer(fail=True)))
    status, headers = _call(app, {"Authorization": "Bearer tok"})
    assert status == 503 and headers["x-reason"] == "introspection_unavailable"
    assert down.hits == 0


def test_guard_does_not_introspect_when_there_is_no_principal():
    """Auth not required and no token: nothing to introspect, request passes."""
    issuer = _Issuer()
    app = AuthIdentityMiddleware(_Down(), auth_provider=_Auth(), require_auth=False,
                                 revocation=_check(issuer))
    assert _call(app, {})[0] == 200 and issuer.calls == []


def test_default_post_uses_httpx_form_post_with_basic_auth(monkeypatch):
    """The real transport: form-encoded POST, client credentials as Basic auth."""
    import httpx

    from core import revocation

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = request.content.decode()
        seen["auth"] = request.headers.get("authorization", "")
        return httpx.Response(200, json={"active": True})

    real_post = httpx.post

    def post_via_mock(url, **kw):
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            return client.post(url, **kw)

    monkeypatch.setattr(httpx, "post", post_via_mock)
    assert revocation._default_post("https://idp/introspect", data={"token": "t", "token_type_hint": "access_token"},
                                    auth=("mcp", "s"), timeout=1.0) == {"active": True}
    assert seen["url"] == "https://idp/introspect"
    assert "token=t" in seen["body"] and "token_type_hint=access_token" in seen["body"]
    assert seen["auth"].startswith("Basic ")
    assert httpx.post is not real_post  # the patch was in effect for the call above
