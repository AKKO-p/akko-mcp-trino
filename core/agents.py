"""The agent product is a second principal, distinct from the user.

A workspace key from Cursor, Claude or a house agent is not a user and must
never become a Trino user. It names *which product* is calling, for quotas
and audit; the user JWT still says *for whom*. Both are bound on every
request when a registry is configured. An empty registry disables the check
entirely, so a deployment that has registered no products is not asked for a
key it cannot have.

The agent name travels in a ContextVar next to the Principal, and is reset
after the request. Keys are compared with a constant-time function.
"""
from __future__ import annotations

import hmac
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Mapping

AGENT_KEY_HEADER = "x-agent-key"

_current_agent: ContextVar[str | None] = ContextVar("mcp_current_agent", default=None)


def current_agent() -> str | None:
    """The registered name of the agent product behind the current request."""
    return _current_agent.get()


def set_current_agent(name: str | None) -> Token:
    return _current_agent.set(name)


def reset_current_agent(token: Token) -> None:
    _current_agent.reset(token)


@dataclass(frozen=True)
class AgentRegistry:
    """Registered agent products, keyed by their secret."""

    _by_key: Mapping[str, str] = field(default_factory=dict)

    @property
    def enabled(self) -> bool:
        return bool(self._by_key)

    @staticmethod
    def from_env(env: Mapping[str, str]) -> "AgentRegistry":
        """Parse ``MCP_AGENT_KEYS`` as ``name:key,name:key``.

        A malformed entry raises: a registry that silently drops an entry
        would either lock a product out or, worse, run without the check.
        """
        raw = env.get("MCP_AGENT_KEYS", "").strip()
        if not raw:
            return AgentRegistry()
        by_key: dict[str, str] = {}
        for entry in raw.split(","):
            entry = entry.strip()
            if not entry:
                continue
            name, sep, key = entry.partition(":")
            if not sep or not name.strip() or not key.strip():
                raise ValueError(
                    "MCP_AGENT_KEYS entries must look like name:key, got a malformed entry"
                )
            by_key[key.strip()] = name.strip()
        return AgentRegistry(by_key)

    def resolve(self, presented: str | None) -> str | None:
        """Return the agent name for a presented key, or None if unknown."""
        if not self._by_key or not presented:
            return None
        for key, name in self._by_key.items():
            if hmac.compare_digest(key.encode(), presented.encode()):
                return name
        return None
