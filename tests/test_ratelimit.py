"""Rate limits per user and per agent product, in the guard, before Trino.

Two windows, both sliding, both in memory: one keyed by the user subject, one
by the agent product. A limit of zero disables that window. The refusal is
`429` with `Retry-After`, `X-Reason: rate_limited` and the request id, so a
host can back off and an operator can find it in the audit join. The clock is
injectable so the tests are deterministic.
"""
from __future__ import annotations

import anyio

from core.auth import Principal
from core.middleware import AuthIdentityMiddleware
from core.ratelimit import RateLimiter, SlidingWindow


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_window_allows_up_to_the_limit_then_refuses():
    clock = _Clock()
    w = SlidingWindow(limit=2, seconds=60, clock=clock)
    assert w.allow("alice") == (True, 0)
    assert w.allow("alice") == (True, 0)
    allowed, retry = w.allow("alice")
    assert allowed is False and retry == 60


def test_window_slides_and_frees_a_slot():
    clock = _Clock()
    w = SlidingWindow(limit=1, seconds=60, clock=clock)
    w.allow("alice")
    clock.now += 30
    assert w.allow("alice") == (False, 30)
    clock.now += 31
    assert w.allow("alice") == (True, 0)


def test_window_is_per_key():
    w = SlidingWindow(limit=1, seconds=60, clock=_Clock())
    assert w.allow("alice")[0] and w.allow("bob")[0]
    assert not w.allow("alice")[0]


def test_zero_limit_disables_the_window():
    w = SlidingWindow(limit=0, seconds=60, clock=_Clock())
    for _ in range(50):
        assert w.allow("anyone") == (True, 0)


def test_limiter_from_env_reads_both_limits():
    lim = RateLimiter.from_env({"MCP_RATE_LIMIT_USER": "10", "MCP_RATE_LIMIT_AGENT": "100",
                                "MCP_RATE_LIMIT_WINDOW_SECONDS": "30"})
    assert lim.enabled
    assert (lim.user.limit, lim.agent.limit, lim.user.seconds) == (10, 100, 30)


def test_limiter_is_disabled_when_unset():
    lim = RateLimiter.from_env({})
    assert not lim.enabled
    assert lim.check(subject="a", agent="b") == (True, 0)


def test_limiter_checks_user_then_agent_and_reports_the_longer_wait():
    clock = _Clock()
    lim = RateLimiter(user=SlidingWindow(limit=1, seconds=60, clock=clock),
                      agent=SlidingWindow(limit=2, seconds=10, clock=clock))
    assert lim.check(subject="alice", agent="cursor") == (True, 0)
    assert lim.check(subject="alice", agent="cursor") == (False, 60)  # user window is full
    assert lim.check(subject="bob", agent="cursor") == (True, 0)      # agent has one slot left
    assert lim.check(subject="carol", agent="cursor") == (False, 10)  # agent window is full


def test_limiter_skips_a_window_whose_key_is_unknown():
    """No subject (auth off) means no per-user count; the agent window still applies."""
    clock = _Clock()
    lim = RateLimiter(user=SlidingWindow(limit=1, seconds=60, clock=clock),
                      agent=SlidingWindow(limit=1, seconds=60, clock=clock))
    assert lim.check(subject=None, agent="cursor") == (True, 0)
    assert lim.check(subject=None, agent="cursor") == (False, 60)


# ---- through the guard ----

class _Auth:
    def verify(self, headers):
        return Principal(subject=headers.get("x-test-user", "alice"))


class _Down:
    def __init__(self):
        self.hits = 0

    async def __call__(self, scope, receive, send):
        self.hits += 1
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


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


def test_guard_refuses_with_429_retry_after_and_reason():
    clock = _Clock()
    lim = RateLimiter(user=SlidingWindow(limit=1, seconds=60, clock=clock),
                      agent=SlidingWindow(limit=0, seconds=60, clock=clock))
    down = _Down()
    app = AuthIdentityMiddleware(down, auth_provider=_Auth(), require_auth=True, limiter=lim)
    assert _call(app, {"Authorization": "Bearer x"})[0] == 200
    status, headers = _call(app, {"Authorization": "Bearer x", "X-Request-Id": "r-2"})
    assert status == 429
    assert headers["retry-after"] == "60"
    assert headers["x-reason"] == "rate_limited"
    assert headers["x-request-id"] == "r-2"
    assert down.hits == 1, "a refused request must not reach the tools"


def test_guard_counts_after_authentication_not_before():
    """An unauthenticated request is refused with 401 and does not consume a slot."""
    clock = _Clock()
    lim = RateLimiter(user=SlidingWindow(limit=1, seconds=60, clock=clock),
                      agent=SlidingWindow(limit=0, seconds=60, clock=clock))

    class _Reject:
        def verify(self, headers):
            return None if "authorization" not in headers else Principal(subject="alice")

    app = AuthIdentityMiddleware(_Down(), auth_provider=_Reject(), require_auth=True, limiter=lim)
    assert _call(app, {})[0] == 401
    assert _call(app, {"Authorization": "Bearer x"})[0] == 200
    assert _call(app, {"Authorization": "Bearer x"})[0] == 429
