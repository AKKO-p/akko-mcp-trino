"""Kerberos (SPNEGO) for Atlas, from a keytab, with ``pyspnego`` (the ``kerberos`` extra).

The provider answers a ``401`` that carries ``WWW-Authenticate: Negotiate`` with an
``Authorization: Negotiate`` token. The client closes that ``401`` without reading its body
(``challenged``), then sends the request again with the token (``answer``). Each negotiation
builds a new security context from ``spnego.KerberosKeytab``: ``pyspnego`` gets a new ticket
from the keytab and keeps it in a cache of its own in memory (``MEMORY:``), so an expired
ticket is never used and no process of its own renews one; each negotiation is thus one
request to the KDC. A redirect of Atlas's passive server to the active one is a new request,
so the negotiation starts again on the new host. Once Atlas has opened a session, its
``ATLASSESSIONID`` cookie is sent and no new negotiation happens until Atlas asks for one.
Atlas's own mutual authentication token is not checked: TLS authenticates Atlas.

The service name is ``ATLAS_SPN`` when set (``HTTP@<host>``), else ``HTTP@<host of the
request>``. The token is SPNEGO (``protocol="negotiate"``), as ``curl --negotiate`` sends.
No message, log or exception names the principal, the keytab or the token.

The negotiation (the keytab read, the requests to the KDC, krb5's name lookups) blocks, so
the provider's asynchronous client runs it on a thread pool of its own, at most
``workers`` at once (``ATLAS_MAX_CONCURRENT_REQUESTS``): the event loop serves every other
read meanwhile, and the call's deadline cuts the wait. A negotiation still running when its
call is cut goes on in its thread until the KDC answers, and its result is dropped; one that
waits for a thread when its call is cut never starts. When every thread is held by a
negotiation whose call was cut, which is taken as a KDC that stopped answering, a new
negotiation does not wait for a thread: it fails at once as a KDC that cannot be reached,
within its call's deadline. The
client sets the address's wait aside while a negotiation runs (``aside``): the time spent on
the KDC is not a wait on Atlas.

A negotiation that fails is a refusal of the account (``NegotiationError`` with
``unreachable`` false), since a retry every few seconds with a keytab the KDC refuses would
lock the account in a directory that counts failed pre-authentications; only a KDC that
cannot be found or reached (the Kerberos codes of ``KDC_UNREACHABLE``) is an outage.
"""

import asyncio
import base64
import threading
from collections.abc import AsyncGenerator, Callable, Generator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import AbstractContextManager, nullcontext
from typing import Any

import httpx

_NEGOTIATE = "negotiate"
# MIT Kerberos codes (krb5.h) of a KDC that was never reached: KRB5_KDC_UNREACH (-1765328228,
# cannot contact any KDC), KRB5_REALM_UNKNOWN (-1765328230, cannot find a KDC for the realm)
# and KRB5_REALM_CANT_RESOLVE (-1765328164, cannot resolve its address). Any other failure,
# KRB5_SERVICE_UNKNOWN (-1765328229) included, reached the KDC, or the keytab.
KDC_UNREACHABLE = frozenset(code & 0xFFFFFFFF for code in (-1765328228, -1765328230, -1765328164))


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

    def __init__(
        self,
        principal: str,
        keytab: str,
        spn: str,
        workers: int = 1,
        aside: Callable[[], AbstractContextManager[None]] = nullcontext,
    ) -> None:
        """Keep the names; ``spnego`` is imported now, nothing is read from the keytab yet.
        ``workers`` bounds the negotiations an asynchronous client runs at once; each runs
        inside ``aside()``."""
        self._spnego = load_spnego()
        self._principal = principal
        self._keytab = keytab
        self._spn = spn
        self._workers = workers
        self._aside = aside
        # Threads start on the first negotiation only.
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="atlas-kerberos")
        # The negotiations under way whose call is gone; their threads end when the KDC answers.
        self._stale: set[Future[bytes]] = set()
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        """Name the class only, never the principal or the keytab."""
        return "SpnegoAuth()"

    def close(self) -> None:
        """Drop the negotiations not started; one under way ends on its own, unwaited."""
        self._pool.shutdown(wait=False, cancel_futures=True)

    def _service(self, request: httpx.Request) -> tuple[str, str]:
        service, at, host = self._spn.partition("@")
        if self._spn and at:
            return service, host
        return "HTTP", request.url.host

    def _token(self, request: httpx.Request) -> bytes:
        """Build a new security context and step it; this blocks on the KDC."""
        service, host = self._service(request)
        try:
            credential = self._spnego.KerberosKeytab(keytab=self._keytab, principal=self._principal)
            context = self._spnego.client(
                username=credential, hostname=host, service=service, protocol="negotiate"
            )
            token: bytes = context.step()
        except Exception as exc:  # noqa: BLE001 - spnego's errors may name the principal
            unreachable = bool(_codes(exc) & KDC_UNREACHABLE)
            raise NegotiationError(unreachable) from None
        return token

    @staticmethod
    def _answer(request: httpx.Request, token: bytes) -> httpx.Request:
        request.headers["Authorization"] = "Negotiate " + base64.b64encode(token).decode()
        return request

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        """Send the request; on a ``Negotiate`` challenge, send it again with a token."""
        response = yield request
        if response.status_code == 401 and _challenge(response) is not None:
            yield self._answer(request, self._token(request))

    async def async_auth_flow(
        self, request: httpx.Request
    ) -> AsyncGenerator[httpx.Request, httpx.Response]:
        """As ``auth_flow``, the negotiation on the pool's threads, never on the event loop.
        httpx reads the challenge's body before it sends the request again: the provider's
        client does not use this flow, it calls ``challenged`` and ``answer`` itself."""
        response = yield request
        if self.challenged(response):
            yield await self.answer(request)

    @staticmethod
    def challenged(response: httpx.Response) -> bool:
        """Whether ``response`` is a ``401`` that carries a ``Negotiate`` challenge."""
        return response.status_code == 401 and _challenge(response) is not None

    async def answer(self, request: httpx.Request) -> httpx.Request:
        """``request`` with the token of a new negotiation, run on the pool's threads inside
        ``aside()``."""
        with self._aside():
            token = await self._negotiate(request)
        return self._answer(request, token)

    async def _negotiate(self, request: httpx.Request) -> bytes:
        """Run ``_token`` on the pool; fail at once when every thread holds a negotiation whose
        call is gone, since the next one would wait for the KDC that holds them."""
        with self._lock:
            if len(self._stale) >= self._workers:
                raise NegotiationError(unreachable=True)
        future = self._pool.submit(self._token, request)
        try:
            return await asyncio.wrap_future(future)
        except asyncio.CancelledError:
            # Not started: it never will. Under way: its thread stays held until the KDC answers.
            if not future.cancel():
                with self._lock:
                    self._stale.add(future)
                future.add_done_callback(self._ended)
            raise

    def _ended(self, future: Future[bytes]) -> None:
        with self._lock:
            self._stale.discard(future)
