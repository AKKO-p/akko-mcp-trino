"""HTTP to Atlas: the addresses of ``ATLAS_URL`` in turn, redirects kept among them.

The client never reads the environment (``trust_env=False``: no implicit proxy, no netrc),
goes through ``ATLAS_PROXY`` only when it is set, and checks certificates against
``ATLAS_CA_BUNDLE`` when it is set. It sends ``X-XSRF-HEADER`` and a library
``User-Agent`` (Atlas's CSRF filter aims at browsers), and keeps the ``ATLASSESSIONID``
cookie so that a session is not opened again on every call.

What an answer means:

- 401 or 403, or a redirect to an address outside ``ATLAS_URL`` (a Knox login on another
  host): ``ProviderRefused``, at once, with no other address tried, so that no loop of
  attempts locks the account;
- a refused connection, a timeout or a 5xx: the next address is tried; when none answers,
  ``ProviderUnavailable``;
- a 200 that is not JSON (a Knox login page), JSON that cannot be read, or any other
  status: ``ProviderUnavailable``;
- Atlas's own "does not exist" answers, by their error code: a 404 with
  ``ATLAS-404-00-009`` (no instance has that unique attribute) or ``ATLAS-404-00-005``
  (no instance has that GUID, a glossary term read by GUID) is ``Missing.INSTANCE``,
  and a 400 with ``ATLAS-400-00-014`` (the type does not exist; Atlas 2.4 without the
  Iceberg model) is ``Missing.TYPE``. Any other 400 or 404 (a page, another code, a wrong
  path behind Knox) is ``ProviderUnavailable``.

Each ``ProviderUnavailable`` logs one line, ``atlas unreachable``, word for word, with no
address, user or object name; a refusal is logged by the server, as
``atlas refused the account``. No error message holds an address, an answer's text or the
secret.
"""

import logging
from collections.abc import Generator, Mapping
from enum import Enum
from importlib.metadata import version
from typing import Any

import httpx
from akko_mcp_trino.context import ProviderRefused, ProviderUnavailable

from .settings import BASIC, KNOX, AtlasSettings

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


# Atlas's error codes for "does not exist", by status: by unique attribute and by GUID
# (Atlas's INSTANCE_GUID_NOT_FOUND), and an entity type.
_MISSING = {
    404: {"ATLAS-404-00-009": Missing.INSTANCE, "ATLAS-404-00-005": Missing.INSTANCE},
    400: {"ATLAS-400-00-014": Missing.TYPE},
}


class _Outage(Exception):
    """This address could not answer; the next one may."""


class _BearerAuth(httpx.Auth):
    """A Knox token, read again on each request so that a rotated file is followed."""

    def __init__(self, settings: AtlasSettings) -> None:
        self._settings = settings

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        request.headers["Authorization"] = f"Bearer {self._settings.secret()}"
        yield request


def _auth(settings: AtlasSettings) -> httpx.Auth | None:
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
        "timeout": settings.timeout_seconds,
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
        options = client_options(settings)
        if transport is not None:
            options.pop("proxy")
        self._http = httpx.AsyncClient(transport=transport, **options)

    async def get(self, path: str, params: Mapping[str, str] | None = None) -> Any:
        """Return the JSON Atlas answers for ``path`` (under ``/api/atlas``), or ``Missing``
        when Atlas says it does not exist; raises ``ProviderRefused`` or
        ``ProviderUnavailable``."""
        count = len(self._urls)
        for step in range(count):
            index = (self._first + step) % count
            try:
                response = await self._send(self._urls[index] + path, params)
            except _Outage:
                continue
            self._first = index
            return self._read(response)
        raise self._unavailable("no address of ATLAS_URL answered")

    async def _send(self, url: str, params: Mapping[str, str] | None) -> httpx.Response:
        """One request to one address, following redirects among the addresses only."""
        for _ in range(_MAX_REDIRECTS + 1):
            try:
                response = await self._http.get(
                    url, params=params, auth=_auth(self._settings) or httpx.USE_CLIENT_DEFAULT
                )
            except httpx.TransportError:
                raise _Outage from None
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

    def _ours(self, url: httpx.URL) -> bool:
        """Whether ``url`` lies under one of the addresses of ``ATLAS_URL``."""
        for base in self._urls:
            own = httpx.URL(base)
            if (url.scheme, url.host, url.port) == (own.scheme, own.host, own.port) and (
                url.path == own.path or url.path.startswith(own.path.rstrip("/") + "/")
            ):
                return True
        return False

    def _read(self, response: httpx.Response) -> Any:
        status = response.status_code
        if status in _REFUSED:
            # A new session is opened on the next call, under the secret read then.
            self._http.cookies.clear()
            raise ProviderRefused(f"atlas answered {status}")
        if missing := _MISSING.get(status, {}).get(_error_code(response)):
            return missing
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
        """Close the connections."""
        await self._http.aclose()
