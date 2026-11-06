"""token-policy: a verified token must also name an allowed client and carry the required scopes.

A foreign client gets the 401 of an invalid token and is never told which scope would let it
in; a missing scope gets 403 insufficient_scope with the scope to ask for (RFC 6750 §3.1).
"""

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from akko_mcp_trino.auth import Principal
from akko_mcp_trino.extensions import Admission
from akko_mcp_trino.middleware import AuthIdentityMiddleware, protected_resource
from akko_mcp_trino.modules import token_policy
from tests.tool_doubles import core_config

RESOURCE = "https://mcp.example.com/mcp"
ISSUER = "https://idp.example.com/realms/data"
METADATA = "https://mcp.example.com/.well-known/oauth-protected-resource/mcp"


def _policy(clients="mcp-host", scopes="data:read data:query", **over):
    config = core_config(resource_url=RESOURCE, oidc_issuer=ISSUER, **over)
    env = {"MCP_ALLOWED_CLIENTS": clients, "MCP_REQUIRED_SCOPES": scopes}
    return token_policy.create(config, env)


class _Auth:
    def __init__(self, principal):
        self.principal = principal

    def verify(self, headers):
        return self.principal


def _door(principal, policy=None):
    """The core's door with the policy, in front of an endpoint that answers ok."""

    async def ok(_request):
        return JSONResponse({"ok": True})

    policy = policy or _policy()
    documents, metadata_url = protected_resource(RESOURCE, ISSUER)
    app = Starlette(routes=[Route("/mcp", ok, methods=["GET", "POST"])])
    app.add_middleware(
        AuthIdentityMiddleware,
        auth_provider=_Auth(principal),
        require_auth=True,
        extensions=[policy],
        documents=documents,
        metadata_url=metadata_url,
    )
    return TestClient(app)


def _caller(client="mcp-host", scopes=("data:read", "data:query")):
    return Principal(subject="alice", client_id=client, scopes=tuple(scopes))


def test_an_allowed_client_with_every_scope_passes():
    assert _door(_caller()).get("/mcp", headers={"Authorization": "Bearer t"}).json() == {
        "ok": True
    }


@pytest.mark.parametrize("client", ["other-app", "", "MCP-HOST", "mcp-host "])
def test_a_foreign_client_gets_the_401_of_an_invalid_token(client):
    r = _door(_caller(client=client)).get("/mcp", headers={"Authorization": "Bearer t"})
    assert r.status_code == 401
    assert r.headers["x-reason"] == "client_not_allowed"
    assert r.headers["www-authenticate"] == (
        f'Bearer resource_metadata="{METADATA}", error="invalid_token"'
    )


@pytest.mark.parametrize("scopes", [("data:read",), (), ("data:read", "DATA:QUERY")])
def test_a_missing_scope_gets_403_insufficient_scope_with_the_scope_to_ask(scopes):
    r = _door(_caller(scopes=scopes)).get("/mcp", headers={"Authorization": "Bearer t"})
    assert r.status_code == 403
    assert r.headers["x-reason"] == "insufficient_scope"
    assert r.headers["www-authenticate"] == (
        f'Bearer resource_metadata="{METADATA}", error="insufficient_scope", '
        'scope="data:read data:query"'
    )


def test_a_foreign_client_is_never_told_which_scope_would_let_it_in():
    r = _door(_caller(client="other-app", scopes=())).get(
        "/mcp", headers={"Authorization": "Bearer t"}
    )
    assert r.status_code == 401, "the client is checked before the scopes"
    said = " ".join([r.text, *r.headers.values()])
    assert "data:read" not in said and "data:query" not in said and "scope=" not in said


def test_a_request_without_a_verified_caller_is_refused_as_the_core_refuses_it():
    policy = _policy()
    assert policy.after_auth(Admission(headers={}, principal=None)).reason == "unauthenticated"


def test_the_metadata_names_the_supported_scopes():
    r = _door(_caller()).get("/.well-known/oauth-protected-resource/mcp")
    assert r.json() == {
        "resource": RESOURCE,
        "authorization_servers": [ISSUER],
        "bearer_methods_supported": ["header"],
        "scopes_supported": ["data:read", "data:query"],
    }


def test_clients_alone_or_scopes_alone_are_each_enforced():
    clients_only = _policy(scopes="")
    assert clients_only.after_auth(Admission(headers={}, principal=_caller(scopes=()))) is None
    assert (
        "scopes_supported"
        not in clients_only.public_documents()["/.well-known/oauth-protected-resource"]
    )
    scopes_only = _policy(clients="")
    assert scopes_only.after_auth(Admission(headers={}, principal=_caller(client="any"))) is None


def test_a_challenge_without_a_resource_url_names_no_metadata():
    config = core_config()
    policy = token_policy.create(config, {"MCP_REQUIRED_SCOPES": "data:read"})
    refusal = policy.after_auth(Admission(headers={}, principal=_caller(scopes=())))
    assert policy.challenge(refusal) == 'Bearer error="insufficient_scope", scope="data:read"'
    assert policy.public_documents() == {}


def test_a_refusal_of_the_core_keeps_the_core_challenge():
    from akko_mcp_trino.extensions import Refusal

    assert _policy().challenge(Refusal("unauthenticated")) is None


@pytest.mark.parametrize("clients, scopes", [("", ""), (" , ", "  "), (None, None)])
def test_a_policy_with_nothing_to_enforce_refuses_to_start(clients, scopes):
    env = {
        k: v
        for k, v in {"MCP_ALLOWED_CLIENTS": clients, "MCP_REQUIRED_SCOPES": scopes}.items()
        if v is not None
    }
    with pytest.raises(ValueError, match="nothing to enforce"):
        token_policy.create(core_config(), env)


def test_the_check_lines_name_clients_scopes_and_the_claims_read():
    lines = _policy(clients="a, b,a", scopes="x y").check_lines()
    assert lines == [
        "token-policy: allowed clients a, b (from claims azp,client_id); "
        "required scopes x, y (from claims scope)"
    ]


@pytest.mark.parametrize(
    "variable, value",
    [
        ("MCP_REQUIRED_SCOPES", 'data"query'),
        ("MCP_REQUIRED_SCOPES", "data\\query"),
        ("MCP_REQUIRED_SCOPES", "données"),
        ("MCP_ALLOWED_CLIENTS", 'host"a'),
        ("MCP_ALLOWED_CLIENTS", "hôte"),
    ],
)
def test_a_scope_or_a_client_outside_the_scope_token_characters_refuses_to_start(variable, value):
    """RFC 6749 §3.3: a scope-token is %x21 / %x23-5B / %x5D-7E, so that it never breaks the
    quoted string of a challenge."""
    with pytest.raises(ValueError, match=variable):
        token_policy.create(core_config(), {variable: value})


@pytest.mark.parametrize("bearer", [None, "not-a-verified-token"])
def test_without_a_verified_token_the_answer_is_the_core_401_never_403(bearer):
    """With MCP_AUTH_REQUIRED=false, a request without a verified caller reaches the policy:
    it answers the core's 401 and challenge (RFC 6750 §3.1), never insufficient_scope."""

    async def ok(_request):
        return JSONResponse({"ok": True})

    documents, metadata_url = protected_resource(RESOURCE, ISSUER)
    app = Starlette(routes=[Route("/mcp", ok)])
    app.add_middleware(
        AuthIdentityMiddleware,
        auth_provider=_Auth(None),
        require_auth=False,
        extensions=[_policy(clients="")],
        documents=documents,
        metadata_url=metadata_url,
    )
    headers = {"Authorization": f"Bearer {bearer}"} if bearer else {}
    r = TestClient(app).get("/mcp", headers=headers)
    assert r.status_code == 401 and r.headers["x-reason"] == "unauthenticated"
    error = ', error="invalid_token"' if bearer else ""
    assert r.headers["www-authenticate"] == f'Bearer resource_metadata="{METADATA}"{error}'
    assert "scope" not in r.headers["www-authenticate"]
