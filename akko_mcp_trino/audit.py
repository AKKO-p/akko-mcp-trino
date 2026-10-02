"""Audit: one line per tool call, keyed by the request id, never the token.

The edge (a gateway, an ingress) assigns `X-Request-Id`; when it does not, the
middleware generates one and echoes it. Every tool call then leaves one line:
the request id, the tool, the user subject, the outcome and the duration. The
bearer has no field to live in, and neither has the SQL text: a leaked audit
log must not become a leaked credential or a leaked query. ``error`` is a code,
never a message: the agent reads the whole message, but a message from Trino
may quote a value read from a row (``Cannot cast '<value>' to INT``). A Trino
error is kept as its type and name (``USER_ERROR:INVALID_CAST_ARGUMENT``), any
other exception as its class, and a tool names its own refusals with
``note(error=...)``.

While it runs, a call keeps a record of what it did (``ToolCall``): the SHA-256
of the SQL it ran for the caller, the tables that SQL names, the rows returned
and whether they were cut, and the tables an extension served descriptions of.
A tool, a plugin's included, says so with ``note(...)``. The core's line does
not write that record; an extension may (``Extension.wrap_audit``), with the
call and the principal the event carries.

A call may have a time budget (``bind_call(tool, budget_seconds)``): past its
deadline, the Trino client starts no query for it and cancels the one running,
and the tool layer answers with an error rather than with what the call had
gathered, or with an answer that came too late. A plugin that waits on its own
backend bounds that wait with ``time_allowed(timeout)``: its own timeout, or
what is left of the call's budget when that is shorter.

`AuditSink` is the contract. `LoggingAudit` writes one JSON line per record to
a logger, which is what a container runtime collects. `InMemoryAudit` is for
tests and for a product that wants to read the lines back.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional, Protocol

REQUEST_ID_HEADER = "x-request-id"

_current_request_id: ContextVar[str | None] = ContextVar("mcp_current_request_id", default=None)


def current_request_id() -> str | None:
    """The request id of the current request, or None outside a request."""
    return _current_request_id.get()


def set_current_request_id(request_id: str | None) -> Token:
    """Bind a request id to the current request; returns the token for reset."""
    return _current_request_id.set(request_id)


def reset_current_request_id(token: Token) -> None:
    """Clear the request id bound by ``set_current_request_id``."""
    _current_request_id.reset(token)


def sql_digest(sql: str) -> str:
    """SHA-256 of the statement text as sent to Trino, in hexadecimal."""
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()


def error_code(exc: BaseException) -> str:
    """What the audit keeps of an error: never its message, which may quote a value read.

    Trino's error type and name when the exception carries them
    (``USER_ERROR:INVALID_CAST_ARGUMENT``), else the exception's class.
    """
    name = getattr(exc, "error_name", None)
    if isinstance(name, str) and name:
        kind = getattr(exc, "error_type", None)
        return f"{kind}:{name}" if isinstance(kind, str) and kind else name
    return type(exc).__name__


class CallTimeout(TimeoutError):
    """A query refused or cancelled because its tool call ran out of its time budget."""


@dataclass(frozen=True)
class AuditEvent:
    """One tool call: who called which tool, with which outcome, for how long.

    ``extra`` holds the fields an extension adds to the line. ``call`` (what the
    call did) and ``principal`` (who it was) are there for an extension to read;
    neither is written as such."""

    request_id: str
    tool: str
    subject: str
    ok: bool
    error: str = ""
    duration_ms: int = 0
    extra: Mapping[str, Any] = field(default_factory=dict)
    call: Optional["ToolCall"] = field(default=None, compare=False, repr=False)
    principal: Any = field(default=None, compare=False, repr=False)

    def as_dict(self) -> dict:
        """The line as a plain dict, ready for JSON: the core's fields, then the extra ones."""
        return {
            "request_id": self.request_id,
            "tool": self.tool,
            "subject": self.subject,
            "ok": self.ok,
            "error": self.error,
            "duration_ms": self.duration_ms,
            **self.extra,
        }


@dataclass
class ToolCall:
    """What one tool call did, filled by ``note`` while it runs, and until when it may run."""

    tool: str
    sql_sha256: str = ""
    tables: list[str] = field(default_factory=list)
    rows: Optional[int] = None
    truncated: bool = False
    metadata_served: list[str] = field(default_factory=list)
    error: str = ""  # the code of the failure the tool names, never a message
    budget_seconds: float = 0.0  # 0: no deadline
    deadline: float = 0.0  # on the time.monotonic() clock, when there is a budget
    expired: bool = False

    def time_left(self) -> Optional[float]:
        """Seconds left before the deadline (negative once past it); None without a budget."""
        return self.deadline - time.monotonic() if self.budget_seconds > 0 else None

    def timed_out(self) -> CallTimeout:
        """Mark the call expired and return the error that says so."""
        self.expired = True
        return CallTimeout(f"the tool call exceeded its time budget of {self.budget_seconds:g} s")

    def over(self) -> bool:
        """Whether the call ran out of its budget: stopped for it, or no time left."""
        left = self.time_left()
        return self.expired or (left is not None and left <= 0)


_current_call: ContextVar[Optional[ToolCall]] = ContextVar("mcp_current_call", default=None)


def current_call() -> Optional[ToolCall]:
    """The tool call running in this context, or None outside a tool."""
    return _current_call.get()


def bind_call(tool: str, budget_seconds: float = 0.0) -> tuple[ToolCall, Token]:
    """Start the record of one call of ``tool``, due within ``budget_seconds`` when above 0;
    returns it and the token for ``reset_call``."""
    call = ToolCall(tool=tool, budget_seconds=max(0.0, budget_seconds))
    if call.budget_seconds:
        call.deadline = time.monotonic() + call.budget_seconds
    return call, _current_call.set(call)


def reset_call(token: Token) -> None:
    """End the call bound by ``bind_call``."""
    _current_call.reset(token)


def time_allowed(timeout: float) -> float:
    """Seconds a wait of the current call may last: ``timeout`` (0: no limit of its own), or
    what is left of the call's budget when that is shorter.

    Outside a call, or in a call without a budget, ``timeout`` itself. Past the call's
    deadline nothing may wait: raises the call's ``CallTimeout``, and the call knows it
    expired. The Trino client bounds each query with it; a plugin bounds its requests to
    its own backend the same way.
    """
    call = _current_call.get()
    left = call.time_left() if call is not None else None
    if call is None or left is None:
        return timeout
    if left <= 0:
        raise call.timed_out()
    return min(timeout, left) if timeout > 0 else left


def _add(into: list[str], names: Iterable[str]) -> None:
    for name in names:
        if name not in into:
            into.append(name)


def note(
    *,
    sql: Optional[str] = None,
    tables: Iterable[str] = (),
    rows: Optional[int] = None,
    truncated: Optional[bool] = None,
    metadata_served: Iterable[str] = (),
    error: Optional[str] = None,
) -> None:
    """Add to the current call's record what the tool did; nothing outside a call.

    ``sql`` is the statement run for the caller, exactly as sent (only its
    SHA-256 is kept); ``tables`` the tables it names or the call is about;
    ``rows`` the rows or items returned; ``truncated`` whether they were cut;
    ``metadata_served`` the tables an extension served descriptions of;
    ``error`` the code of the failure the tool returns, never its message.
    """
    call = _current_call.get()
    if call is None:
        return
    if sql is not None:
        call.sql_sha256 = sql_digest(sql)
    _add(call.tables, tables)
    if rows is not None:
        call.rows = rows
    if truncated is not None:
        call.truncated = truncated
    _add(call.metadata_served, metadata_served)
    if error is not None:
        call.error = error


class AuditSink(Protocol):
    """The contract an audit destination fulfils: ``record`` one event."""

    def record(self, event: AuditEvent) -> None:  # pragma: no cover - protocol
        """Record one event."""
        ...


class InMemoryAudit:
    """Keeps events in a list; for tests and for products that expose the join."""

    def __init__(self) -> None:
        """Start with no events."""
        self.events: list[AuditEvent] = []

    def record(self, event: AuditEvent) -> None:
        """Append the event."""
        self.events.append(event)


class LoggingAudit:
    """Writes one JSON line per event to a logger (``mcp.audit`` by default)."""

    def __init__(self, logger: logging.Logger | None = None) -> None:
        """Use ``logger``, or the ``mcp.audit`` logger."""
        self._log = logger or logging.getLogger("mcp.audit")

    def record(self, event: AuditEvent) -> None:
        """Log the event as one compact JSON line."""
        self._log.info(json.dumps(event.as_dict(), separators=(",", ":")))
