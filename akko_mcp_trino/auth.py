"""Authentication: a pluggable provider interface and the JWT verification behind it.

`AuthProvider` is the contract; `JwksJwtAuth` is the implementation that ships.
It verifies the signature against the issuer's JWKS, the issuer, the audience
and the expiry, and fails closed. `UnverifiedJwtAuth` stays in the module for
local development and tests only; `build_auth` never selects it.
"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass, field
from typing import Optional, Protocol, Sequence, Union

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Principal:
    """A verified caller. `subject` names it in the audit line and to the identity mode."""

    subject: str
    roles: list = field(default_factory=list)
    email: str = ""
    # The token's `jti`, for the audit join. Never the token itself.
    token_id: str = ""
    # The application that asked for the token: `azp` (OpenID Connect), else
    # `client_id` (RFC 9068). Empty when the token names none.
    client_id: str = ""
    # The scopes the token carries (`scope`, space-delimited, RFC 6749 §3.3).
    scopes: tuple[str, ...] = ()


class AuthProvider(Protocol):
    """The authentication contract: inspect the request headers, return a Principal or None."""

    def verify(self, headers: dict) -> Optional[Principal]:  # pragma: no cover - protocole
        """Return the verified Principal for these request headers, or None."""
        ...


def decode_jwt_unsafe(token: str) -> Optional[dict]:
    """Decode a JWT payload WITHOUT verifying the signature.

    Kept for development and tests. Never use it to make an access decision:
    a forged token decodes just as well as a real one.
    """
    try:
        payload = token.split(".")[1]
        payload += "=" * (4 - len(payload) % 4)  # padding base64
        return json.loads(base64.b64decode(payload))
    except Exception:
        return None


def validate_token(token: Optional[str]) -> Optional[list]:
    """Return the realm roles of a JWT, or None when it cannot be decoded.

    NOTE: the signature is NOT verified here. See `JwksJwtAuth` for the real check."""
    if not token:
        return None
    claims = decode_jwt_unsafe(token)
    if not claims:
        return None
    roles = claims.get("realm_access", {}).get("roles")
    if not isinstance(roles, list):
        return None
    return roles


def extract_bearer_token(headers: dict) -> Optional[str]:
    """Extract the bearer token from a headers dict; the header name is case-insensitive."""
    auth = headers.get("authorization") or headers.get("Authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None


def build_auth(config, audiences: Sequence[str] = ()) -> Optional[AuthProvider]:
    """Pick the auth provider from configuration, with nothing hardcoded; ``audiences``
    are accepted besides ``MCP_OIDC_AUDIENCE`` (an extension's, see ``accepted_audiences``).

    Auth disabled: None. JWKS configured: `JwksJwtAuth`, which verifies signatures.

    FAIL-CLOSED: auth enabled without a `jwks_url` is a blocking configuration
    error, and the server refuses to start. An authentication layer that cannot
    verify anything must not pretend to; the old fallback to `UnverifiedJwtAuth`
    accepted any forged token. That class stays available for tests and local
    development, but is never selected automatically.

    A token must be issued for this server (RFC 8707): its audience is
    ``MCP_OIDC_AUDIENCE``, ``MCP_RESOURCE_URL`` or an extension's, and never the audience
    of the token the server obtains for Trino (``TRINO_TOKEN_EXCHANGE_AUDIENCE``). A
    token Trino would accept is refused here, even when it names this server too, and a
    configuration that would accept one refuses to start."""
    if not config.auth_enabled:
        return None
    if not config.jwks_url:
        raise ValueError(
            "MCP_AUTH_ENABLED=true but MCP_JWKS_URL is empty: refusing to start with "
            "unverified JWT checking (fail-closed). Set MCP_JWKS_URL, MCP_OIDC_ISSUER "
            "and MCP_OIDC_AUDIENCE to your identity provider."
        )
    accepted = accepted_audiences(config, [*audiences, config.resource_url])
    if not accepted:
        raise ValueError(
            "MCP_AUTH_ENABLED=true but no token audience is set: set MCP_OIDC_AUDIENCE (or "
            "MCP_RESOURCE_URL) to the audience the identity provider gives tokens issued "
            "for this server"
        )
    refused = [a for a in (config.token_exchange_audience,) if a]
    overlap = sorted(set(accepted) & set(refused))
    if overlap:
        raise ValueError(
            f"audience {overlap[0]!r} is both accepted from callers and requested for Trino "
            f"(TRINO_TOKEN_EXCHANGE_AUDIENCE): a token issued for Trino must not open this "
            f"server; give this server an audience of its own"
        )
    return JwksJwtAuth(
        config.jwks_url,
        config.oidc_issuer,
        accepted,
        leeway_seconds=config.jwt_leeway_seconds,
        refused_audiences=refused,
    )


def accepted_audiences(config, extra: Sequence[str] = ()) -> list[str]:
    """The audiences a token may carry: ``MCP_OIDC_AUDIENCE``, then ``extra`` (what an
    extension accepts, such as this server's canonical URL). An empty list refuses
    every token."""
    audiences: list[str] = []
    for value in (config.oidc_audience, *extra):
        if value and value not in audiences:
            audiences.append(value)
    return audiences


def _scopes(claim) -> tuple[str, ...]:
    if isinstance(claim, str):
        return tuple(claim.split())
    if isinstance(claim, list):
        return tuple(str(s) for s in claim)
    return ()


def _principal_from_claims(claims: dict) -> Principal:
    roles = claims.get("realm_access", {}).get("roles")
    return Principal(
        subject=str(claims.get("preferred_username") or claims.get("sub") or ""),
        roles=roles if isinstance(roles, list) else [],
        email=str(claims.get("email") or ""),
        token_id=str(claims.get("jti") or ""),
        client_id=str(claims.get("azp") or claims.get("client_id") or ""),
        scopes=_scopes(claims.get("scope")),
    )


class UnverifiedJwtAuth:
    """Development-only provider: decodes the JWT WITHOUT verifying the signature.
    Never selected by `build_auth`; use `JwksJwtAuth` anywhere that matters."""

    def verify(self, headers: dict) -> Optional[Principal]:
        """Decode the bearer without verifying it (development only)."""
        claims = decode_jwt_unsafe(extract_bearer_token(headers) or "")
        if not claims:
            return None
        return _principal_from_claims(claims)


class JwksJwtAuth:
    """Verifies the SIGNATURE against the issuer's JWKS, plus issuer, audience and expiry.

    `jwks_client` (a PyJWKClient) is injectable, so the class is testable with a
    local RSA key and no network. Any failure — signature, iss, aud, exp, missing
    token, an audience in ``refused_audiences`` — yields None. Fail closed."""

    def __init__(
        self,
        jwks_url: str,
        issuer: str,
        audience: Union[str, Sequence[str]],
        *,
        jwks_client=None,
        algorithms=None,
        leeway_seconds: int = 30,
        refused_audiences: Sequence[str] = (),
    ):
        """Verify tokens against ``jwks_url`` for ``issuer`` and ``audience`` (one value or
        several, any of which may match), refusing a token that also names one of
        ``refused_audiences``; ``jwks_client`` is injectable for tests.
        """
        self._refused = frozenset(refused_audiences)
        self._issuer = issuer
        self._audience = audience if isinstance(audience, str) else list(audience)
        self._leeway = leeway_seconds
        self._algorithms = algorithms or ["RS256"]
        if jwks_client is not None:
            self._client = jwks_client
        else:  # pragma: no cover - network construction (PyJWKClient), proven by running it
            from jwt import PyJWKClient

            self._client = PyJWKClient(jwks_url)

    def verify(self, headers: dict) -> Optional[Principal]:
        """Verify the bearer's signature, issuer, audience and expiry; None if absent or invalid."""
        token = extract_bearer_token(headers)
        if not token:
            return None
        try:
            import jwt as _jwt

            key = self._client.get_signing_key_from_jwt(token).key
            claims = _jwt.decode(
                token,
                key,
                algorithms=self._algorithms,
                issuer=self._issuer,
                audience=self._audience,
                leeway=self._leeway,  # clocks drift; an iat one second ahead is not a forgery
            )
        except Exception as exc:  # noqa: BLE001 — signature/iss/aud/exp invalid → fail-closed
            log.info("token refused: %s", type(exc).__name__)  # the reason, never the token
            return None
        aud = claims.get("aud")
        if self._refused & set([aud] if isinstance(aud, str) else aud or []):
            log.info("token refused: issued for another resource")
            return None
        return _principal_from_claims(claims)
