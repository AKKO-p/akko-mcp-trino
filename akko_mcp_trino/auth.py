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
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Protocol, Sequence, Union

from .config import (
    DEFAULT_JWKS_CACHE_SECONDS,
    DEFAULT_JWKS_MIN_REFRESH_SECONDS,
    DEFAULT_JWKS_TIMEOUT_SECONDS,
    parse_jwt_algorithms,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Principal:
    """A verified caller. `subject` names it in the audit line and to the identity mode."""

    subject: str
    roles: list = field(default_factory=list)
    email: str = ""
    # The token's `jti`, for the audit join. Never the token itself.
    token_id: str = ""
    # The application that asked for the token: the first of `MCP_JWT_CLIENT_CLAIMS` the
    # token carries (`azp`, OpenID Connect, else `client_id`, RFC 9068, by default). Empty
    # when the token names none.
    client_id: str = ""
    # The scopes the token carries: the first of `MCP_JWT_SCOPE_CLAIMS` it holds (`scope`,
    # space-delimited, RFC 6749 §3.3, by default).
    scopes: tuple[str, ...] = ()


# Why a token was refused, as the audit line and the refusal counter name it: a stable code,
# by the class of the verification error (the most specific first), never its message.
INVALID_TOKEN = "invalid_token"
_REFUSAL_CODES = {
    "ExpiredSignatureError": "token_expired",
    "ImmatureSignatureError": "token_not_yet_valid",
    "InvalidAudienceError": "wrong_audience",
    "InvalidIssuerError": "wrong_issuer",
    "InvalidSignatureError": "bad_signature",
    "MissingRequiredClaimError": "missing_claim",
    "InvalidAlgorithmError": "wrong_algorithm",
    "PyJWKClientConnectionError": "jwks_unreachable",
    "PyJWKClientError": "unknown_key",
}
TOKEN_REFUSAL_CODES = (INVALID_TOKEN, "no_user_claim", *sorted(set(_REFUSAL_CODES.values())))


def token_refusal_code(exc: BaseException) -> str:
    """The code of a token refused for ``exc``: that of its class, or of the nearest class it
    derives from, else ``invalid_token``."""
    for cls in type(exc).__mro__:
        code = _REFUSAL_CODES.get(cls.__name__)
        if code is not None:
            return code
    return INVALID_TOKEN


@dataclass(frozen=True)
class TokenVerdict:
    """What the verification of a request's token found: the verified caller, or None and
    the code of why (``reason``, empty when the request carried no token)."""

    principal: Optional[Principal]
    reason: str = ""


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
    access = claims.get("realm_access")
    roles = access.get("roles") if isinstance(access, dict) else None
    if not isinstance(roles, list):
        return None
    return roles


def extract_bearer_token(headers: dict) -> Optional[str]:
    """Extract the bearer token from a headers dict; the header name is case-insensitive."""
    auth = headers.get("authorization") or headers.get("Authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None


def build_auth(
    config, audiences: Sequence[str] = (), on_fetch: Optional[Callable[[bool], None]] = None
) -> Optional[AuthProvider]:
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
    configuration that would accept one refuses to start.

    The issuer's keys are read by a ``JwksKeys`` within ``MCP_JWKS_TIMEOUT_SECONDS``, kept
    ``MCP_JWKS_CACHE_SECONDS`` and read again at most once every
    ``MCP_JWKS_MIN_REFRESH_SECONDS``; ``on_fetch`` hears whether each read succeeded."""
    if not config.auth_enabled:
        return None
    if not config.jwks_url:
        raise ValueError(
            "MCP_AUTH_ENABLED=true but MCP_JWKS_URL is empty: refusing to start with "
            "unverified JWT checking (fail-closed). Set MCP_JWKS_URL, MCP_OIDC_ISSUER "
            "and MCP_OIDC_AUDIENCE to your identity provider."
        )
    if not config.oidc_issuer:
        # PyJWT skips the issuer check only for None; an empty string is compared to the
        # token's iss, so every token would be refused after the server had started.
        raise ValueError(
            "MCP_AUTH_ENABLED=true but MCP_OIDC_ISSUER is empty: every token would be "
            "refused. Set MCP_OIDC_ISSUER to the issuer your identity provider puts in "
            "its tokens (iss)."
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
    keys = JwksKeys.from_url(
        config.jwks_url,
        timeout_seconds=config.jwks_timeout_seconds,
        cache_seconds=config.jwks_cache_seconds,
        min_refresh_seconds=config.jwks_min_refresh_seconds,
        on_fetch=on_fetch,
    )
    return JwksJwtAuth(
        config.jwks_url,
        config.oidc_issuer,
        accepted,
        jwks_client=keys,
        leeway_seconds=config.jwt_leeway_seconds,
        refused_audiences=refused,
        algorithms=parse_jwt_algorithms(config.jwt_algorithms),
        user_claim=config.jwt_user_claim,
        client_claims=config.client_claims(),
        scope_claims=config.scope_claims(),
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


# The claims read by default: those of 0.7.
CLIENT_CLAIMS = ("azp", "client_id")
SCOPE_CLAIMS = ("scope",)


def _first(claims: dict, names: Sequence[str]) -> Any:
    """The value of the first of ``names`` the token carries, whatever it holds (an empty
    one included: the next claim is then never read), or None when it carries none."""
    for name in names:
        if name in claims:
            return claims[name]
    return None


def _client(claim) -> str:
    """The client a claim names: its text; any other value is no client, never a conversion."""
    return claim if isinstance(claim, str) else ""


def _scopes(claim) -> tuple[str, ...]:
    """The scopes of a claim: a space-delimited string, or a list of strings. Any other value,
    a list holding anything but strings included, is no scope: never a conversion."""
    if isinstance(claim, str):
        return tuple(claim.split())
    if isinstance(claim, list) and all(isinstance(s, str) for s in claim):
        return tuple(claim)
    return ()


def _principal_from_claims(
    claims: dict,
    user_claim: str = "sub",
    client_claims: Sequence[str] = CLIENT_CLAIMS,
    scope_claims: Sequence[str] = SCOPE_CLAIMS,
) -> Optional[Principal]:
    """The caller a verified token names: the text in the ``user_claim`` claim
    (``MCP_JWT_USER_CLAIM``, ``sub`` by default), a top-level claim read as written. A token
    without it, or whose value is not text, is empty, or has surrounding spaces or control
    characters, names nobody (None): never another claim, never a cleaned-up value."""
    value = claims.get(user_claim)
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or any(ord(ch) < 32 or ord(ch) == 127 for ch in value)
    ):
        return None
    access = claims.get("realm_access")
    roles = access.get("roles") if isinstance(access, dict) else None
    return Principal(
        subject=value,
        roles=roles if isinstance(roles, list) else [],
        email=str(claims.get("email") or ""),
        token_id=str(claims.get("jti") or ""),
        client_id=_client(_first(claims, client_claims)),
        scopes=_scopes(_first(claims, scope_claims)),
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


class JwksKeys:
    """The issuer's signing keys, by ``kid``, read from its JWKS and kept in memory.

    Reading the JWKS is the only network call of token verification, and a caller chooses
    what the token's ``kid`` says. So the keys are read again only when they are older than
    ``cache_seconds`` or a token names a ``kid`` they do not hold, and in either case at most
    once every ``min_refresh_seconds``, a failed read included: a stream of tokens naming
    unknown keys causes one read per interval, never one per token. One read runs at a time,
    and nobody waits for it: a caller that finds a read under way is answered from the keys
    already held. A failed read keeps the keys held, so an identity provider that cannot be
    reached does not refuse tokens signed with a key already known. ``fetch`` returns the
    JWKS document (a dict) and raises on failure; ``on_fetch`` hears ``True`` or ``False``
    after each read; ``clock`` is injectable for tests.
    """

    def __init__(
        self,
        fetch: Callable[[], Any],
        *,
        cache_seconds: float,
        min_refresh_seconds: float,
        on_fetch: Optional[Callable[[bool], None]] = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        """Hold no key yet: the first token read causes the first read of the JWKS."""
        self._fetch = fetch
        self._cache = cache_seconds
        self._min_refresh = min_refresh_seconds
        self._on_fetch = on_fetch
        self._clock = clock
        self._keys: dict[str, Any] = {}
        self._loaded_at: Optional[float] = None
        self._attempted_at: Optional[float] = None
        # Whether the last read failed: a key missing then is the JWKS's absence, not the token's.
        self._failed = False
        self._reading = threading.Lock()

    @classmethod
    def from_url(
        cls,
        url: str,
        *,
        timeout_seconds: float,
        cache_seconds: float,
        min_refresh_seconds: float,
        on_fetch: Optional[Callable[[bool], None]] = None,
    ) -> "JwksKeys":
        """Keys read from ``url`` by PyJWT's client (http and https only, no redirect),
        within ``timeout_seconds``; PyJWT's own cache is off, this class is the cache."""
        from jwt import PyJWKClient

        client = PyJWKClient(url, cache_jwk_set=False, timeout=timeout_seconds)
        return cls(
            client.fetch_data,
            cache_seconds=cache_seconds,
            min_refresh_seconds=min_refresh_seconds,
            on_fetch=on_fetch,
        )

    def get_signing_key_from_jwt(self, token: str) -> Any:
        """The key the token's ``kid`` names; raises ``PyJWKClientError`` when none is held."""
        import jwt as _jwt

        kid = _jwt.get_unverified_header(token).get("kid")
        if not isinstance(kid, str):
            raise _jwt.PyJWKClientError("the token names no key")
        if self._stale(kid) and not self._attempted_recently():
            self._read()
        key = self._keys.get(kid)
        if key is None and self._failed:
            raise _jwt.PyJWKClientConnectionError("the JWKS could not be read")
        if key is None:
            raise _jwt.PyJWKClientError("no signing key matches the token")
        return key

    def _stale(self, kid: str) -> bool:
        """True when the held keys miss ``kid`` or are past their lifetime."""
        return (
            kid not in self._keys
            or self._loaded_at is None
            or self._clock() - self._loaded_at >= self._cache
        )

    def _attempted_recently(self) -> bool:
        """True when a read was attempted, successful or not, in the last interval."""
        return (
            self._attempted_at is not None
            and self._clock() - self._attempted_at < self._min_refresh
        )

    def _read(self) -> None:
        """Read the JWKS once, unless another read is under way; keep the held keys on failure."""
        if not self._reading.acquire(blocking=False):
            return
        try:
            if self._attempted_recently():  # another thread read them in the meantime
                return
            self._attempted_at = self._clock()
            try:
                from jwt import PyJWKSet

                jwk_set = PyJWKSet.from_dict(self._fetch())
                keys = {
                    k.key_id: k
                    for k in jwk_set.keys
                    if k.key_id and k.public_key_use in ("sig", None)
                }
                if not keys:
                    raise ValueError("the JWKS holds no signing key with a kid")
            except Exception as exc:  # noqa: BLE001 — any failure keeps the held keys
                log.warning("JWKS read failed: %s", type(exc).__name__)
                self._failed = True
                if self._on_fetch is not None:
                    self._on_fetch(False)
                return
            self._keys = keys
            self._loaded_at = self._attempted_at
            self._failed = False
            if self._on_fetch is not None:
                self._on_fetch(True)
        finally:
            self._reading.release()


class JwksJwtAuth:
    """Verifies the SIGNATURE against the issuer's JWKS, plus issuer, audience and expiry.

    `jwks_client` (a `JwksKeys` by default, any object with `get_signing_key_from_jwt`) is
    injectable, so the class is testable with a local RSA key and no network. Any failure
    (signature, iss, aud, exp, missing token, an audience in ``refused_audiences``) yields
    None. Fail closed."""

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
        user_claim: str = "sub",
        client_claims: Sequence[str] = CLIENT_CLAIMS,
        scope_claims: Sequence[str] = SCOPE_CLAIMS,
    ):
        """Verify tokens against ``jwks_url`` for ``issuer`` and ``audience`` (one value or
        several, any of which may match), refusing a token that also names one of
        ``refused_audiences``; ``jwks_client`` is injectable for tests. ``algorithms`` are
        public-key ones only (``JWT_ALGORITHMS``), RS256 when none is given: a shared-secret
        algorithm or ``none`` raises ValueError, here as at start. ``client_claims`` and
        ``scope_claims`` name the claims read for the client and the scopes, in order.
        """
        self._refused = frozenset(refused_audiences)
        self._user_claim = user_claim
        self._client_claims = tuple(client_claims)
        self._scope_claims = tuple(scope_claims)
        self._issuer = issuer
        self._audience = audience if isinstance(audience, str) else list(audience)
        self._leeway = leeway_seconds
        self._algorithms = parse_jwt_algorithms(",".join(algorithms or ["RS256"]))
        if jwks_client is not None:
            self._client = jwks_client
        else:
            self._client = JwksKeys.from_url(
                jwks_url,
                timeout_seconds=DEFAULT_JWKS_TIMEOUT_SECONDS,
                cache_seconds=DEFAULT_JWKS_CACHE_SECONDS,
                min_refresh_seconds=DEFAULT_JWKS_MIN_REFRESH_SECONDS,
            )

    def verify(self, headers: dict) -> Optional[Principal]:
        """Verify the bearer's signature, issuer, audience and expiry; None if absent or invalid."""
        return self.check(headers).principal

    def check(self, headers: dict) -> TokenVerdict:
        """Verify the bearer like ``verify``, and say why when it is refused: a code of
        ``TOKEN_REFUSAL_CODES``, never the token nor any of its claims."""
        token = extract_bearer_token(headers)
        if not token:
            return TokenVerdict(None)
        try:
            import jwt as _jwt

            signing_key = self._client.get_signing_key_from_jwt(token)
            # A key the issuer marks with an algorithm verifies only tokens of that algorithm.
            declared = (getattr(signing_key, "_jwk_data", None) or {}).get("alg")
            if declared and declared != _jwt.get_unverified_header(token).get("alg"):
                raise _jwt.InvalidAlgorithmError("the token's algorithm is not its key's")
            claims = _jwt.decode(
                token,
                signing_key.key,
                algorithms=self._algorithms,
                issuer=self._issuer,
                audience=self._audience,
                leeway=self._leeway,  # clocks drift; an iat one second ahead is not a forgery
                options={"require": ["exp", "iss", "aud"]},  # a token without exp never ends
            )
        except Exception as exc:  # noqa: BLE001 — signature/iss/aud/exp invalid → fail-closed
            log.info("token refused: %s", type(exc).__name__)  # the reason, never the token
            return TokenVerdict(None, token_refusal_code(exc))
        aud = claims.get("aud")
        if self._refused & set([aud] if isinstance(aud, str) else aud or []):
            log.info("token refused: issued for another resource")
            return TokenVerdict(None, "wrong_audience")
        principal = _principal_from_claims(
            claims, self._user_claim, self._client_claims, self._scope_claims
        )
        if principal is None:
            log.info("token refused: no %s claim", self._user_claim)
            return TokenVerdict(None, "no_user_claim")
        return TokenVerdict(principal)
