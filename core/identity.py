"""Identité de la requête courante — propagée vers Trino (X-Trino-User).

Pattern standard ASGI : un middleware (par requête) résout le Principal vérifié et le
pose dans un ContextVar ; les outils lisent `current_subject()` pour exécuter la requête
SOUS l'identité de l'utilisateur (et non le compte de service). Sans identité résolue
(auth désactivée / pas de token), `current_subject()` = None → repli compte de service.
Vendor-neutre, pur, 100% testable.
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
    """Identité à propager à Trino, ou None (→ repli compte de service)."""
    p = _current_principal.get()
    return p.subject if p and p.subject else None
