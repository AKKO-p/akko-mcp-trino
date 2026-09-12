"""Dual identity: the agent product is not the user, and both are known.

The CDP Agent Gateway blueprint names two principals on every request: the end
user (the JWT subject, who Ranger or OPA authorises) and the agent platform
(`X-Agent-Key`: Cursor, Claude, a house agent — never a user). The key exists
for quotas and audit, not for data access. This module adopts that shape.

Empty configuration disables the check, exactly as in the blueprint: a
deployment that has not registered any agent product is not asked for a key.
"""
from __future__ import annotations

import anyio

from core.agents import AgentRegistry, current_agent
from core.auth import Principal
from core.middleware import AuthIdentityMiddleware


class _Downstream:
    def __init__(self):
        self.seen_agent = "unset"

    async def __call__(self, scope, receive, send):
        self.seen_agent = current_agent()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


class _Auth:
    def verify(self, headers):
        return Principal(subject="alice_admin")


async def _call_async(app, headers=None):
    sent = []

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(m):
        sent.append(m)

    scope = {"type": "http", "method": "POST", "path": "/mcp",
             "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]}
    await app(scope, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    return start["status"], dict((k.decode(), v.decode()) for k, v in start.get("headers", []))


def _call(app, headers=None):
    return anyio.run(_call_async, app, headers)


def test_registry_parses_name_key_pairs_from_env():
    reg = AgentRegistry.from_env({"MCP_AGENT_KEYS": "cursor:k-cursor-1,claude:k-claude-2"})
    assert reg.enabled
    assert reg.resolve("k-cursor-1") == "cursor"
    assert reg.resolve("k-claude-2") == "claude"
    assert reg.resolve("nope") is None


def test_registry_is_disabled_when_unset():
    """No registered agent products means no key is asked for — blueprint behaviour."""
    reg = AgentRegistry.from_env({})
    assert not reg.enabled
    assert reg.resolve("anything") is None


def test_registry_tolerates_a_trailing_comma():
    """`cursor:k1,` is how a shell loop or a Helm join renders one entry."""
    reg = AgentRegistry.from_env({"MCP_AGENT_KEYS": "cursor:k1, "})
    assert reg.resolve("k1") == "cursor"


def test_registry_rejects_malformed_entries_loudly():
    import pytest

    with pytest.raises(ValueError):
        AgentRegistry.from_env({"MCP_AGENT_KEYS": "cursor-without-colon"})


def test_known_agent_key_is_resolved_and_visible_downstream():
    down = _Downstream()
    reg = AgentRegistry.from_env({"MCP_AGENT_KEYS": "cursor:k1"})
    app = AuthIdentityMiddleware(down, auth_provider=_Auth(), require_auth=True, agents=reg)
    status, _ = _call(app, {"Authorization": "Bearer x", "X-Agent-Key": "k1"})
    assert status == 200
    assert down.seen_agent == "cursor"


def test_missing_agent_key_is_refused_when_agents_are_registered():
    down = _Downstream()
    reg = AgentRegistry.from_env({"MCP_AGENT_KEYS": "cursor:k1"})
    app = AuthIdentityMiddleware(down, auth_provider=_Auth(), require_auth=True, agents=reg)
    status, headers = _call(app, {"Authorization": "Bearer x"})
    assert status == 401
    assert headers.get("x-reason") == "agent_key_missing"
    assert down.seen_agent == "unset", "the request reached the tools without an agent identity"


def test_unknown_agent_key_is_refused():
    down = _Downstream()
    reg = AgentRegistry.from_env({"MCP_AGENT_KEYS": "cursor:k1"})
    app = AuthIdentityMiddleware(down, auth_provider=_Auth(), require_auth=True, agents=reg)
    status, headers = _call(app, {"Authorization": "Bearer x", "X-Agent-Key": "forged"})
    assert status == 401
    assert headers.get("x-reason") == "agent_key_unknown"


def test_no_registry_means_no_key_required_and_agent_is_none():
    down = _Downstream()
    app = AuthIdentityMiddleware(down, auth_provider=_Auth(), require_auth=True, agents=None)
    status, _ = _call(app, {"Authorization": "Bearer x"})
    assert status == 200
    assert down.seen_agent is None


def test_agent_identity_is_cleared_after_the_request():
    down = _Downstream()
    reg = AgentRegistry.from_env({"MCP_AGENT_KEYS": "cursor:k1"})
    app = AuthIdentityMiddleware(down, auth_provider=_Auth(), require_auth=True, agents=reg)
    _call(app, {"Authorization": "Bearer x", "X-Agent-Key": "k1"})
    assert current_agent() is None
