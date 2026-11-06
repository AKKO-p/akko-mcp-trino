"""Kerberos (SPNEGO) for Atlas, from a keytab, with ``pyspnego`` (the ``kerberos`` extra).

The provider answers a ``401`` that carries ``WWW-Authenticate: Negotiate`` with an
``Authorization: Negotiate`` token. Each negotiation builds a new security context from
``spnego.KerberosKeytab``: ``pyspnego`` gets a new ticket from the keytab and keeps it in a
cache of its own in memory (``MEMORY:``), so an expired ticket is never used and no
process of its own renews one; each negotiation is thus one request to the KDC. A
redirect of Atlas's passive server to the active one is a new request, so the negotiation
starts again on the new host. Once Atlas has opened a session, its ``ATLASSESSIONID``
cookie is sent and no new negotiation happens until Atlas asks for one. Atlas's own
mutual authentication token is not checked: TLS authenticates Atlas.

The service name is ``ATLAS_SPN`` when set (``HTTP@<host>``), else ``HTTP@<host of the
request>``. No message, log or exception names the principal, the keytab or the token.
"""

import base64
from collections.abc import Generator
from typing import Any

import httpx

_NEGOTIATE = "negotiate"


class NegotiationError(Exception):
    """The security context could not be built or stepped; the reason stays out."""


def load_spnego() -> Any:
    """Import ``spnego``; raises ``ValueError`` when the ``kerberos`` extra is missing."""
    try:
        import spnego
    except ImportError:
        raise ValueError(
            "ATLAS_AUTH=kerberos needs the kerberos extra: "
            'pip install "akko-mcp-trino-atlas[kerberos]"'
        ) from None
    return spnego


def _challenge(response: httpx.Response) -> str | None:
    """The token of a ``Negotiate`` challenge (empty for a bare one), or ``None``."""
    for value in response.headers.get_list("www-authenticate"):
        scheme, _, token = value.strip().partition(" ")
        if scheme.lower() == _NEGOTIATE:
            return token.strip()
    return None


class SpnegoAuth(httpx.Auth):
    """``httpx.Auth`` that answers a ``Negotiate`` challenge from a keytab."""

    def __init__(self, principal: str, keytab: str, spn: str) -> None:
        """Keep the names; ``spnego`` is imported now, nothing is read from the keytab yet."""
        self._spnego = load_spnego()
        self._principal = principal
        self._keytab = keytab
        self._spn = spn

    def __repr__(self) -> str:
        """Name the class only, never the principal or the keytab."""
        return "SpnegoAuth()"

    def _service(self, request: httpx.Request) -> tuple[str, str]:
        service, at, host = self._spn.partition("@")
        if self._spn and at:
            return service, host
        return "HTTP", request.url.host

    def _context(self, request: httpx.Request) -> Any:
        service, host = self._service(request)
        credential = self._spnego.KerberosKeytab(keytab=self._keytab, principal=self._principal)
        return self._spnego.client(
            username=credential, hostname=host, service=service, protocol="kerberos"
        )

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        """Send the request; on a ``Negotiate`` challenge, send it again with a token."""
        response = yield request
        if response.status_code != 401 or _challenge(response) is None:
            return
        try:
            context = self._context(request)
            token = context.step()
        except Exception:  # noqa: BLE001 - spnego's errors may name the principal
            raise NegotiationError("the kerberos negotiation failed") from None
        request.headers["Authorization"] = "Negotiate " + base64.b64encode(token).decode()
        yield request
