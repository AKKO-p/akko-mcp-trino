"""Identity of the current request, forwarded to Trino as X-Trino-User.

Standard ASGI pattern: a per-request middleware resolves the verified Principal
and stores it in a ContextVar; the tools read `current_subject()` to run the
query UNDER the user's identity rather than the service account. With no
resolved identity (auth disabled, no token), `current_subject()` is None and the
service account is used. Pure, and fully testable.
"""
from __future__ import annotations

import contextvars
from typing import Optional

from .auth import Principal

_current_principal: contextvars.ContextVar[Optional[Principal]] = contextvars.ContextVar(
    "mcp_current_principal", default=None
)


def set_current_principal(principal: Optional[Principal]) -> contextvars.Token:
    return _current_principal.set(principal)


def reset_current_principal(token: contextvars.Token) -> None:
    _current_principal.reset(token)


def current_principal() -> Optional[Principal]:
    return _current_principal.get()


def current_subject() -> Optional[str]:
    """The identity to forward to Trino, or None (falls back to the service account)."""
    p = _current_principal.get()
    return p.subject if p and p.subject else None
