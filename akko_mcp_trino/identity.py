"""The verified caller of the current request.

Standard ASGI pattern: a per-request middleware resolves the verified Principal
and stores it in a ContextVar. The audit line names it, and the tools hand its
subject to the identity mode with every query. The core's mode, ``service``,
runs every query as the configured identity whoever the caller is; an extension
may read the caller here. With no verified caller (auth disabled, no token),
``current_subject()`` is None. Pure, and fully testable.
"""

from __future__ import annotations

import contextvars
from typing import Optional

from .auth import Principal

_current_principal: contextvars.ContextVar[Optional[Principal]] = contextvars.ContextVar(
    "mcp_current_principal", default=None
)


def set_current_principal(principal: Optional[Principal]) -> contextvars.Token:
    """Bind the Principal to the current request; returns the token for reset."""
    return _current_principal.set(principal)


def reset_current_principal(token: contextvars.Token) -> None:
    """Clear the Principal bound by ``set_current_principal``."""
    _current_principal.reset(token)


def current_principal() -> Optional[Principal]:
    """The Principal of the current request, or None."""
    return _current_principal.get()


def current_subject() -> Optional[str]:
    """The verified caller's subject, or None when no token was verified."""
    p = _current_principal.get()
    return p.subject if p and p.subject else None
