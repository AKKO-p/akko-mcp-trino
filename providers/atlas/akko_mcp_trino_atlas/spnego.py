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
request>``. The token is SPNEGO (``protocol="negotiate"``), as ``curl --negotiate`` sends.
No message, log or exception names the principal, the keytab or the token.

A negotiation that fails is a refusal of the account (``NegotiationError`` with
``unreachable`` false), since a retry every few seconds with a keytab the KDC refuses would
lock the account in a directory that counts failed pre-authentications; only a KDC that
cannot be found or reached (the Kerberos codes of ``KDC_UNREACHABLE``) is an outage.
"""

import base64
from collections.abc import Generator
from typing import Any

import httpx

_NEGOTIATE = "negotiate"
# MIT Kerberos codes of a KDC that was never reached: KRB5_KDC_UNREACH (cannot contact any
# KDC), KRB5_REALM_UNKNOWN (cannot find a KDC for the realm) and KRB5_REALM_CANT_RESOLVE
# (cannot resolve its address). Any other failure reached the KDC, or the keytab.
KDC_UNREACHABLE = frozenset(code & 0xFFFFFFFF for code in (-1765328228, -1765328229, -1765328164))


class NegotiationError(Exception):
    """The security context could not be built or stepped; the reason stays out.

    ``unreachable`` says the KDC could not be found or reached: an outage, not a refusal.
    """

    def __init__(self, unreachable: bool) -> None:
        """Say whether the KDC could not be reached; the message is fixed."""
        super().__init__("the kerberos negotiation failed")
        self.unreachable = unreachable


def _codes(error: BaseException) -> set[int]:
    """The Kerberos and GSSAPI codes an error carries, its causes and base error included
    (``krb5.Krb5Error.err_code``, ``gssapi`` ``min_code``), as unsigned 32-bit numbers."""
    codes: set[int] = set()
    seen: list[BaseException] = []
    pending: list[BaseException | None] = [error]
    while pending:
        current = pending.pop()
        if current is None or any(current is s for s in seen) or len(seen) > 8:
            continue
        seen.append(current)
        for name in ("err_code", "min_code"):
            value = getattr(current, name, None)
            if isinstance(value, int):
                codes.add(value & 0xFFFFFFFF)
        pending += [current.__cause__, current.__context__, getattr(current, "base_error", None)]
    return codes


def load_spnego() -> Any:
    """Import ``spnego`` and the Kerberos bindings it needs (``gssapi``, and ``krb5`` to read
    a keytab); raises ``ValueError`` when the ``kerberos`` extra is missing. ``pyspnego``
    installed alone imports, but cannot speak Kerberos: that is refused at start, not taken
    for an outage on every call."""
    try:
        import gssapi  # noqa: F401
        import krb5  # noqa: F401
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
            username=credential, hostname=host, service=service, protocol="negotiate"
        )

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        """Send the request; on a ``Negotiate`` challenge, send it again with a token."""
        response = yield request
        if response.status_code != 401 or _challenge(response) is None:
            return
        try:
            context = self._context(request)
            token = context.step()
        except Exception as exc:  # noqa: BLE001 - spnego's errors may name the principal
            unreachable = bool(_codes(exc) & KDC_UNREACHABLE)
            raise NegotiationError(unreachable) from None
        request.headers["Authorization"] = "Negotiate " + base64.b64encode(token).decode()
        yield request
