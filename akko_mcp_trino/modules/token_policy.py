"""token-policy: what a verified token must also carry, an allowed client and the required scopes.

The signature, the issuer and the audience prove a token was issued for this server. They
do not say which application asked for it: any client of the identity provider can be given
this server's audience. With ``MCP_ALLOWED_CLIENTS``, the application the token names (the
first of ``MCP_JWT_CLIENT_CLAIMS`` it carries) must be on the list; a token that names none
is refused. With ``MCP_REQUIRED_SCOPES``, its scopes (the first of ``MCP_JWT_SCOPE_CLAIMS``)
must hold every one of them. Both lists are separated by commas or spaces, compared exactly,
case included, with no wildcard. A policy with neither refuses to start: it would enforce
nothing.

The two refusals differ on purpose. A foreign application gets the 401 of an invalid token.
A missing scope gets 403 ``insufficient_scope`` (RFC 6750 §3.1), whose challenge names the
scopes to ask for, so that an MCP host authorizes again with them instead of retrying the
same token. The client is checked first, so a foreign application is never told which scope
would let it in. The protected resource metadata the core publishes adds
``scopes_supported``.
"""

import re
from typing import Mapping, Optional

from ..auth import Principal
from ..config import Config
from ..extensions import Admission, Extension, Refusal
from ..middleware import (
    INVALID_TOKEN_MESSAGE,
    NO_TOKEN_MESSAGE,
    UNAUTHENTICATED,
    protected_resource,
)

CLIENT_NOT_ALLOWED = "client_not_allowed"
INSUFFICIENT_SCOPE = "insufficient_scope"
INSUFFICIENT_SCOPE_MESSAGE = (
    "the token lacks a scope this server requires: ask the identity provider for a token "
    "with the scopes the WWW-Authenticate header names"
)


# A scope-token (RFC 6749 §3.3): %x21 / %x23-5B / %x5D-7E, never a quote or a backslash, so
# that it never breaks the quoted string of a challenge. A client is held to the same.
_SCOPE_TOKEN = re.compile(r"[\x21\x23-\x5b\x5d-\x7e]+")


def split_values(value: Optional[str]) -> tuple[str, ...]:
    """A setting separated by commas or spaces, each value once, in order."""
    return tuple(dict.fromkeys(v for v in re.split(r"[\s,]+", value or "") if v))


def _checked(variable: str, values: tuple[str, ...]) -> tuple[str, ...]:
    """``values`` when each is a scope-token; ValueError naming ``variable`` otherwise."""
    bad = [v for v in values if not _SCOPE_TOKEN.fullmatch(v)]
    if bad:
        raise ValueError(
            f"token-policy: {variable} holds {len(bad)} value(s) outside the characters of an "
            "OAuth scope-token (printable ASCII, no space, no quote, no backslash)"
        )
    return values


class TokenPolicyExtension(Extension):
    """The allowed clients and the required scopes, checked once the core verified the token."""

    name = "token-policy"

    def __init__(self, config: Config, clients: tuple[str, ...], scopes: tuple[str, ...]):
        """Enforce ``clients`` (any, when empty) and ``scopes`` (none, when empty)."""
        self._config = config
        self.clients = clients
        self.scopes = scopes
        self._documents, self._metadata_url = protected_resource(
            config.resource_url, config.oidc_issuer
        )

    def check_lines(self) -> list[str]:
        """The clients and the scopes enforced, and the claims they are read from."""
        clients = ", ".join(self.clients) or "any"
        scopes = ", ".join(self.scopes) or "none"
        return [
            f"token-policy: allowed clients {clients} (from claims "
            f"{','.join(self._config.client_claims())}); required scopes {scopes} (from "
            f"claims {','.join(self._config.scope_claims())})"
        ]

    def refusal(self, principal: Principal) -> Optional[Refusal]:
        """Why this verified caller is refused, the client before the scopes, or None."""
        if self.clients and principal.client_id not in self.clients:
            return Refusal(
                CLIENT_NOT_ALLOWED, 401, error="invalid_token", message=INVALID_TOKEN_MESSAGE
            )
        if any(scope not in principal.scopes for scope in self.scopes):
            return Refusal(
                INSUFFICIENT_SCOPE,
                403,
                error="insufficient_scope",
                message=INSUFFICIENT_SCOPE_MESSAGE,
            )
        return None

    def after_auth(self, admission: Admission) -> Optional[Refusal]:
        """Refuse a request without a verified token as the core does (401, never 403), then a
        foreign client, then a token without every required scope."""
        if admission.principal is None:
            # MCP_AUTH_REQUIRED=false lets it through the core: it gets the core's own 401.
            if admission.bearer:
                return Refusal(
                    UNAUTHENTICATED, error="invalid_token", message=INVALID_TOKEN_MESSAGE
                )
            return Refusal(UNAUTHENTICATED, message=NO_TOKEN_MESSAGE)
        return self.refusal(admission.principal)

    def challenge(self, refusal: Refusal) -> Optional[str]:
        """The challenge of this policy's refusals; the core's for any other."""
        if refusal.reason not in (CLIENT_NOT_ALLOWED, INSUFFICIENT_SCOPE):
            return None
        parts = []
        if self._metadata_url:
            parts.append(f'resource_metadata="{self._metadata_url}"')
        parts.append(f'error="{refusal.error}"')
        if refusal.reason == INSUFFICIENT_SCOPE:
            parts.append(f'scope="{" ".join(self.scopes)}"')
        return "Bearer " + ", ".join(parts)

    def public_documents(self) -> Mapping[str, dict]:
        """The core's protected resource metadata, with the scopes it supports."""
        if not self.scopes:
            return self._documents
        return {
            path: {**document, "scopes_supported": list(self.scopes)}
            for path, document in self._documents.items()
        }


def create(config: Config, env: Mapping[str, str]) -> TokenPolicyExtension:
    """The policy of ``MCP_ALLOWED_CLIENTS`` and ``MCP_REQUIRED_SCOPES``; ValueError when
    both are empty."""
    clients = _checked("MCP_ALLOWED_CLIENTS", split_values(env.get("MCP_ALLOWED_CLIENTS")))
    scopes = _checked("MCP_REQUIRED_SCOPES", split_values(env.get("MCP_REQUIRED_SCOPES")))
    if not clients and not scopes:
        raise ValueError(
            "token-policy: MCP_ALLOWED_CLIENTS and MCP_REQUIRED_SCOPES are both empty, so "
            "there is nothing to enforce: set one of them, or remove token-policy from "
            "MCP_EXTENSIONS"
        )
    return TokenPolicyExtension(config, clients, scopes)
