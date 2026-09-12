"""Rate limits per user and per agent product, decided in the guard before Trino.

Two sliding windows in memory: one keyed by the user subject, one by the agent
product. A limit of zero disables its window, so a deployment opts in per
principal. The state is per process; a deployment with several replicas gets
a per-replica quota, which is the honest thing to say in the README rather
than pretend a shared store exists.

The refusal is `429` with `Retry-After`, sent by the middleware. The clock is
injectable so the windows are testable without sleeping.
"""
from __future__ import annotations

import math
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Mapping


class SlidingWindow:
    """At most ``limit`` events per key in the last ``seconds``."""

    def __init__(self, *, limit: int, seconds: int, clock: Callable[[], float] = time.monotonic):
        self.limit = limit
        self.seconds = seconds
        self._clock = clock
        self._events: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> tuple[bool, int]:
        """Return (allowed, retry_after_seconds). Counts the event when allowed."""
        allowed, retry = self.peek(key)
        if allowed:
            self.record(key)
        return allowed, retry

    def peek(self, key: str) -> tuple[bool, int]:
        """Would ``key`` be allowed now? Counts nothing."""
        if self.limit <= 0:
            return True, 0
        now = self._clock()
        with self._lock:
            events = self._prune(key, now)
            if len(events) >= self.limit:
                return False, max(1, math.ceil(events[0] + self.seconds - now))
            return True, 0

    def record(self, key: str) -> None:
        if self.limit <= 0:
            return
        now = self._clock()
        with self._lock:
            self._prune(key, now).append(now)

    def _prune(self, key: str, now: float) -> deque[float]:
        events = self._events.setdefault(key, deque())
        while events and events[0] <= now - self.seconds:
            events.popleft()
        return events


@dataclass(frozen=True)
class RateLimiter:
    user: SlidingWindow = field(default_factory=lambda: SlidingWindow(limit=0, seconds=60))
    agent: SlidingWindow = field(default_factory=lambda: SlidingWindow(limit=0, seconds=60))

    @property
    def enabled(self) -> bool:
        return self.user.limit > 0 or self.agent.limit > 0

    @staticmethod
    def from_env(env: Mapping[str, str]) -> "RateLimiter":
        seconds = int(env.get("MCP_RATE_LIMIT_WINDOW_SECONDS", "60"))
        return RateLimiter(
            user=SlidingWindow(limit=int(env.get("MCP_RATE_LIMIT_USER", "0")), seconds=seconds),
            agent=SlidingWindow(limit=int(env.get("MCP_RATE_LIMIT_AGENT", "0")), seconds=seconds),
        )

    def check(self, *, subject: str | None, agent: str | None) -> tuple[bool, int]:
        """Both windows must allow; a refused request consumes no slot in either.

        A window whose key is unknown is skipped."""
        if not self.enabled:
            return True, 0
        pairs = [(w, k) for w, k in ((self.user, subject), (self.agent, agent)) if k]
        waits = [retry for allowed, retry in (w.peek(k) for w, k in pairs) if not allowed]
        if waits:
            return False, max(waits)
        for window, key in pairs:
            window.record(key)
        return True, 0
