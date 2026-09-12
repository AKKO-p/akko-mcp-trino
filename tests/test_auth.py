"""core.auth: unverified JWT helpers, Principal, JwksJwtAuth (real crypto), build_auth."""
import base64
import json
import time

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from core import auth
from core.config import Config


# --- test RSA key (generated once) + fake JWKS (no network) ---
_PRIV = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_PUB = _PRIV.public_key()
_OTHER = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_ISS = "https://kc.example/realms/akko"
_AUD = "akko-mcp"


def _sign(claims=None, key=None, iss=_ISS, aud=_AUD, exp_delta=3600):
    payload = {"iss": iss, "aud": aud, "exp": int(time.time()) + exp_delta, **(claims or {})}
    return pyjwt.encode(payload, key or _PRIV, algorithm="RS256")


class _FakeJwks:
    def get_signing_key_from_jwt(self, token):
        return type("K", (), {"key": _PUB})()


def _jwks_provider():
    return auth.JwksJwtAuth("https://kc.example/jwks", _ISS, _AUD, jwks_client=_FakeJwks())


def _bearer(tok):
    return {"authorization": f"Bearer {tok}"}


def _unsafe_jwt(payload: dict) -> str:
    body = base64.b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"header.{body}.sig"


# ---- unverified JWT helpers (legacy) ----
def test_decode_jwt_unsafe_reads_payload_and_malformed():
    assert auth.decode_jwt_unsafe(_unsafe_jwt({"a": 1})) == {"a": 1}
    assert auth.decode_jwt_unsafe("not-a-jwt") is None


def test_validate_token_roles_and_none():
    assert auth.validate_token(_unsafe_jwt({"realm_access": {"roles": ["r"]}})) == ["r"]
    assert auth.validate_token(None) is None
    assert auth.validate_token(_unsafe_jwt({"realm_access": {}})) is None
    assert auth.validate_token(_unsafe_jwt({"realm_access": {"roles": "x"}})) is None
    assert auth.validate_token("garbage-not-a-jwt") is None


def test_extract_bearer_token():
    assert auth.extract_bearer_token({"authorization": "Bearer abc"}) == "abc"
    assert auth.extract_bearer_token({"Authorization": "bearer xyz"}) == "xyz"
    assert auth.extract_bearer_token({"authorization": "Basic abc"}) is None
    assert auth.extract_bearer_token({}) is None


# ---- Principal ----
def test_principal_defaults():
    p = auth.Principal(subject="alice")
    assert p.subject == "alice" and p.roles == [] and p.email == ""


# ---- UnverifiedJwtAuth (repli) ----
def test_unverified_provider_returns_principal():
    p = auth.UnverifiedJwtAuth().verify(_bearer(_unsafe_jwt(
        {"preferred_username": "dave_steward", "email": "d@x", "realm_access": {"roles": ["akko-steward"]}})))
    assert p == auth.Principal(subject="dave_steward", roles=["akko-steward"], email="d@x")


def test_unverified_provider_no_token_or_bad():
    assert auth.UnverifiedJwtAuth().verify({}) is None
    assert auth.UnverifiedJwtAuth().verify(_bearer("garbage")) is None


# ---- JwksJwtAuth (real verification) ----
def test_jwks_valid_token_returns_principal():
    tok = _sign({"preferred_username": "mcp-user", "realm_access": {"roles": ["akko-viewer"]}})
    p = _jwks_provider().verify(_bearer(tok))
    assert p == auth.Principal(subject="mcp-user", roles=["akko-viewer"])


def test_jwks_falls_back_to_sub_when_no_preferred_username():
    p = _jwks_provider().verify(_bearer(_sign({"sub": "uuid-123"})))
    assert p.subject == "uuid-123"


def test_jwks_rejects_bad_signature():
    tok = _sign({"preferred_username": "x"}, key=_OTHER)  # signed with another key
    assert _jwks_provider().verify(_bearer(tok)) is None


def test_jwks_rejects_wrong_issuer():
    assert _jwks_provider().verify(_bearer(_sign(iss="https://evil/realms/x"))) is None


def test_jwks_rejects_wrong_audience():
    assert _jwks_provider().verify(_bearer(_sign(aud="other-aud"))) is None


def test_jwks_rejects_expired():
    assert _jwks_provider().verify(_bearer(_sign(exp_delta=-10))) is None


def test_jwks_no_token_returns_none():
    assert _jwks_provider().verify({}) is None


# ---- build_auth (provider selection) ----
def _cfg(auth_enabled, jwks_url=""):
    return Config(
        trino_host="h", trino_port=8080, trino_user="u", trino_catalog="c",
        max_rows=100, read_only=True, auth_enabled=auth_enabled,
        server_name="x", health_port=3001, jwks_url=jwks_url,
        oidc_issuer=_ISS, oidc_audience=_AUD, transport="sse", mcp_port=3000, auth_required=False,
    )


def test_build_auth_disabled_returns_none():
    assert auth.build_auth(_cfg(False)) is None


def test_build_auth_jwks_when_url_set():
    provider = auth.build_auth(_cfg(True, jwks_url="https://kc.example/jwks"))
    assert isinstance(provider, auth.JwksJwtAuth)


def test_build_auth_fails_closed_when_no_jwks():
    # Auth enabled without a JWKS URL is a blocking error. There is no fallback to
    # UnverifiedJwtAuth any more: it accepted any forged JWT, which is fail-open.
    with pytest.raises(ValueError):
        auth.build_auth(_cfg(True))
