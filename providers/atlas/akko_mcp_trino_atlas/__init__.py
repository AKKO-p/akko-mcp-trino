"""Apache Atlas context provider for akko-mcp-trino.

Registered under the entry point group ``akko_mcp_trino.context`` as ``atlas``, and
loaded only when ``MCP_CONTEXT_PROVIDERS`` names it. It reads what Atlas holds about
the tables Trino lists for the caller, with an account of its own and through the REST
API v2 only; it never writes to Atlas, and the data itself still flows through Trino
under the caller's identity.

``from_env`` reads the settings and refuses to start without an address or a way to
authenticate. Three ways are accepted: Basic (``ATLAS_USER`` with ``ATLAS_PASSWORD`` or
``ATLAS_PASSWORD_FILE``), a Knox token (``ATLAS_KNOX_TOKEN`` or ``ATLAS_KNOX_TOKEN_FILE``)
and Kerberos (``ATLAS_AUTH=kerberos`` with ``ATLAS_PRINCIPAL`` and ``ATLAS_KEYTAB``).
``ATLAS_URL`` holds one address, or several separated by commas for Atlas in high
availability. No message names an address or a secret.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from akko_mcp_trino.context import FileSecret, secret_setting

__version__ = "0.7.2"

BASIC, KNOX, KERBEROS = "basic", "knox", "kerberos"
# Settings of Basic and Knox, refused with Kerberos so that none is silently ignored.
_BASIC_SETTINGS = (
    "ATLAS_USER",
    "ATLAS_PASSWORD",
    "ATLAS_PASSWORD_FILE",
    "ATLAS_KNOX_TOKEN",
    "ATLAS_KNOX_TOKEN_FILE",
)


@dataclass(frozen=True)
class AtlasSettings:
    """The provider's settings, as ``from_env`` validated them."""

    urls: tuple[str, ...]
    auth: str
    user: str = ""
    principal: str = ""
    keytab: str = ""
    _secret: FileSecret | str = field(default="", repr=False)

    def secret(self) -> str:
        """Return the password or the Knox token, read again when its file was rotated."""
        return self._secret.value() if isinstance(self._secret, FileSecret) else self._secret


def _urls(env: Mapping[str, str]) -> tuple[str, ...]:
    """The addresses of ``ATLAS_URL``, without a trailing slash; the message never shows one."""
    urls = tuple(u.strip().rstrip("/") for u in env.get("ATLAS_URL", "").split(",") if u.strip())
    if not urls:
        raise ValueError("ATLAS_URL is not set")
    for url in urls:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("ATLAS_URL: each address must be an http or https URL with a host")
        if parts.username or parts.password:
            raise ValueError("ATLAS_URL: an address must not hold credentials")
        try:
            parts.port
        except ValueError:
            raise ValueError("ATLAS_URL: an address has a port that is not a number") from None
    return urls


def from_env(env: Mapping[str, str]) -> AtlasSettings:
    """Read and check the address and the authentication; raises ``ValueError`` when the
    provider could not authenticate, so the server refuses to start."""
    urls = _urls(env)
    auth = env.get("ATLAS_AUTH", BASIC).strip().lower() or BASIC
    if auth == KERBEROS:
        for name in ("ATLAS_PRINCIPAL", "ATLAS_KEYTAB"):
            if not env.get(name, "").strip():
                raise ValueError(f"ATLAS_AUTH=kerberos needs {name}")
        if any(env.get(name, "").strip() for name in _BASIC_SETTINGS):
            raise ValueError("ATLAS_AUTH=kerberos does not take a user, a password or a token")
        return AtlasSettings(
            urls,
            KERBEROS,
            principal=env["ATLAS_PRINCIPAL"].strip(),
            keytab=env["ATLAS_KEYTAB"].strip(),
        )
    if auth != BASIC:
        raise ValueError("ATLAS_AUTH must be basic or kerberos")
    if any(env.get(name, "").strip() for name in ("ATLAS_PRINCIPAL", "ATLAS_KEYTAB")):
        raise ValueError("ATLAS_PRINCIPAL and ATLAS_KEYTAB need ATLAS_AUTH=kerberos")
    user = env.get("ATLAS_USER", "").strip()
    password = secret_setting(env, "ATLAS_PASSWORD")
    token = secret_setting(env, "ATLAS_KNOX_TOKEN")
    if token and (user or password):
        raise ValueError("set either ATLAS_USER and ATLAS_PASSWORD, or ATLAS_KNOX_TOKEN")
    if token:
        return AtlasSettings(urls, KNOX, _secret=token)
    if not user and not password:
        raise ValueError(
            "Atlas needs a way to authenticate: ATLAS_USER and ATLAS_PASSWORD, "
            "ATLAS_KNOX_TOKEN, or ATLAS_AUTH=kerberos"
        )
    if not password:
        raise ValueError("ATLAS_USER is set without ATLAS_PASSWORD or ATLAS_PASSWORD_FILE")
    if not user:
        raise ValueError("ATLAS_PASSWORD is set without ATLAS_USER")
    return AtlasSettings(urls, BASIC, user=user, _secret=password)
