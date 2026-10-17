"""akko_mcp_trino.middleware: per-request JWT verification and ContextVar propagation (pure ASGI).

Proves the identity set by the middleware IS visible in the endpoint (the
BaseHTTPMiddleware separate-task trap is avoided), and the strict mode (401)."""

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from akko_mcp_trino.auth import Principal
from akko_mcp_trino.identity import current_bearer, current_subject
from akko_mcp_trino.middleware import AuthIdentityMiddleware


class _FakeAuth:
    def __init__(self, principal):
        self._p = principal
        self.seen_headers = None

    def verify(self, headers):
        self.seen_headers = headers
        return self._p


def _client(auth_provider, require_auth=False):
    async def whoami(_request):
        # read INSIDE the request
        return JSONResponse({"subject": current_subject(), "bearer": current_bearer()})

    app = Starlette(routes=[Route("/whoami", whoami)])
    app.add_middleware(
        AuthIdentityMiddleware, auth_provider=auth_provider, require_auth=require_auth
    )
    return TestClient(app)


def test_no_provider_subject_is_none():
    assert _client(None).get("/whoami").json()["subject"] is None


def test_provider_principal_visible_in_endpoint():
    # the point: the ContextVar set by the pure ASGI middleware reaches the endpoint
    assert (
        _client(_FakeAuth(Principal(subject="carol"))).get("/whoami").json()["subject"] == "carol"
    )


def test_provider_receives_request_headers():
    auth = _FakeAuth(Principal(subject="x"))
    _client(auth).get("/whoami", headers={"Authorization": "Bearer tok"})
    assert auth.seen_headers.get("authorization") == "Bearer tok"


def test_require_auth_rejects_unauthenticated_401():
    r = _client(_FakeAuth(None), require_auth=True).get("/whoami")
    assert r.status_code == 401 and r.json()["error"] == "unauthenticated"


def test_require_auth_passes_when_authenticated():
    r = _client(_FakeAuth(Principal(subject="bob")), require_auth=True).get("/whoami")
    assert r.status_code == 200 and r.json()["subject"] == "bob"


def test_contextvar_is_reset_after_request():
    _client(_FakeAuth(Principal(subject="x"))).get("/whoami")
    assert current_subject() is None  # cleared after the request (no leak)


def test_the_bearer_is_bound_only_once_verified_and_cleared_afterwards():
    """The ``jwt`` mode sends Trino the bound bearer: a token the provider did not verify
    is never bound, so it never reaches Trino."""
    headers = {"Authorization": "Bearer eyJ.raw"}
    verified = _client(_FakeAuth(Principal(subject="alice"))).get("/whoami", headers=headers)
    assert verified.json() == {"subject": "alice", "bearer": "eyJ.raw"}
    forged = _client(_FakeAuth(None)).get("/whoami", headers=headers)
    assert forged.json() == {"subject": None, "bearer": None}
    assert current_bearer() is None


def test_non_http_scope_passes_through():
    import anyio

    events = []

    async def downstream(scope, receive, send):
        events.append(scope["type"])

    mw = AuthIdentityMiddleware(downstream, auth_provider=_FakeAuth(Principal(subject="x")))
    anyio.run(mw, {"type": "lifespan"}, None, None)
    assert events == ["lifespan"]  # non-http passes through without touching identity


# ---- request id: assigned or honoured, echoed, visible inside the request ----

from akko_mcp_trino.audit import current_request_id  # noqa: E402


def _client_with_request_id(auth_provider=None):
    async def whoami(_request):
        return JSONResponse({"request_id": current_request_id()})

    app = Starlette(routes=[Route("/whoami", whoami)])
    app.add_middleware(AuthIdentityMiddleware, auth_provider=auth_provider)
    return TestClient(app)


def test_request_id_is_honoured_when_the_edge_sends_one():
    r = _client_with_request_id().get("/whoami", headers={"X-Request-Id": "edge-42"})
    assert r.json()["request_id"] == "edge-42"
    assert r.headers["x-request-id"] == "edge-42"


def test_request_id_is_generated_and_echoed_otherwise():
    r = _client_with_request_id().get("/whoami")
    generated = r.json()["request_id"]
    assert generated and len(generated) >= 16
    assert r.headers["x-request-id"] == generated


def test_request_id_is_on_the_401_too():
    r = _client(_FakeAuth(None), require_auth=True).get(
        "/whoami", headers={"X-Request-Id": "edge-7"}
    )
    assert r.status_code == 401 and r.headers["x-request-id"] == "edge-7"


def test_request_id_is_cleared_after_request():
    _client_with_request_id().get("/whoami", headers={"X-Request-Id": "edge-1"})
    assert current_request_id() is None


def test_a_request_id_unfit_for_a_trino_client_tag_is_replaced():
    """The id travels to Trino as a client tag, a comma-separated header: an id carrying
    a comma would add a tag of the caller's choosing. Only a plain token is honoured."""
    for bad in ("a,b", "x" * 129, "tool=list_catalogs, request_id=forged", "a b"):
        r = _client_with_request_id().get("/whoami", headers={"X-Request-Id": bad})
        rid = r.json()["request_id"]
        assert rid != bad and len(rid) == 32 and r.headers["x-request-id"] == rid
    for good in ("edge-42", "0f6e.A_b:c", "x" * 128):
        r = _client_with_request_id().get("/whoami", headers={"X-Request-Id": good})
        assert r.json()["request_id"] == good


# ---- a clear refusal, and where to get a token (RFC 9728)


def test_a_refusal_says_what_the_client_should_send():
    client = _client(_FakeAuth(None), require_auth=True)
    no_token = client.get("/whoami")
    assert no_token.json()["message"].startswith("this server requires a bearer token")
    bad = client.get("/whoami", headers={"Authorization": "Bearer eyJ.forged"})
    assert bad.status_code == 401
    assert "invalid, expired, or not issued for this server" in bad.json()["message"]
    assert "eyJ.forged" not in bad.text


def test_protected_resource_metadata_names_the_server_and_its_provider():
    from akko_mcp_trino.middleware import protected_resource

    assert protected_resource("", "https://idp") == ({}, "")
    paths, url = protected_resource("https://mcp.example.com/mcp", "https://idp/realms/r")
    doc = {
        "resource": "https://mcp.example.com/mcp",
        "authorization_servers": ["https://idp/realms/r"],
        "bearer_methods_supported": ["header"],
    }
    assert paths == {
        "/.well-known/oauth-protected-resource": doc,
        "/.well-known/oauth-protected-resource/mcp": doc,
    }
    assert url == "https://mcp.example.com/.well-known/oauth-protected-resource/mcp"
    root, root_url = protected_resource("https://mcp.example.com/", "")
    assert list(root) == ["/.well-known/oauth-protected-resource"]
    assert root["/.well-known/oauth-protected-resource"]["authorization_servers"] == []
    assert root_url == "https://mcp.example.com/.well-known/oauth-protected-resource"


def test_the_core_serves_its_metadata_and_points_to_it_in_the_challenge():
    from akko_mcp_trino.middleware import protected_resource

    async def ok(_request):
        return JSONResponse({})

    paths, url = protected_resource("https://mcp.example.com/mcp", "https://idp")
    app = Starlette(routes=[Route("/mcp", ok, methods=["POST"])])
    app.add_middleware(
        AuthIdentityMiddleware,
        auth_provider=_FakeAuth(None),
        require_auth=True,
        documents=paths,
        metadata_url=url,
    )
    client = TestClient(app)
    meta = client.get("/.well-known/oauth-protected-resource/mcp")
    assert meta.status_code == 200 and meta.json()["resource"] == "https://mcp.example.com/mcp"
    assert client.post("/mcp").headers["www-authenticate"] == f'Bearer resource_metadata="{url}"'
    bad = client.post("/mcp", headers={"Authorization": "Bearer x"})
    assert bad.headers["www-authenticate"] == (
        f'Bearer resource_metadata="{url}", error="invalid_token"'
    )


def test_an_extensions_challenge_and_documents_take_precedence_over_the_cores():
    from akko_mcp_trino.extensions import Extension, Refusal

    class _Ext(Extension):
        def public_documents(self):
            return {"/.well-known/oauth-protected-resource": {"resource": "theirs"}}

        def challenge(self, refusal):
            return 'Bearer realm="theirs"'

        def after_auth(self, admission):
            return Refusal("blocked", status=403)

    async def ok(_request):
        return JSONResponse({})

    app = Starlette(routes=[Route("/mcp", ok)])
    app.add_middleware(
        AuthIdentityMiddleware,
        auth_provider=_FakeAuth(None),
        require_auth=True,
        extensions=[_Ext()],
        documents={"/.well-known/oauth-protected-resource": {"resource": "core"}},
        metadata_url="https://mcp/.well-known/oauth-protected-resource",
    )
    client = TestClient(app)
    assert client.get("/.well-known/oauth-protected-resource").json() == {"resource": "theirs"}
    assert client.get("/mcp").headers["www-authenticate"] == 'Bearer realm="theirs"'


def test_the_core_challenges_only_its_own_refusal():
    from akko_mcp_trino.extensions import Extension, Refusal

    class _Blocks(Extension):
        def after_auth(self, admission):
            return Refusal("blocked", status=403)

    async def ok(_request):
        return JSONResponse({})

    app = Starlette(routes=[Route("/mcp", ok)])
    app.add_middleware(
        AuthIdentityMiddleware,
        auth_provider=_FakeAuth(Principal(subject="u")),
        require_auth=True,
        extensions=[_Blocks()],
        metadata_url="https://mcp/.well-known/oauth-protected-resource",
    )
    refused = TestClient(app).get("/mcp")
    assert refused.status_code == 403 and "www-authenticate" not in refused.headers
    assert refused.json() == {"error": "blocked"}
