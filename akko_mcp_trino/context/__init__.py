"""The context interface: types, protocol, errors and statuses.

``CONTEXT_API`` is the version of the protocol. The core refuses a provider
whose ``context_api`` differs from it.
"""

from .errors import ProviderError, ProviderRefused, ProviderUnavailable
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
    "ContextProvider",
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
