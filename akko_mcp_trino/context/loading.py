"""Context providers: sources another package adds, chosen by name at start.

A provider factory ``(env) -> ContextProvider`` is registered under the entry point
group ``akko_mcp_trino.context`` and named in ``MCP_CONTEXT_PROVIDERS``. Nothing is
loaded that is not named, and an unknown name refuses to start: a typo must not
silently serve less context than the operator believes. A factory reads its own
settings from ``env`` and raises ``ValueError`` when it cannot serve them; it does not
reach its catalogue, so a catalogue that is down does not keep the server from starting.
"""

from collections.abc import Callable, Mapping, Sequence
from importlib.metadata import entry_points
from typing import Any

from ..config import Config, provider_names
from . import CONTEXT_API
from .provider import ContextProvider

ENTRY_POINT_GROUP = "akko_mcp_trino.context"

Factory = Callable[[Mapping[str, str]], ContextProvider]


def _entry_points() -> Sequence[Any]:
    """The factories other packages register under ``akko_mcp_trino.context``."""
    return list(entry_points(group=ENTRY_POINT_GROUP))


def load_providers(config: Config, env: Mapping[str, str]) -> list[ContextProvider]:
    """The providers named by ``config.context_providers``, built from ``env``, in that order.

    Raises ValueError on an unknown or repeated name, on two providers that report the same
    name and on a provider of another ``context_api``, and lets through the ValueError of
    a factory that cannot serve its settings: either way the server refuses to start.
    """
    names = provider_names(config.context_providers)
    if not names:
        return []
    installed = {ep.name: ep for ep in _entry_points()}
    loaded: list[ContextProvider] = []
    for name in names:
        if name not in installed:
            raise ValueError(
                f"unknown context provider {name!r}; "
                f"installed providers: {sorted(installed) or 'none'}"
            )
        if names.count(name) > 1:
            raise ValueError(f"context provider {name!r} is listed twice")
        provider = installed[name].load()(env)
        if provider.context_api != CONTEXT_API:
            raise ValueError(
                f"context provider {name!r} speaks context_api {provider.context_api}; "
                f"this server expects {CONTEXT_API}"
            )
        if any(other.name == provider.name for other in loaded):
            raise ValueError(f"more than one context provider reports the name {provider.name!r}")
        loaded.append(provider)
    return loaded
