"""core.middleware : vérification JWT par requête + propagation ContextVar (ASGI pur).

Prouve que l'identité posée par le middleware est BIEN visible côté endpoint (le piège
BaseHTTPMiddleware/tâche-séparée est évité), et le mode strict (401)."""
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from core.auth import Principal
from core.identity import current_subject
from core.middleware import AuthIdentityMiddleware


class _FakeAuth:
    def __init__(self, principal):
        self._p = principal
        self.seen_headers = None

    def verify(self, headers):
        self.seen_headers = headers
        return self._p


def _client(auth_provider, require_auth=False):
    async def whoami(_request):
        return JSONResponse({"subject": current_subject()})  # lu DANS la requête

    app = Starlette(routes=[Route("/whoami", whoami)])
    app.add_middleware(AuthIdentityMiddleware, auth_provider=auth_provider, require_auth=require_auth)
    return TestClient(app)


def test_no_provider_subject_is_none():
    assert _client(None).get("/whoami").json()["subject"] is None


def test_provider_principal_visible_in_endpoint():
    # le cœur : le ContextVar posé par le middleware ASGI pur arrive jusqu'à l'endpoint
    assert _client(_FakeAuth(Principal(subject="carol"))).get("/whoami").json()["subject"] == "carol"


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
    assert current_subject() is None  # nettoyé après la requête (pas de fuite)


def test_non_http_scope_passes_through():
    import anyio

    events = []

    async def downstream(scope, receive, send):
        events.append(scope["type"])

    mw = AuthIdentityMiddleware(downstream, auth_provider=_FakeAuth(Principal(subject="x")))
    anyio.run(mw, {"type": "lifespan"}, None, None)
    assert events == ["lifespan"]  # non-http → pass-through sans toucher l'identité
