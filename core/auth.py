"""Authentification — interface pluggable + fonctions JWT actuelles (vendor-neutre).

P1 refactor pur : on PRÉSERVE le comportement existant — décodage JWT SANS
vérification de signature, désactivé par défaut. L'interface `AuthProvider` prépare
P2 (vérification JWKS réelle) sans changer le comportement courant. Aucune dépendance
AKKO : la couche AKKO injectera un provider Keycloak/JWKS en P2.
"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from typing import Optional, Protocol


@dataclass(frozen=True)
class Principal:
    """Identité authentifiée. `subject` = identité propagée à Trino (X-Trino-User, S4)."""
    subject: str
    roles: list = field(default_factory=list)
    email: str = ""


class AuthProvider(Protocol):
    """Contrat d'authentification : vérifie les en-têtes et renvoie un Principal/None."""

    def verify(self, headers: dict) -> Optional[Principal]:  # pragma: no cover - protocole
        ...


def decode_jwt_unsafe(token: str) -> Optional[dict]:
    """Décode le payload d'un JWT SANS vérifier la signature.

    ⚠️ P1 : comportement historique conservé tel quel. La vérification de signature
    (JWKS) est le chantier P2.
    """
    try:
        payload = token.split(".")[1]
        payload += "=" * (4 - len(payload) % 4)  # padding base64
        return json.loads(base64.b64decode(payload))
    except Exception:
        return None


def validate_token(token: Optional[str]) -> Optional[list]:
    """Valide un JWT et renvoie les rôles realm, ou None si invalide.

    NOTE : la signature N'EST PAS vérifiée (P1, à corriger en P2)."""
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
    """Extrait le token Bearer d'un dict d'en-têtes (insensible à la casse de la clé)."""
    auth = headers.get("authorization") or headers.get("Authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None


def build_auth(config) -> Optional[AuthProvider]:
    """Sélectionne le provider d'auth selon la config (Lego, zéro hardcoding) :
    auth désactivée → None ; JWKS configuré → JwksJwtAuth (signature vérifiée).

    FAIL-CLOSED (F1, audit sécu MCP 2026-07) : auth ACTIVÉE sans `jwks_url` = ERREUR
    de configuration bloquante. On REFUSE de démarrer avec une vérification JWT non
    signée (l'ancien repli `UnverifiedJwtAuth` acceptait n'importe quel JWT forgé =
    porte fail-open). `UnverifiedJwtAuth` reste dans le module pour les tests/dev
    mais n'est JAMAIS sélectionné automatiquement."""
    if not config.auth_enabled:
        return None
    if not config.jwks_url:
        raise ValueError(
            "MCP auth_enabled=true mais jwks_url vide : refus de demarrer avec une "
            "verification JWT non signee (fail-closed). Configurer MCP_JWKS_URL + "
            "OIDC_ISSUER + OIDC_AUDIENCE vers Keycloak."
        )
    return JwksJwtAuth(config.jwks_url, config.oidc_issuer, config.oidc_audience)


def _principal_from_claims(claims: dict) -> Principal:
    roles = claims.get("realm_access", {}).get("roles")
    return Principal(
        subject=str(claims.get("preferred_username") or claims.get("sub") or ""),
        roles=roles if isinstance(roles, list) else [],
        email=str(claims.get("email") or ""),
    )


class UnverifiedJwtAuth:
    """Provider hérité : décode le JWT SANS vérifier la signature (P1). À n'utiliser
    qu'en repli ; préférer JwksJwtAuth en prod (S3)."""

    def verify(self, headers: dict) -> Optional[Principal]:
        claims = decode_jwt_unsafe(extract_bearer_token(headers) or "")
        if not claims:
            return None
        return _principal_from_claims(claims)


class JwksJwtAuth:
    """Provider S3 : vérifie SIGNATURE (JWKS de l'émetteur) + issuer + audience + exp.

    Le `jwks_client` (PyJWKClient) est injectable → testable avec une clé RSA locale,
    sans appel réseau. Tout échec (signature, iss, aud, exp, absence) → None (fail-closed)."""

    def __init__(self, jwks_url: str, issuer: str, audience: str, *, jwks_client=None, algorithms=None):
        self._issuer = issuer
        self._audience = audience
        self._algorithms = algorithms or ["RS256"]
        if jwks_client is not None:
            self._client = jwks_client
        else:  # pragma: no cover - construction réseau (PyJWKClient), prouvée au déploiement
            from jwt import PyJWKClient
            self._client = PyJWKClient(jwks_url)

    def verify(self, headers: dict) -> Optional[Principal]:
        token = extract_bearer_token(headers)
        if not token:
            return None
        try:
            import jwt as _jwt
            key = self._client.get_signing_key_from_jwt(token).key
            claims = _jwt.decode(
                token, key, algorithms=self._algorithms,
                issuer=self._issuer, audience=self._audience,
            )
        except Exception:  # noqa: BLE001 — signature/iss/aud/exp invalide → fail-closed
            return None
        return _principal_from_claims(claims)
