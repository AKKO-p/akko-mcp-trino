"""The context interface: types, protocol, errors, statuses, and the chain that reads it.

``CONTEXT_API`` is the version of the protocol. The core refuses a provider
whose ``context_api`` differs from it. An adapter finds here all it needs: the
protocol, the types, the errors, the statuses and ``secret_setting``; the contract
it must pass is ``akko_mcp_trino.testing``.
"""

from dataclasses import dataclass

from ..config import Config
from .chain import ContextChain, ContextSettings, Gathered
from .errors import ProviderError, ProviderRefused, ProviderUnavailable
from .loop import ContextLoop
from .provider import ContextProvider
from .secrets import FileSecret, secret_setting
from .status import (
    ERROR,
    NOT_CHECKED,
    NOT_FOUND,
    NOT_MAPPED,
    OK,
    PARTIAL,
    REFUSED,
    UNAVAILABLE,
    status_of,
)
from .types import (
    CLASSIFICATION,
    GLOSSARY,
    OWNER,
    PROPERTY,
    ColumnContext,
    Join,
    Labelled,
    TableContext,
    TableRef,
    Term,
    TermRef,
)

CONTEXT_API = 1


@dataclass(frozen=True)
class ContextSetup:
    """What the tools receive when context providers are loaded: the chain and the
    configuration whose bounds they apply."""

    chain: ContextChain
    config: Config


__all__ = [
    "CLASSIFICATION",
    "CONTEXT_API",
    "ERROR",
    "GLOSSARY",
    "NOT_CHECKED",
    "NOT_FOUND",
    "NOT_MAPPED",
    "OK",
    "OWNER",
    "PARTIAL",
    "PROPERTY",
    "REFUSED",
    "UNAVAILABLE",
    "ColumnContext",
    "ContextChain",
    "ContextLoop",
    "ContextProvider",
    "ContextSetup",
    "ContextSettings",
    "FileSecret",
    "Gathered",
    "Join",
    "Labelled",
    "ProviderError",
    "ProviderRefused",
    "ProviderUnavailable",
    "TableContext",
    "TableRef",
    "Term",
    "TermRef",
    "secret_setting",
    "status_of",
]
