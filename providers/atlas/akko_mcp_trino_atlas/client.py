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
- a body longer than ``ATLAS_MAX_RESPONSE_BYTES`` once decoded, with a ``Content-Length``
  or without one, or in a content coding other than none, ``identity``, ``gzip`` or
  ``deflate``, or in more than one: the read stops there (or never starts) and the call is
  ``ProviderAnswerTooLarge``, for it alone. The address answered: no other address is tried,
  none is left, and the core opens no retry pause. One line per call, ``atlas answered more
  than ATLAS_MAX_RESPONSE_BYTES for one read; lower MCP_CONTEXT_BATCH`` or ``atlas answered
  in a content coding other than gzip or deflate``, names no table and no address. Only the
  body of a 200, 400, 403 or 404 is read: a 5xx, a redirect, a 401 and a Kerberos challenge
  are closed unread;
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
import json
import logging
import time
import zlib
from collections.abc import Generator, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from enum import Enum
from importlib.metadata import version
from types import MappingProxyType
from typing import Any

import httpx
from akko_mcp_trino.context import ProviderAnswerTooLarge, ProviderRefused, ProviderUnavailable

from .settings import BASIC, KERBEROS, KNOX, AtlasSettings
from .spnego import NegotiationError, SpnegoAuth

log = logging.getLogger(__name__)

USER_AGENT = f"akko-mcp-trino-atlas/{version('akko-mcp-trino-atlas')}"
SESSION_COOKIE = "ATLASSESSIONID"
# The passive server of Atlas in high availability redirects once to the active one.
_MAX_REDIRECTS = 3
_REFUSED = (401, 403)
# The statuses whose body ``_read`` looks at; any other answer is closed unread.
_READ_BODY = frozenset({200, 400, 403, 404})
# The content codings the client asks for, and the only ones it reads, one at most.
ACCEPT_ENCODING = "gzip, deflate"
# The most bytes one decoding step gives: the body is held, plus at most this much.
_STEP = 65_536
# The fixed texts of an answer refused for its call alone; ``--check`` prints them.
TOO_LARGE = "atlas answered more than ATLAS_MAX_RESPONSE_BYTES"
UNREAD_CODING = "atlas answered in a content coding other than gzip or deflate"


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


class _TooLarge(Exception):
    """The body of an answer is longer than ``ATLAS_MAX_RESPONSE_BYTES``."""


class _Coding(Exception):
    """The body of an answer is in a content coding the client does not read."""


class _Identity:
    """No content coding: the bytes as they come."""

    unconsumed_tail = b""

    @staticmethod
    def decompress(data: bytes, max_length: int) -> bytes:
        return data


class _Deflate:
    """``deflate``: zlib data, or raw deflate data as some servers send it."""

    def __init__(self) -> None:
        self._zlib = zlib.decompressobj()
        self._first = True

    @property
    def unconsumed_tail(self) -> bytes:
        return self._zlib.unconsumed_tail

    def decompress(self, data: bytes, max_length: int) -> bytes:
        if self._first:
            self._first = False
            try:
                return self._zlib.decompress(data, max_length)
            except zlib.error:
                self._zlib = zlib.decompressobj(-zlib.MAX_WBITS)
        return self._zlib.decompress(data, max_length)


def _decoder(response: httpx.Response) -> Any:
    """The decoder of the answer's ``Content-Encoding``: none, ``identity``, or exactly one
    of ``gzip`` and ``deflate``; any other coding, or more than one, raises ``_Coding``."""
    codings = [c.strip().lower() for c in response.headers.get("content-encoding", "").split(",")]
    codings = [c for c in codings if c and c != "identity"]
    if not codings:
        return _Identity()
    if codings == ["gzip"]:
        return zlib.decompressobj(16 + zlib.MAX_WBITS)
    if codings == ["deflate"]:
        return _Deflate()
    raise _Coding


class Watch:
    """The first tries a call is waiting on, shared by the call's child tasks: each one's
    address and the monotonic time it started. What is left in it when the call's deadline
    passes is what may not have answered in time. ``said`` holds the lines about an answer
    refused for this call alone (too large, or in a coding not read) the call has logged."""

    def __init__(self) -> None:
        """Start empty: no first try waiting, nothing said."""
        self.waiting: dict[object, tuple[int, float]] = {}
        self.said: set[str] = set()


_WATCH: ContextVar[Watch | None] = ContextVar("atlas_watch", default=None)
# The watch and the entry of the first try the current request waits on, if any.
_WAITING: ContextVar[tuple[Watch, object] | None] = ContextVar("atlas_waiting", default=None)


class _BearerAuth(httpx.Auth):
    """A Knox token, read again on each request so that a rotated file is followed."""

    def __init__(self, settings: AtlasSettings) -> None:
        self._settings = settings

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        request.headers["Authorization"] = f"Bearer {self._settings.secret()}"
        yield request


def _auth(settings: AtlasSettings) -> httpx.Auth | None:
    """Basic or Knox, sent with the request; Kerberos is negotiated by the client itself."""
    if settings.auth == BASIC:
        return httpx.BasicAuth(settings.user, settings.secret())
    if settings.auth == KNOX:
        return _BearerAuth(settings)
    return None


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
        "headers": {
            "X-XSRF-HEADER": "",
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Accept-Encoding": ACCEPT_ENCODING,
        },
    }


def _error_code(response: httpx.Response, body: bytearray) -> str:
    """The ``errorCode`` of an Atlas error answer, or an empty string."""
    if not _is_json(response):
        return ""
    try:
        doc = json.loads(body)
    except ValueError:
        return ""
    code = doc.get("errorCode") if isinstance(doc, dict) else None
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
                aside=self._aside,
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
                response, body = await self._send(
                    self._urls[index] + path, params, index, step == 0
                )
            except _Outage as outage:
                if outage.connect:
                    # A silent or closed address is not tried first again.
                    self._first = (index + 1) % count
                continue
            except _TooLarge:
                # The address answered, too much for one read: the call alone fails.
                raise self._refused_for_call(
                    TOO_LARGE, " for one read; lower MCP_CONTEXT_BATCH"
                ) from None
            except _Coding:
                raise self._refused_for_call(UNREAD_CODING) from None
            # The address that answered, after a passive server's redirect included.
            self._first = self._index(response.url, index)
            return self._read(response, body, missing)
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
    ) -> tuple[httpx.Response, bytearray]:
        """One request to one address, following redirects among the addresses only;
        ``index`` is the position of that address in ``ATLAS_URL``, ``first`` whether it is
        the address the call started with."""
        for _ in range(_MAX_REDIRECTS + 1):
            at = self._index(httpx.URL(url), index)
            try:
                response, body = await self._wait(at if first else None, url, params)
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
                return response, body
            target = response.url.join(location)
            if not self._ours(target):
                raise ProviderRefused("atlas redirected outside ATLAS_URL")
            url, params = str(target), None
        raise _Outage

    async def _wait(
        self, at: int | None, url: str, params: Mapping[str, str] | None
    ) -> tuple[httpx.Response, bytearray]:
        """The request and its body; while it waits, ``at`` (a first try's address) is in the
        call's watch, and stays there when the wait is cancelled, for ``leave`` to read."""
        watch = _WATCH.get()
        if at is None or watch is None:
            return await self._fetch(url, params)
        entry = object()
        watch.waiting[entry] = (at, time.monotonic())
        token = _WAITING.set((watch, entry))
        try:
            answer = await self._fetch(url, params)
        except asyncio.CancelledError:
            raise
        except BaseException:
            watch.waiting.pop(entry, None)
            raise
        finally:
            _WAITING.reset(token)
        watch.waiting.pop(entry, None)
        return answer

    async def _fetch(
        self, url: str, params: Mapping[str, str] | None
    ) -> tuple[httpx.Response, bytearray]:
        """Send the request, and with Kerberos answer a ``Negotiate`` challenge: the challenge
        is closed unread, and the request is sent again with a token. The body of the answer
        is read only when ``_read`` looks at it (see ``_body``); any other is closed unread."""
        request = self._http.build_request("GET", url, params=params)
        auth = _auth(self._settings)
        response = await self._http.send(request, auth=auth, stream=True)
        if self._kerberos is not None and self._kerberos.challenged(response):
            await response.aclose()
            request = await self._kerberos.answer(request)
            response = await self._http.send(request, auth=None, stream=True)
        try:
            body = bytearray()
            if response.status_code in _READ_BODY:
                body = await self._body(response)
            return response, body
        finally:
            await response.aclose()

    async def _body(self, response: httpx.Response) -> bytearray:
        """Read the body as it arrives and decode it in steps of at most 64 KiB, never more
        than the bytes left under ``ATLAS_MAX_RESPONSE_BYTES`` plus one: one byte more raises
        ``_TooLarge``, whatever ``Content-Length`` says, or without one. A coding the client
        does not read raises ``_Coding`` before anything is read."""
        decoder = _decoder(response)
        cap, body = self._settings.max_response_bytes, bytearray()
        if response.is_stream_consumed:
            # An in-memory transport handed the body already read, and httpx decoded it.
            body += response.content
            if len(body) > cap:
                raise _TooLarge
            return body
        try:
            async for data in response.aiter_raw():
                while True:
                    step = min(cap - len(body) + 1, _STEP)
                    out = decoder.decompress(data, step)
                    body += out
                    if len(body) > cap:
                        raise _TooLarge
                    data = decoder.unconsumed_tail
                    # A full step may leave output in the decoder, even with no input left.
                    if not data and len(out) < step:
                        break
        except zlib.error:
            raise httpx.DecodingError("atlas answered a body that cannot be decoded") from None
        return body

    @contextmanager
    def _aside(self) -> Iterator[None]:
        """A Kerberos negotiation waits on the KDC, not on the address: the address answered its
        challenge (an HTTP answer, noted as such), and the first try's entry leaves the watch
        while the negotiation runs, and comes back, its wait started anew, once the KDC
        answered. A negotiation cut by the deadline leaves no entry, so no address is left."""
        waiting = _WAITING.get()
        if waiting is None:
            yield
            return
        watch, entry = waiting
        at, _ = watch.waiting[entry]
        self._answered[at] = time.monotonic()
        del watch.waiting[entry]
        yield
        watch.waiting[entry] = (at, time.monotonic())

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

    def _read(self, response: httpx.Response, body: bytearray, missing: MissingCodes) -> Any:
        status = response.status_code
        if status == 403 and _error_code(response, body) == OBJECTS_REFUSED:
            return Missing.FORBIDDEN
        if status in _REFUSED:
            # A new session is opened on the next call, under the secret read then.
            self._http.cookies.clear()
            raise ProviderRefused(f"atlas answered {status}")
        if status != 200 and (found := missing.get((status, _error_code(response, body)))):
            return found
        if status != 200:
            raise self._unavailable(f"atlas answered {status}")
        if not _is_json(response):
            raise self._unavailable("atlas answered a page that is not JSON")
        try:
            return json.loads(body)
        except ValueError:
            raise self._unavailable("atlas answered JSON that cannot be read") from None

    @staticmethod
    def _refused_for_call(why: str, advice: str = "") -> ProviderAnswerTooLarge:
        """The call's error for an answer refused for this call alone, said once per call
        with ``advice``: the address answered, it is not an outage, and no retry pause
        opens."""
        watch = _WATCH.get()
        if watch is None or why not in watch.said:
            if watch is not None:
                watch.said.add(why)
            log.warning("%s%s", why, advice)
        return ProviderAnswerTooLarge(why)

    @staticmethod
    def _unavailable(why: str) -> ProviderUnavailable:
        log.warning("atlas unreachable")
        return ProviderUnavailable(why)

    async def aclose(self) -> None:
        """Close the connections, and the threads of Kerberos negotiations."""
        await self._http.aclose()
        if self._kerberos is not None:
            self._kerberos.close()
