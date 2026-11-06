"""The provider's settings, read from the environment and checked once, at start.

Every check raises ``ValueError``, so the server refuses to start; no message names an
address, a proxy or a secret.
"""

import json
import ssl
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from urllib.parse import urlsplit

from akko_mcp_trino.context import FileSecret, secret_setting

from .spnego import load_spnego

BASIC, KNOX, KERBEROS = "basic", "knox", "kerberos"
# Settings of Basic and Knox, refused with Kerberos so that none is silently ignored.
_BASIC_SETTINGS = (
    "ATLAS_USER",
    "ATLAS_PASSWORD",
    "ATLAS_PASSWORD_FILE",
    "ATLAS_KNOX_TOKEN",
    "ATLAS_KNOX_TOKEN_FILE",
)
DEFAULT_TABLE_TYPES = ("hive_table", "iceberg_table")
# Under the 8 KiB request header limit of the Jetty that serves Atlas, request line included.
DEFAULT_MAX_QUERY_CHARS = 6000
DEFAULT_TIMEOUT_SECONDS = 5.0
DEFAULT_MAX_CONCURRENT_REQUESTS = 4
MAX_CONCURRENT_REQUESTS = 32


@dataclass(frozen=True)
class AtlasSettings:
    """The provider's settings, as ``from_env`` validated them.

    ``clusters`` maps a Trino catalog, folded to lower case, to its Atlas cluster. The
    three allow lists name, by exact name, the glossaries, classifications and business
    metadata sets that may be served; empty serves none. ``owner_attribute`` is
    ``<set>.<attribute>`` of business metadata, empty when no owner is served.
    """

    urls: tuple[str, ...]
    auth: str
    user: str = ""
    principal: str = ""
    keytab: str = field(default="", repr=False)
    spn: str = ""
    clusters: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    table_types: tuple[str, ...] = DEFAULT_TABLE_TYPES
    max_query_chars: int = DEFAULT_MAX_QUERY_CHARS
    glossaries: frozenset[str] = frozenset()
    classifications: frozenset[str] = frozenset()
    business_metadata: frozenset[str] = frozenset()
    owner_attribute: str = ""
    term_status_attribute: str = ""
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_concurrent_requests: int = DEFAULT_MAX_CONCURRENT_REQUESTS
    verify: ssl.SSLContext | bool = field(default=True, repr=False, compare=False)
    proxy: str = field(default="", repr=False)
    _secret: FileSecret | str = field(default="", repr=False)

    def secret(self) -> str:
        """Return the password or the Knox token, read again when its file was rotated."""
        return self._secret.value() if isinstance(self._secret, FileSecret) else self._secret

    def credentials_stamp(self) -> str:
        """Return a value that changes with the secret's file; empty without one."""
        return self._secret.stamp() if isinstance(self._secret, FileSecret) else ""


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
            raise ValueError(
                "ATLAS_URL: an address has a port that is not a valid number"
            ) from None
    return urls


def _names(env: Mapping[str, str], name: str) -> frozenset[str]:
    """The names of a comma-separated setting, as written; ``*`` is a name, never a wildcard."""
    return frozenset(part.strip() for part in env.get(name, "").split(",") if part.strip())


def _clusters(env: Mapping[str, str]) -> Mapping[str, str]:
    """``ATLAS_CLUSTERS``, its catalogs folded to lower case as Trino lists them."""
    text = env.get("ATLAS_CLUSTERS", "").strip()
    if not text:
        raise ValueError("ATLAS_CLUSTERS is not set")
    try:
        clusters = json.loads(text)
    except ValueError:
        raise ValueError("ATLAS_CLUSTERS is not JSON") from None
    if not isinstance(clusters, dict) or not clusters:
        raise ValueError("ATLAS_CLUSTERS must map each Trino catalog to an Atlas cluster")
    folded: dict[str, str] = {}
    for catalog, cluster in clusters.items():
        if not catalog.strip() or not isinstance(cluster, str) or not cluster.strip():
            raise ValueError("ATLAS_CLUSTERS must map each Trino catalog to an Atlas cluster")
        key = catalog.strip().lower()
        if key in folded:
            raise ValueError("ATLAS_CLUSTERS names a catalog twice once folded to lower case")
        folded[key] = cluster.strip()
    return MappingProxyType(folded)


def _positive(env: Mapping[str, str], name: str, default: float, kind: type) -> float:
    text = env.get(name, "").strip()
    if not text:
        return default
    try:
        value = kind(text)
    except ValueError:
        raise ValueError(f"{name} must be a positive number") from None
    if not value > 0:
        raise ValueError(f"{name} must be a positive number")
    return value


def _concurrency(env: Mapping[str, str]) -> int:
    text = env.get("ATLAS_MAX_CONCURRENT_REQUESTS", "").strip()
    if not text:
        return DEFAULT_MAX_CONCURRENT_REQUESTS
    if not text.isdecimal() or not 1 <= int(text) <= MAX_CONCURRENT_REQUESTS:
        raise ValueError(
            "ATLAS_MAX_CONCURRENT_REQUESTS must be an integer between 1 and "
            f"{MAX_CONCURRENT_REQUESTS}"
        )
    return int(text)


def _owner(env: Mapping[str, str]) -> str:
    owner = env.get("ATLAS_OWNER_ATTRIBUTE", "").strip()
    if owner:
        business, _, attribute = owner.partition(".")
        if not business or not attribute:
            raise ValueError("ATLAS_OWNER_ATTRIBUTE must read <business metadata>.<attribute>")
    return owner


def _verify(env: Mapping[str, str]) -> ssl.SSLContext | bool:
    path = env.get("ATLAS_CA_BUNDLE", "").strip()
    if not path:
        return True
    try:
        return ssl.create_default_context(cafile=path)
    except (OSError, ssl.SSLError) as exc:
        raise ValueError(f"ATLAS_CA_BUNDLE: cannot read the file ({type(exc).__name__})") from None


def _proxy(env: Mapping[str, str]) -> str:
    proxy = env.get("ATLAS_PROXY", "").strip()
    if proxy:
        parts = urlsplit(proxy)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("ATLAS_PROXY must be an http or https URL with a host")
    return proxy


def _auth(env: Mapping[str, str]) -> dict:
    """The authentication settings, by keyword of ``AtlasSettings``."""
    auth = env.get("ATLAS_AUTH", BASIC).strip().lower() or BASIC
    if auth == KERBEROS:
        for name in ("ATLAS_PRINCIPAL", "ATLAS_KEYTAB"):
            if not env.get(name, "").strip():
                raise ValueError(f"ATLAS_AUTH=kerberos needs {name}")
        if any(env.get(name, "").strip() for name in _BASIC_SETTINGS):
            raise ValueError("ATLAS_AUTH=kerberos does not take a user, a password or a token")
        spn = env.get("ATLAS_SPN", "").strip()
        service, at, host = spn.partition("@")
        if spn and (not at or not service or not host):
            raise ValueError("ATLAS_SPN must read <service>@<host>, such as HTTP@atlas.example")
        load_spnego()
        return {
            "spn": spn,
            "auth": KERBEROS,
            "principal": env["ATLAS_PRINCIPAL"].strip(),
            "keytab": env["ATLAS_KEYTAB"].strip(),
        }
    if auth != BASIC:
        raise ValueError("ATLAS_AUTH must be basic or kerberos")
    if any(env.get(name, "").strip() for name in ("ATLAS_PRINCIPAL", "ATLAS_KEYTAB", "ATLAS_SPN")):
        raise ValueError("ATLAS_PRINCIPAL, ATLAS_KEYTAB and ATLAS_SPN need ATLAS_AUTH=kerberos")
    user = env.get("ATLAS_USER", "").strip()
    password = secret_setting(env, "ATLAS_PASSWORD")
    token = secret_setting(env, "ATLAS_KNOX_TOKEN")
    if token and (user or password):
        raise ValueError("set either ATLAS_USER and ATLAS_PASSWORD, or ATLAS_KNOX_TOKEN")
    if token:
        return {"auth": KNOX, "_secret": token}
    if not user and not password:
        raise ValueError(
            "Atlas needs a way to authenticate: ATLAS_USER and ATLAS_PASSWORD, "
            "ATLAS_KNOX_TOKEN, or ATLAS_AUTH=kerberos"
        )
    if not password:
        raise ValueError("ATLAS_USER is set without ATLAS_PASSWORD or ATLAS_PASSWORD_FILE")
    if not user:
        raise ValueError("ATLAS_PASSWORD is set without ATLAS_USER")
    return {"auth": BASIC, "user": user, "_secret": password}


def read_settings(env: Mapping[str, str]) -> AtlasSettings:
    """Read and check every ``ATLAS_*`` setting; raises ``ValueError`` on the first wrong one."""
    urls = _urls(env)
    auth = _auth(env)
    types = tuple(
        dict.fromkeys(
            t.strip()
            for t in env.get("ATLAS_TABLE_TYPES", ",".join(DEFAULT_TABLE_TYPES)).split(",")
            if t.strip()
        )
    )
    if not types:
        raise ValueError("ATLAS_TABLE_TYPES names no entity type")
    return AtlasSettings(
        urls,
        clusters=_clusters(env),
        table_types=types,
        max_query_chars=int(_positive(env, "ATLAS_MAX_QUERY_CHARS", DEFAULT_MAX_QUERY_CHARS, int)),
        glossaries=_names(env, "ATLAS_GLOSSARIES"),
        classifications=_names(env, "ATLAS_CLASSIFICATIONS"),
        business_metadata=_names(env, "ATLAS_BUSINESS_METADATA"),
        owner_attribute=_owner(env),
        term_status_attribute=env.get("ATLAS_TERM_STATUS_ATTRIBUTE", "").strip(),
        timeout_seconds=_positive(env, "ATLAS_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS, float),
        max_concurrent_requests=_concurrency(env),
        verify=_verify(env),
        proxy=_proxy(env),
        **auth,
    )
