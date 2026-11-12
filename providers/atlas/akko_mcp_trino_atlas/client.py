"""HTTP to Atlas: the addresses of ``ATLAS_URL`` in turn, redirects kept among them.

The client never reads the environment (``trust_env=False``: no implicit proxy, no netrc),
goes through ``ATLAS_PROXY`` only when it is set, and checks certificates against
``ATLAS_CA_BUNDLE`` when it is set. It sends ``X-XSRF-HEADER`` and a library
``User-Agent`` (Atlas's CSRF filter aims at browsers), and keeps the ``ATLASSESSIONID``
cookie so that a session is not opened again on every call.

What an answer means:

- 401 or 403, or a redirect to an address outside ``ATLAS_URL`` (a Knox login on another
  host): ``ProviderRefused``, at once, with no other address tried, so that no loop of
  attempts locks the account. A 403 with ``ATLAS-403-00-001`` is not a refusal of the
  account: Atlas refused it some of the objects asked, and the read is ``Missing.FORBIDDEN``;
- a refused connection, a connection that does not open within
  ``ATLAS_CONNECT_TIMEOUT_SECONDS``, a timeout, a body that cannot be decoded or a 5xx: the
  next address is tried; when none answers, ``ProviderUnavailable``. An address that could
  not be connected to is left for the next one in the calls that follow, and so is the
  address a call started with when it did not answer in time: a read or write timeout, or the
  call's own deadline reached after it waited on it for at least
  ``ATLAS_CONNECT_TIMEOUT_SECONDS``, or for any time when it had not answered since
  ``ATLAS_TIMEOUT_SECONDS`` before the wait (``leave``, from the provider). Any other
  cancellation (a sibling read cancelled by a failure, a client gone, a shutdown), a shorter
  wait on an address that had just answered and any other transport error leave nothing. An
  address that did not answer in time, the only one included, is logged once, by its
  position in ``ATLAS_URL``, until it answers again;
- a 200 that is not JSON (a Knox login page), JSON that cannot be read, or any other
  status: ``ProviderUnavailable``;
- Atlas's own "does not exist" answers, by status and error code, only those the read
  names as its own (``missing``): ``INSTANCE_BY_ATTRIBUTE``, a 404 with
  ``ATLAS-404-00-009`` (no instance has that unique attribute); ``INSTANCE_BY_GUID``, a
  404 with ``ATLAS-404-00-005`` (no instance has that GUID); ``UNKNOWN_TYPE``, a 400 with
  ``ATLAS-400-00-014`` (the type does not exist; Atlas 2.4 without the Iceberg model).
  Any other 400 or 404 (a page, another code, a code another read expects, a wrong path
  behind Knox) is ``ProviderUnavailable``.

Each ``ProviderUnavailable`` logs one line, ``atlas unreachable``, word for word, with no
address, user or object name; a refusal is logged by the server, as
``atlas refused the account``. No error message holds an address, an answer's text or the
secret.
"""

import asyncio
import logging
import time
from collections.abc import Generator, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from enum import Enum
from importlib.metadata import version
from types import MappingProxyType
from typing import Any

import httpx
from akko_mcp_trino.context import ProviderRefused, ProviderUnavailable

from .settings import BASIC, KERBEROS, KNOX, AtlasSettings
from .spnego import NegotiationError, SpnegoAuth

log = logging.getLogger(__name__)

USER_AGENT = f"akko-mcp-trino-atlas/{version('akko-mcp-trino-atlas')}"
SESSION_COOKIE = "ATLASSESSIONID"
# The passive server of Atlas in high availability redirects once to the active one.
_MAX_REDIRECTS = 3
_REFUSED = (401, 403)


class Missing(Enum):
    """An answer by which Atlas says that what was asked does not exist."""

    INSTANCE = "instance"
    TYPE = "type"
    FORBIDDEN = "forbidden"


MissingCodes = Mapping[tuple[int, str], Missing]
# Atlas's "does not exist" answers, by status and error code; each read names those that
# mean "nothing" for it, and every other one is an outage.
INSTANCE_BY_ATTRIBUTE: MissingCodes = MappingProxyType(
    {(404, "ATLAS-404-00-009"): Missing.INSTANCE}
)
INSTANCE_BY_GUID: MissingCodes = MappingProxyType({(404, "ATLAS-404-00-005"): Missing.INSTANCE})
UNKNOWN_TYPE: MissingCodes = MappingProxyType({(400, "ATLAS-400-00-014"): Missing.TYPE})
_NONE: MissingCodes = MappingProxyType({})
# Atlas's answer when the account may not read one of the objects asked (Atlas's
# UNAUTHORIZED_ACCESS): the account is open, those objects are not served.
OBJECTS_REFUSED = "ATLAS-403-00-001"


class _Outage(Exception):
    """This address could not answer; the next one may. ``connect`` says no connection
    could be opened to it."""

    def __init__(self, connect: bool = False) -> None:
        super().__init__("outage")
        self.connect = connect


class Watch:
    """The first tries a call is waiting on, shared by the call's child tasks: each one's
    address and the monotonic time it started. What is left in it when the call's deadline
    passes is what may not have answered in time."""

    def __init__(self) -> None:
        """Start empty: no first try waiting."""
        self.waiting: dict[object, tuple[int, float]] = {}


_WATCH: ContextVar[Watch | None] = ContextVar("atlas_watch", default=None)


class _BearerAuth(httpx.Auth):
    """A Knox token, read again on each request so that a rotated file is followed."""

    def __init__(self, settings: AtlasSettings) -> None:
        self._settings = settings

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        request.headers["Authorization"] = f"Bearer {self._settings.secret()}"
        yield request


def _auth(settings: AtlasSettings, kerberos: SpnegoAuth | None) -> httpx.Auth | None:
    if settings.auth == BASIC:
        return httpx.BasicAuth(settings.user, settings.secret())
    if settings.auth == KNOX:
        return _BearerAuth(settings)
    return kerberos


def client_options(settings: AtlasSettings) -> dict[str, Any]:
    """The keywords of the ``httpx.AsyncClient`` the provider opens on the network."""
    return {
        "trust_env": False,
        "verify": settings.verify,
        "proxy": settings.proxy or None,
        "timeout": httpx.Timeout(
            settings.timeout_seconds, connect=settings.connect_timeout_seconds
        ),
        "follow_redirects": False,
        "headers": {"X-XSRF-HEADER": "", "User-Agent": USER_AGENT, "Accept": "application/json"},
    }


def _error_code(response: httpx.Response) -> str:
    """The ``errorCode`` of an Atlas error answer, or an empty string."""
    if not _is_json(response):
        return ""
    try:
        body = response.json()
    except ValueError:
        return ""
    code = body.get("errorCode") if isinstance(body, dict) else None
    return code if isinstance(code, str) else ""


def _is_json(response: httpx.Response) -> bool:
    kind = response.headers.get("content-type", "").split(";")[0].strip().lower()
    return kind == "application/json" or kind.endswith("+json")


class AtlasClient:
    """Reads JSON from Atlas, trying the addresses of ``ATLAS_URL`` in turn."""

    def __init__(
        self, settings: AtlasSettings, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        """Open the client; ``transport`` replaces the network, its proxy included."""
        self._settings = settings
        self._urls = settings.urls
        self._first = 0
        # The addresses already said not to answer in time, until one answers again.
        self._said: set[int] = set()
        # The monotonic time of the last HTTP answer from each address.
        self._answered: dict[int, float] = {}
        self._kerberos = (
            SpnegoAuth(
                settings.principal,
                settings.keytab,
                settings.spn,
                workers=settings.max_concurrent_requests,
            )
            if settings.auth == KERBEROS
            else None
        )
        options = client_options(settings)
        if transport is not None:
            options.pop("proxy")
        self._http = httpx.AsyncClient(transport=transport, **options)

    async def get(
        self,
        path: str,
        params: Mapping[str, str] | None = None,
        *,
        missing: MissingCodes = _NONE,
    ) -> Any:
        """Return the JSON Atlas answers for ``path`` (under ``/api/atlas``), or the
        ``Missing`` that ``missing`` gives the answer's status and error code; raises
        ``ProviderRefused`` or ``ProviderUnavailable``."""
        count, start = len(self._urls), self._first
        for step in range(count):
            index = (start + step) % count
            try:
                response = await self._send(self._urls[index] + path, params, index, step == 0)
            except _Outage as outage:
                if outage.connect:
                    # A silent or closed address is not tried first again.
                    self._first = (index + 1) % count
                continue
            # The address that answered, after a passive server's redirect included.
            self._first = self._index(response.url, index)
            return self._read(response, missing)
        raise self._unavailable("no address of ATLAS_URL answered")

    @contextmanager
    def watch(self) -> Iterator[Watch]:
        """Note, for the call made inside, the addresses its first tries wait on."""
        watch = Watch()
        token = _WATCH.set(watch)
        try:
            yield watch
        finally:
            _WATCH.reset(token)

    def leave(self, watch: Watch) -> None:
        """The call's deadline passed: leave the addresses its first tries still waited on,
        when the wait lasted at least ``ATLAS_CONNECT_TIMEOUT_SECONDS`` (the time an address
        is given to show it is alive), or when the address has not answered since
        ``ATLAS_TIMEOUT_SECONDS`` before the wait started. A shorter wait on an address that
        answered just before says nothing of it: the time had gone on another address, or on
        another read of the same tool call."""
        now, floor = time.monotonic(), self._settings.connect_timeout_seconds
        late = {
            index
            for index, started in watch.waiting.values()
            if now - started >= floor or not self._answered_since(index, started)
        }
        for index in sorted(late):
            self._late(index)

    def _answered_since(self, index: int, started: float) -> bool:
        """Whether the address ``index`` answered within ``ATLAS_TIMEOUT_SECONDS`` before
        ``started``, or after it."""
        answered = self._answered.get(index)
        return answered is not None and answered >= started - self._settings.timeout_seconds

    async def _send(
        self, url: str, params: Mapping[str, str] | None, index: int, first: bool
    ) -> httpx.Response:
        """One request to one address, following redirects among the addresses only;
        ``index`` is the position of that address in ``ATLAS_URL``, ``first`` whether it is
        the address the call started with."""
        for _ in range(_MAX_REDIRECTS + 1):
            at = self._index(httpx.URL(url), index)
            try:
                auth = _auth(self._settings, self._kerberos) or httpx.USE_CLIENT_DEFAULT
                response = await self._wait(at if first else None, url, params, auth)
            except (httpx.ConnectError, httpx.ConnectTimeout):
                raise _Outage(connect=True) from None
            except (httpx.ReadTimeout, httpx.WriteTimeout):
                # Connected, then no answer in time: a frozen server, or the active one stuck.
                if first:
                    self._late(at)
                raise _Outage from None
            except (httpx.TransportError, httpx.DecodingError):
                raise _Outage from None
            except NegotiationError as failed:
                # The same credentials fail on every address: none is tried.
                if failed.unreachable:
                    raise self._unavailable("the kerberos KDC could not be reached") from None
                self._http.cookies.clear()
                raise ProviderRefused("the kerberos negotiation was refused") from None
            self._said.discard(at)
            self._answered[at] = time.monotonic()
            if response.status_code >= 500:
                raise _Outage
            location = response.headers.get("location")
            # A redirect without a location is an answer like another: not followed.
            if not response.is_redirect or not location:
                return response
            target = response.url.join(location)
            if not self._ours(target):
                raise ProviderRefused("atlas redirected outside ATLAS_URL")
            url, params = str(target), None
        raise _Outage

    async def _wait(
        self, at: int | None, url: str, params: Mapping[str, str] | None, auth: Any
    ) -> httpx.Response:
        """The request; while it waits, ``at`` (a first try's address) is in the call's
        watch, and stays there when the wait is cancelled, for ``leave`` to read."""
        watch = _WATCH.get()
        if at is None or watch is None:
            return await self._http.get(url, params=params, auth=auth)
        entry = object()
        watch.waiting[entry] = (at, time.monotonic())
        try:
            response = await self._http.get(url, params=params, auth=auth)
        except asyncio.CancelledError:
            raise
        except BaseException:
            del watch.waiting[entry]
            raise
        del watch.waiting[entry]
        return response

    def _late(self, index: int) -> None:
        """The address ``index`` did not answer in time: the next calls start after it when
        it is still the first and another address exists, and it is said once until it
        answers again."""
        count = len(self._urls)
        if count > 1 and self._first == index:
            self._first = (index + 1) % count
        if index not in self._said:
            self._said.add(index)
            log.warning(
                "atlas address %d of %d in ATLAS_URL did not answer in time", index + 1, count
            )

    def _ours(self, url: httpx.URL) -> bool:
        """Whether ``url`` lies under one of the addresses of ``ATLAS_URL``."""
        return self._index(url, -1) >= 0

    def _index(self, url: httpx.URL, default: int) -> int:
        """The index of the address of ``ATLAS_URL`` that ``url`` lies under, or ``default``."""
        for index, base in enumerate(self._urls):
            own = httpx.URL(base)
            if (url.scheme, url.host, url.port) == (own.scheme, own.host, own.port) and (
                url.path == own.path or url.path.startswith(own.path.rstrip("/") + "/")
            ):
                return index
        return default

    def _read(self, response: httpx.Response, missing: MissingCodes) -> Any:
        status = response.status_code
        if status == 403 and _error_code(response) == OBJECTS_REFUSED:
            return Missing.FORBIDDEN
        if status in _REFUSED:
            # A new session is opened on the next call, under the secret read then.
            self._http.cookies.clear()
            raise ProviderRefused(f"atlas answered {status}")
        if status != 200 and (found := missing.get((status, _error_code(response)))):
            return found
        if status != 200:
            raise self._unavailable(f"atlas answered {status}")
        if not _is_json(response):
            raise self._unavailable("atlas answered a page that is not JSON")
        try:
            return response.json()
        except ValueError:
            raise self._unavailable("atlas answered JSON that cannot be read") from None

    @staticmethod
    def _unavailable(why: str) -> ProviderUnavailable:
        log.warning("atlas unreachable")
        return ProviderUnavailable(why)

    async def aclose(self) -> None:
        """Close the connections, and the threads of Kerberos negotiations."""
        await self._http.aclose()
        if self._kerberos is not None:
            self._kerberos.close()
