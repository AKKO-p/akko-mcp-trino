"""Extensions: what another package adds to the server beyond tools, chosen by name at start.

A tool plugin (``akko_mcp_trino.tools``) adds tools. An extension may add more:
identity modes to reach Trino, guards at the door, fields of the audit line,
documents served without a token, routes behind the guard, client tags on the
queries, and tools. A package registers a factory ``(config, env) -> Extension``
under the entry point group ``akko_mcp_trino.extensions``; the operator names it
in ``MCP_EXTENSIONS``. Nothing is loaded that is not named, an unknown name
refuses to start, and so does a factory that raises ``ValueError`` on a setting
it cannot serve.

An extension never widens what the core allows. A guard may only refuse; it runs
after the core has verified the token, never instead of it, and the read-only
guard on SQL applies to every query whatever a guard answered. An identity mode
decides who the server is for Trino; Trino decides what that identity may read.

`Extension` is the base class: every hook does nothing, so an extension
overrides only what it adds.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from typing import TYPE_CHECKING, Any, Callable, Mapping, Optional, Sequence

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .auth import Principal
    from .config import Config
    from .trino_client import IdentityMode

ENTRY_POINT_GROUP = "akko_mcp_trino.extensions"


@dataclass(frozen=True)
class Refusal:
    """A guard's refusal: ``reason`` in ``X-Reason`` and in the body, never the token.

    ``error`` is the RFC 6750 error code a challenge may carry (``invalid_token``,
    ``insufficient_scope``); ``headers`` are added to the response (``Retry-After``);
    ``message``, when set, tells the client in the body what to do about it.
    """

    reason: str
    status: int = 401
    error: str = ""
    headers: Mapping[str, str] = field(default_factory=dict)
    message: str = ""


@dataclass
class Admission:
    """One request at the door, as the guards see it.

    ``bearer`` is the token the request presented, for a guard to read;
    ``principal`` is None before authentication and when no token verified;
    ``stdio`` is true for a stdio admission: once per process, at start, except
    with ``TRINO_IDENTITY_MODE=oauth2``, where the guards run on every request
    that is not discovery, never at start or for discovery, and always after
    Trino named the user, so ``before_auth`` already sees that ``principal``. A
    guard may ``bind`` a value to a context variable for the duration of the
    request (a value it derived from the request, for example); the middleware
    resets it afterwards.
    """

    headers: Mapping[str, str]
    bearer: Optional[str] = None
    principal: Optional["Principal"] = None
    stdio: bool = False
    bindings: list[tuple[ContextVar, Any]] = field(default_factory=list)

    def bind(self, var: ContextVar, value: Any) -> None:
        """Set ``var`` to ``value`` while the request runs."""
        self.bindings.append((var, value))


class Extension:
    """What an extension may add. Every hook of the base class adds nothing."""

    name = ""

    def check_lines(self) -> list[str]:
        """Lines ``--check`` prints for this extension, never a secret."""
        return []

    def identity_modes(self) -> Mapping[str, "IdentityMode"]:
        """Identity modes by ``TRINO_IDENTITY_MODE`` name; a ``service`` one replaces the core's."""
        return {}

    def audiences(self) -> list[str]:
        """Token audiences accepted besides ``MCP_OIDC_AUDIENCE``."""
        return []

    def before_auth(self, admission: Admission) -> Optional[Refusal]:
        """Refuse a request before its token is looked at, or None; in ``oauth2`` over
        stdio, Trino has already named the user (see `Admission`)."""
        return None

    def after_auth(self, admission: Admission) -> Optional[Refusal]:
        """Refuse a request once the core has verified its token (or found none), or None."""
        return None

    def public_documents(self) -> Mapping[str, dict]:
        """JSON documents served without a token, by request path."""
        return {}

    def challenge(self, refusal: Refusal) -> Optional[str]:
        """The ``WWW-Authenticate`` value of a refusal, or None for none."""
        return None

    def routes(self) -> list[Any]:
        """Starlette routes added to the served app, behind the identity guard."""
        return []

    def wrap_audit(self, audit: Any) -> Any:
        """The audit sink the server writes to, given the one it would write to otherwise."""
        return audit

    def client_tags(self) -> list[str]:
        """Client tags of a query run now (``X-Trino-Client-Tags``)."""
        return []

    def describe_table_doc(self) -> str:
        """Text appended to the description of ``describe_table``, for what a describer adds."""
        return ""

    def tool_registrars(self) -> list[Callable[..., None]]:
        """Tool registrars, called like a tool plugin's after the core's tools."""
        return []


Factory = Callable[["Config", Mapping[str, str]], Extension]


def _entry_points() -> Sequence[Any]:
    """The factories other packages register under ``akko_mcp_trino.extensions``."""
    return list(entry_points(group=ENTRY_POINT_GROUP))


def extension_names(value: str) -> list[str]:
    """The names a ``MCP_EXTENSIONS`` value lists, in order."""
    return [n.strip() for n in value.split(",") if n.strip()]


def load_extensions(config: "Config", env: Mapping[str, str]) -> list[Extension]:
    """The extensions named by ``config.extensions``, built from ``env``, in that order.

    Raises ValueError on an unknown name, and lets through the ValueError of a
    factory that cannot serve its settings: either way the server refuses to start.
    """
    names = extension_names(config.extensions)
    if not names:
        return []
    installed = {ep.name: ep for ep in _entry_points()}
    loaded: list[Extension] = []
    for name in names:
        if name not in installed:
            raise ValueError(
                f"unknown extension {name!r}; installed extensions: {sorted(installed) or 'none'}"
            )
        loaded.append(installed[name].load()(config, env))
    return loaded
