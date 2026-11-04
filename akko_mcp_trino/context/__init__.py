"""The context interface: types, protocol, errors, statuses, and the chain that reads it.

``CONTEXT_API`` is the version of the protocol. The core refuses a provider
whose ``context_api`` differs from it.
"""

from .chain import ContextChain, ContextSettings, Gathered
from .errors import ProviderError, ProviderRefused, ProviderUnavailable
from .loop import ContextLoop
from .provider import ContextProvider
from .status import status_of
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

__all__ = [
    "CLASSIFICATION",
    "CONTEXT_API",
    "GLOSSARY",
    "OWNER",
    "PROPERTY",
    "ColumnContext",
    "ContextChain",
    "ContextLoop",
    "ContextProvider",
    "ContextSettings",
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
    "status_of",
]
