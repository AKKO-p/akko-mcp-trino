"""RFC 9728 protected-resource metadata, so MCP hosts know where to log in.

Cursor, Claude and VS Code read ``/.well-known/oauth-protected-resource`` and
expect a refused request to carry ``WWW-Authenticate: Bearer`` with a
``resource_metadata`` URL. Without both, a host sees a bare 401 and cannot
start an auth flow. The document names only what the configuration says:
``authorization_servers`` is empty when no issuer is configured, because a
guessed IdP is a trap, not a convenience.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import Config

WELL_KNOWN_PATH = "/.well-known/oauth-protected-resource"
REALM = "trino"


@dataclass(frozen=True)
class ProtectedResource:
    """What RFC 9728 needs to say about this server: its URL and its issuer."""

    resource: str
    issuer: str

    @staticmethod
    def from_config(config: Config) -> "ProtectedResource":
        """Build from ``MCP_RESOURCE_URL`` and ``MCP_OIDC_ISSUER``."""
        return ProtectedResource(
            resource=config.resource_url.rstrip("/"), issuer=config.oidc_issuer
        )

    def document(self) -> dict:
        """The protected-resource metadata document."""
        return {
            "resource": self.resource,
            "authorization_servers": [self.issuer] if self.issuer else [],
            "bearer_methods_supported": ["header"],
        }

    def www_authenticate(self) -> str:
        """The ``WWW-Authenticate`` value a 401 carries."""
        return f'Bearer realm="{REALM}", resource_metadata="{self.resource}{WELL_KNOWN_PATH}"'
