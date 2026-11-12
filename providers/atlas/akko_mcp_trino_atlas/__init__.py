"""Apache Atlas context provider for akko-mcp-trino.

Registered under the entry point group ``akko_mcp_trino.context`` as ``atlas``, and
loaded only when ``MCP_CONTEXT_PROVIDERS`` names it. It reads what Atlas holds about
the tables Trino lists for the caller, with an account of its own and through the REST
API v2 only; it never writes to Atlas, and the data itself still flows through Trino
under the caller's identity.

``from_env`` reads the settings and refuses to start without an address, a way to
authenticate or ``ATLAS_CLUSTERS``. Three ways to authenticate are accepted: Basic
(``ATLAS_USER`` with ``ATLAS_PASSWORD`` or ``ATLAS_PASSWORD_FILE``), a Knox token
(``ATLAS_KNOX_TOKEN`` or ``ATLAS_KNOX_TOKEN_FILE``) and Kerberos (``ATLAS_AUTH=kerberos``
with ``ATLAS_PRINCIPAL`` and ``ATLAS_KEYTAB``). ``ATLAS_URL`` holds one address, or
several separated by commas for Atlas in high availability; a password or a token over plain
``http`` is refused, and Kerberos over ``http`` is said once at start. No message names an address
or a secret. Without ``ATLAS_GLOSSARIES``, ``ATLAS_CLASSIFICATIONS`` and
``ATLAS_BUSINESS_METADATA``, only descriptions are served.
"""

import logging
from collections.abc import Mapping

from .provider import AtlasProvider
from .settings import BASIC, KERBEROS, KNOX, AtlasSettings, read_settings

__version__ = "0.8.0"

__all__ = ["BASIC", "KERBEROS", "KNOX", "AtlasProvider", "AtlasSettings", "from_env"]

log = logging.getLogger(__name__)


def from_env(env: Mapping[str, str]) -> AtlasProvider:
    """Build the provider from the ``ATLAS_*`` settings; raises ``ValueError`` when one is
    wrong or missing, so the server refuses to start."""
    settings = read_settings(env)
    for warning in settings.warnings():
        log.warning(warning)
    return AtlasProvider(settings)
