"""Context providers: sources another package adds, chosen by name at start.

A provider factory ``(env) -> ContextProvider`` is registered under the entry point
group ``akko_mcp_trino.context`` and named in ``MCP_CONTEXT_PROVIDERS``. Nothing is
loaded that is not named, and an unknown name refuses to start: a typo must not
silently serve less context than the operator believes. A factory reads its own
settings from ``env`` and raises ``ValueError`` when it cannot serve them; it does not
reach its catalogue, so a catalogue that is down does not keep the server from starting.
"""

import asyncio
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
    try:
        for name in names:
            loaded.append(_build(name, names, installed, loaded, env))
    except Exception:
        close_providers(loaded)
        raise
    return loaded


def _build(
    name: str,
    names: Sequence[str],
    installed: Mapping[str, Any],
    loaded: Sequence[ContextProvider],
    env: Mapping[str, str],
) -> ContextProvider:
    """Build one provider; a failure other than a ValueError is named by its type only."""
    if name not in installed:
        raise ValueError(
            f"unknown context provider {name!r}; installed providers: {sorted(installed) or 'none'}"
        )
    if names.count(name) > 1:
        raise ValueError(f"context provider {name!r} is listed twice")
    try:
        provider = installed[name].load()(env)
    except ValueError:
        raise
    except Exception as exc:  # noqa: BLE001 - its text may hold a secret: only the type is kept
        raise ValueError(
            f"context provider {name!r} failed to load ({type(exc).__name__})"
        ) from None
    if provider.context_api != CONTEXT_API:
        raise ValueError(
            f"context provider {name!r} speaks context_api {provider.context_api}; "
            f"this server expects {CONTEXT_API}"
        )
    if any(other.name == provider.name for other in loaded):
        raise ValueError(f"more than one context provider reports the name {provider.name!r}")
    return provider


def close_providers(providers: Sequence[ContextProvider]) -> None:
    """Release what providers hold open; a failure to close is not raised."""
    for provider in providers:
        try:
            asyncio.run(provider.aclose())
        except Exception:  # noqa: BLE001 - closing never hides the reason it was needed
            pass
