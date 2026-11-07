"""akko_mcp_trino.auth: unverified JWT helpers, Principal, JwksJwtAuth (real crypto), build_auth."""

import base64
import dataclasses
import json
import time

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from akko_mcp_trino import auth
from akko_mcp_trino.config import Config

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
    p = auth.UnverifiedJwtAuth().verify(
        _bearer(
            _unsafe_jwt(
                {
                    "sub": "dave_steward",
                    "email": "d@x",
                    "realm_access": {"roles": ["data-steward"]},
                }
            )
        )
    )
    assert p == auth.Principal(subject="dave_steward", roles=["data-steward"], email="d@x")


def test_unverified_provider_no_token_or_bad():
    assert auth.UnverifiedJwtAuth().verify({}) is None
    assert auth.UnverifiedJwtAuth().verify(_bearer("garbage")) is None


# ---- JwksJwtAuth (real verification) ----
def test_jwks_valid_token_returns_principal():
    tok = _sign({"sub": "mcp-user", "realm_access": {"roles": ["data-viewer"]}})
    p = _jwks_provider().verify(_bearer(tok))
    assert p == auth.Principal(subject="mcp-user", roles=["data-viewer"])


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
    """Expired beyond the 30 s clock leeway."""
    assert _jwks_provider().verify(_bearer(_sign(exp_delta=-60))) is None


def test_jwks_no_token_returns_none():
    assert _jwks_provider().verify({}) is None


# ---- build_auth (provider selection) ----
def _cfg(auth_enabled, jwks_url="", **kw):
    return Config(
        **kw,
        trino_host="h",
        trino_identity_mode="service",
        query_timeout_seconds=0.0,
        call_timeout_seconds=0.0,
        trino_port=8080,
        trino_user="u",
        trino_catalog="c",
        max_rows=100,
        read_only=True,
        auth_enabled=auth_enabled,
        server_name="x",
        health_port=3001,
        jwks_url=jwks_url,
        oidc_issuer=_ISS,
        oidc_audience=_AUD,
        transport="sse",
        mcp_port=3000,
        auth_required=False,
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


def test_an_empty_issuer_refuses_every_signed_token():
    # The behaviour build_auth now refuses to start with: PyJWT compares "" to iss, so a
    # token from the right issuer, signed by the right key, is refused.
    p = auth.JwksJwtAuth("https://kc.example/jwks", "", _AUD, jwks_client=_FakeJwks())
    assert p.verify(_bearer(_sign())) is None


def test_build_auth_refuses_an_empty_issuer():
    # An empty issuer is compared to iss by PyJWT, so the server would start and then
    # refuse every caller; it must refuse to start instead.
    cfg = dataclasses.replace(_cfg(True, jwks_url="https://kc.example/jwks"), oidc_issuer="")
    with pytest.raises(ValueError, match="MCP_OIDC_ISSUER is empty"):
        auth.build_auth(cfg)


def test_token_id_is_carried_from_jti_for_audit():
    p = _jwks_provider().verify(_bearer(_sign({"sub": "u", "jti": "tok-123"})))
    assert p.token_id == "tok-123"


def test_token_id_is_empty_when_the_token_has_no_jti():
    assert _jwks_provider().verify(_bearer(_sign({"sub": "u"}))).token_id == ""


def test_a_token_issued_one_second_in_the_future_is_accepted_within_leeway():
    """Found live on 13 September 2026: the issuer's clock was one second ahead
    of the server's, every fresh token had `iat` in the future, and one request
    in three got a 401. Clocks drift; a leeway of a few seconds is standard."""
    tok = _sign({"sub": "u", "iat": int(time.time()) + 2, "nbf": int(time.time()) + 2})
    assert _jwks_provider().verify(_bearer(tok)) is not None


def test_leeway_does_not_resurrect_an_expired_token():
    tok = _sign({"sub": "u"}, exp_delta=-120)
    assert _jwks_provider().verify(_bearer(tok)) is None


def test_leeway_is_configurable_and_bounded(monkeypatch):
    provider = auth.JwksJwtAuth(
        "https://kc/jwks", _ISS, _AUD, jwks_client=_FakeJwks(), leeway_seconds=0
    )
    tok = _sign({"sub": "u", "iat": int(time.time()) + 5})
    assert provider.verify(_bearer(tok)) is None


def test_refusal_reason_is_logged_without_the_token(caplog):
    import logging

    tok = _sign({"sub": "u"}, key=_OTHER)
    with caplog.at_level(logging.INFO, logger="akko_mcp_trino.auth"):
        assert _jwks_provider().verify(_bearer(tok)) is None
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "refused" in joined and tok not in joined


# ---- which application asked for the token, and with which scopes


def test_principal_carries_the_client_that_asked_for_the_token():
    """Keycloak names the application in `azp`; RFC 9068 in `client_id`."""
    p = _jwks_provider().verify(_bearer(_sign({"sub": "u", "azp": "host-a"})))
    assert p.client_id == "host-a"
    p = _jwks_provider().verify(_bearer(_sign({"sub": "u", "client_id": "host-b"})))
    assert p.client_id == "host-b"
    assert _jwks_provider().verify(_bearer(_sign({"sub": "u"}))).client_id == ""


def test_principal_carries_the_granted_scopes():
    p = _jwks_provider().verify(_bearer(_sign({"sub": "u", "scope": "openid data:query"})))
    assert p.scopes == ("openid", "data:query")
    p = _jwks_provider().verify(_bearer(_sign({"sub": "u", "scope": ["a", "b"]})))
    assert p.scopes == ("a", "b")
    assert _jwks_provider().verify(_bearer(_sign({"sub": "u"}))).scopes == ()
    assert _jwks_provider().verify(_bearer(_sign({"sub": "u", "scope": 3}))).scopes == ()


def test_jwks_accepts_any_of_several_audiences():
    provider = auth.JwksJwtAuth(
        "https://kc/jwks", _ISS, ["data", "https://mcp.example.com/mcp"], jwks_client=_FakeJwks()
    )
    assert provider.verify(_bearer(_sign({"sub": "u"}, aud="data"))) is not None
    assert provider.verify(_bearer(_sign({"sub": "u"}, aud="https://mcp.example.com/mcp")))
    assert provider.verify(_bearer(_sign({"sub": "u"}, aud="other"))) is None


def test_an_extensions_audiences_are_accepted_after_the_configured_one():
    """An extension may accept more audiences (a server URL, for example); the configured
    one comes first, and none is listed twice."""
    cfg = _cfg(True)
    assert auth.accepted_audiences(cfg) == [_AUD]
    assert auth.accepted_audiences(cfg, ["https://mcp.example.com/mcp", _AUD]) == [
        _AUD,
        "https://mcp.example.com/mcp",
    ]
    provider = auth.build_auth(
        Config(**{**cfg.__dict__, "jwks_url": "https://kc/jwks"}), ["https://mcp/mcp"]
    )
    assert provider._audience == [_AUD, "https://mcp/mcp"]
    assert auth.accepted_audiences(_cfg(True)) == [_AUD]
    nothing = Config(**{**_cfg(True).__dict__, "oidc_audience": ""})
    assert auth.accepted_audiences(nothing) == []


def test_no_accepted_audience_refuses_every_token():
    """Nothing configured is not "any audience": fail closed, as before."""
    provider = auth.JwksJwtAuth("https://kc/jwks", _ISS, [], jwks_client=_FakeJwks())
    assert provider.verify(_bearer(_sign({"sub": "u"}))) is None


# ---- a token is accepted only when it was issued for this server (RFC 8707)


def _built(**over):
    cfg = Config(**{**_cfg(True, jwks_url="https://kc/jwks").__dict__, **over})
    provider = auth.build_auth(cfg)
    provider._client = _FakeJwks()
    return provider


def test_a_token_issued_for_trino_is_refused_by_this_server():
    """The audience Trino accepts is never one this server accepts: a token issued for
    Trino does not open this server, even when it also names this server."""
    provider = _built(token_exchange_audience="trino")
    assert provider.verify(_bearer(_sign({"sub": "u"}))) is not None
    assert provider.verify(_bearer(_sign({"sub": "u"}, aud="trino"))) is None
    assert provider.verify(_bearer(_sign({"sub": "u"}, aud=[_AUD, "trino"]))) is None


def test_a_refused_audience_is_logged_without_the_token(caplog):
    import logging

    tok = _sign({"sub": "u"}, aud=[_AUD, "trino"])
    with caplog.at_level(logging.INFO, logger="akko_mcp_trino.auth"):
        assert _built(token_exchange_audience="trino").verify(_bearer(tok)) is None
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "issued for another resource" in joined and tok not in joined


def test_the_servers_resource_url_is_an_accepted_audience():
    provider = _built(oidc_audience="", resource_url="https://mcp.example.com/mcp")
    assert provider.verify(_bearer(_sign({"sub": "u"}, aud="https://mcp.example.com/mcp")))
    assert provider.verify(_bearer(_sign({"sub": "u"}))) is None


def test_a_server_that_would_accept_a_trino_token_refuses_to_start():
    with pytest.raises(ValueError, match="both accepted from callers and requested for Trino"):
        auth.build_auth(
            Config(
                **{
                    **_cfg(True, jwks_url="https://kc/jwks").__dict__,
                    "token_exchange_audience": _AUD,
                }
            )
        )


def test_a_server_with_no_audience_of_its_own_refuses_to_start():
    with pytest.raises(ValueError, match="no token audience is set"):
        auth.build_auth(
            Config(**{**_cfg(True, jwks_url="https://kc/jwks").__dict__, "oidc_audience": ""})
        )


def _rs512(claims=None):
    payload = {"iss": _ISS, "aud": _AUD, "exp": int(time.time()) + 3600, **(claims or {})}
    return pyjwt.encode(payload, _PRIV, algorithm="RS512")


def _built_for(**kw):
    provider = auth.build_auth(_cfg(True, jwks_url="https://kc.example/jwks", **kw))
    provider._client = _FakeJwks()  # the keys, without the network
    return provider


def test_rs256_only_by_default_so_an_rs512_token_is_refused():
    provider = _built_for()
    assert provider.verify(_bearer(_sign({"sub": "u"}))).subject == "u"
    assert provider.verify(_bearer(_rs512({"sub": "u"}))) is None


def test_mcp_jwt_algorithms_accepts_the_algorithms_it_names():
    """An identity provider that signs with RS512, or rotates from RS256 to RS512."""
    provider = _built_for(jwt_algorithms="rs256, RS512")
    assert provider.verify(_bearer(_sign({"sub": "u"}))).subject == "u"
    assert provider.verify(_bearer(_rs512({"sub": "v"}))).subject == "v"


@pytest.mark.parametrize("value", ["HS256", "none", "RS256,HS512", "", " , "])
def test_a_shared_secret_algorithm_none_or_nothing_refuses_to_start(value):
    from akko_mcp_trino.config import parse_jwt_algorithms

    with pytest.raises(ValueError, match="MCP_JWT_ALGORITHMS"):
        parse_jwt_algorithms(value)
    assert any("MCP_JWT_ALGORITHMS" in p for p in _cfg(True, jwt_algorithms=value).problems())


def test_mcp_jwt_user_claim_names_the_user_from_that_claim_only():
    """An identity provider that puts the user in sub (the OpenID Connect standard) and
    something else in preferred_username: MCP_JWT_USER_CLAIM=sub picks sub."""
    provider = _built_for(jwt_user_claim="sub")
    p = provider.verify(_bearer(_sign({"sub": "A12345", "preferred_username": "Jean D."})))
    assert p.subject == "A12345"


def test_a_token_without_the_named_claim_is_refused_not_read_from_another():
    provider = _built_for(jwt_user_claim="upn")
    assert provider.verify(_bearer(_sign({"sub": "A12345", "preferred_username": "x"}))) is None
    assert provider.verify(_bearer(_sign({"upn": ""}))) is None
    assert provider.verify(_bearer(_sign({"upn": ["A12345"]}))) is None
    assert provider.verify(_bearer(_sign({"upn": "A12345"}))).subject == "A12345"


def test_sub_names_the_user_by_default_the_openid_connect_standard():
    """sub is the stable, unique identifier OpenID Connect gives the user: the default. A
    token without it is refused, never read from preferred_username."""
    provider = _built_for()
    assert provider.verify(_bearer(_sign({"sub": "s", "preferred_username": "p"}))).subject == "s"
    assert provider.verify(_bearer(_sign({"preferred_username": "p"}))) is None


def test_an_issuer_whose_sub_is_an_opaque_id_names_preferred_username():
    """Keycloak gives sub a UUID the access rules do not know."""
    provider = _built_for(jwt_user_claim="preferred_username")
    p = provider.verify(_bearer(_sign({"sub": "5f2c-uuid", "preferred_username": "carol"})))
    assert p.subject == "carol"


def test_an_empty_setting_falls_back_to_sub(monkeypatch):
    monkeypatch.setenv("MCP_JWT_USER_CLAIM", "  ")
    assert Config.from_env().jwt_user_claim == "sub"


@pytest.mark.parametrize("value", ["sub", "upn", "preferred_username", "urn:oid:0.9.1", "x-uid"])
def test_a_claim_name_is_accepted(value):
    assert not any("MCP_JWT_USER_CLAIM" in p for p in _cfg(True, jwt_user_claim=value).problems())


@pytest.mark.parametrize("value", ["a b", "sub;drop", "1abc", "é"])
def test_a_malformed_claim_name_refuses_to_start(value):
    assert any("MCP_JWT_USER_CLAIM" in p for p in _cfg(True, jwt_user_claim=value).problems())


# ---- 0.7.1 review: what a token must never get through ----
def _hs(claims, secret, alg="HS256", headers=None):
    payload = {"iss": _ISS, "aud": _AUD, "exp": int(time.time()) + 600, **claims}
    return pyjwt.encode(payload, secret, algorithm=alg, headers=headers)


def test_a_shared_secret_token_signed_with_the_public_key_is_refused():
    """Algorithm confusion: an HS256 token whose secret is the RSA public key."""
    from cryptography.hazmat.primitives import serialization

    pem = _PUB.public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    provider = _built_for(jwt_algorithms="RS256,RS512")
    for alg in ("HS256", "HS512"):
        try:
            tok = _hs({"sub": "u"}, pem, alg)
        except Exception:  # noqa: BLE001 - PyJWT may itself refuse to sign so
            continue
        assert provider.verify(_bearer(tok)) is None


def test_an_unsigned_token_is_refused():
    tok = pyjwt.encode(
        {"iss": _ISS, "aud": _AUD, "exp": int(time.time()) + 600, "sub": "u"},
        None,
        algorithm="none",
    )
    assert _built_for().verify(_bearer(tok)) is None


def test_a_token_without_exp_iss_or_aud_is_refused():
    for drop in ("exp", "iss", "aud"):
        payload = {"iss": _ISS, "aud": _AUD, "exp": int(time.time()) + 600, "sub": "u"}
        del payload[drop]
        tok = pyjwt.encode(payload, _PRIV, algorithm="RS256")
        assert _jwks_provider().verify(_bearer(tok)) is None, drop


def test_a_key_marked_with_an_algorithm_verifies_only_that_algorithm():
    class _MarkedJwks:
        def get_signing_key_from_jwt(self, token):
            return type("K", (), {"key": _PUB, "_jwk_data": {"alg": "RS256"}})()

    provider = auth.JwksJwtAuth(
        "https://kc/jwks", _ISS, _AUD, jwks_client=_MarkedJwks(), algorithms=["RS256", "RS512"]
    )
    assert provider.verify(_bearer(_sign({"sub": "u"}))).subject == "u"
    assert provider.verify(_bearer(_rs512({"sub": "u"}))) is None


def test_the_class_built_directly_refuses_a_shared_secret_algorithm_and_reads_sub():
    with pytest.raises(ValueError, match="MCP_JWT_ALGORITHMS"):
        auth.JwksJwtAuth(
            "https://kc/jwks", _ISS, _AUD, jwks_client=_FakeJwks(), algorithms=["HS256"]
        )
    p = _jwks_provider().verify(_bearer(_sign({"sub": "s", "preferred_username": "p"})))
    assert p.subject == "s"
    assert _jwks_provider().verify(_bearer(_sign({"preferred_username": "p"}))) is None


@pytest.mark.parametrize("value", [" alice", "alice ", " ", "al\nice", "a\tb", 42, ["A1"], ""])
def test_a_user_that_is_not_clean_text_names_nobody(value):
    assert _jwks_provider().verify(_bearer(_sign({"sub": value}))) is None


def test_realm_access_that_is_not_an_object_gives_no_roles_not_an_error():
    p = _jwks_provider().verify(_bearer(_sign({"sub": "u", "realm_access": ["x"]})))
    assert p.subject == "u" and p.roles == []
    assert auth.validate_token(_sign({"sub": "u", "realm_access": "x"})) is None


@pytest.mark.parametrize("value", ["https://example.com/uid", "urn:oid:0.9.2342.19200300.100.1.1"])
def test_a_url_style_claim_name_is_accepted(value):
    assert not any("MCP_JWT_USER_CLAIM" in p for p in _cfg(True, jwt_user_claim=value).problems())


# ---- the claims that name the client and the scopes, set by the operator


def _claims_provider(client_claims, scope_claims):
    return auth.JwksJwtAuth(
        "https://kc.example/jwks",
        _ISS,
        _AUD,
        jwks_client=_FakeJwks(),
        client_claims=client_claims,
        scope_claims=scope_claims,
    )


def test_the_client_and_the_scopes_come_from_the_first_claim_present():
    """Okta names the application in `cid` and lists the scopes in `scp`."""
    provider = _claims_provider(("cid", "azp"), ("scp", "scope"))
    p = provider.verify(_bearer(_sign({"sub": "u", "cid": "okta-app", "azp": "x", "scp": ["a"]})))
    assert (p.client_id, p.scopes) == ("okta-app", ("a",))
    p = provider.verify(_bearer(_sign({"sub": "u", "azp": "x", "scope": "b c"})))
    assert (p.client_id, p.scopes) == ("x", ("b", "c")), "the next claim when the first is absent"


def test_an_entra_scp_string_is_split_on_spaces():
    provider = _claims_provider(("appid", "azp"), ("scp",))
    p = provider.verify(_bearer(_sign({"sub": "u", "appid": "entra-app", "scp": "Data.Read x"})))
    assert (p.client_id, p.scopes) == ("entra-app", ("Data.Read", "x"))


@pytest.mark.parametrize("value", [3, {"a": 1}, ["a", 3], [["a"]], True, None])
def test_a_scope_claim_of_another_type_gives_no_scope_never_a_conversion(value):
    provider = _claims_provider(("azp",), ("scp",))
    assert provider.verify(_bearer(_sign({"sub": "u", "scp": value}))).scopes == ()


def test_the_default_claims_are_those_of_0_7():
    """azp then client_id for the client, scope for the scopes: the principal of 0.7."""
    default = _jwks_provider()
    claims = {"sub": "u", "jti": "j", "azp": "a", "client_id": "b", "scope": "x y", "scp": "z"}
    assert default.verify(_bearer(_sign(claims))) == auth.Principal(
        subject="u", token_id="j", client_id="a", scopes=("x", "y")
    )


def test_a_claim_present_wins_even_empty_and_the_next_is_never_read():
    """The first claim the token carries, whatever it holds: an empty scope list gives no
    scope, never the next claim's; an empty client gives no client."""
    provider = _claims_provider(("cid", "azp"), ("scp", "scope"))
    claims = {"sub": "u", "cid": "", "azp": "x", "scp": [], "scope": "data:query"}
    p = provider.verify(_bearer(_sign(claims)))
    assert (p.client_id, p.scopes) == ("", ())


@pytest.mark.parametrize("value", [["mcp-host"], True, 3, {"a": "b"}])
def test_a_client_claim_that_is_not_text_is_no_client_never_a_conversion(value):
    provider = _claims_provider(("azp", "client_id"), ("scope",))
    assert provider.verify(_bearer(_sign({"sub": "u", "azp": value}))).client_id == ""
