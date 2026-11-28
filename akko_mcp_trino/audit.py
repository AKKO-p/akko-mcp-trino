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

Each line says when it was written (``ts``, UTC, ISO 8601 with milliseconds) and what it
is (``event``): ``tool_call`` for a call, ``refused`` for a request the door turned away
before any tool ran (``RefusalEvent``, written by the middleware with a stable ``reason``
code, never the token nor a claim of a token it could not verify). A call's line also says
who Trino saw (``trino_user``, from the queries the call ran), under which identity mode,
which application asked for the token (``client_id``, a verified claim), which MCP client
says it is calling (``mcp_client``, declared by the client at initialization, never
verified), from which address (``client_ip``), and the ids Trino gave its queries
(``query_ids``). An extension's field never takes the place of one of these.

`AuditSink` is the contract. `LoggingAudit` writes one JSON line per record to
a logger, which is what a container runtime collects. `InMemoryAudit` is for
tests and for a product that wants to read the lines back.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import threading
import time
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional, Protocol

REQUEST_ID_HEADER = "x-request-id"
# The names a record keeps in each of its lists (``MCP_AUDIT_MAX_NAMES``): past them, it counts
# the others, once each, so that a line is never without a bound.
DEFAULT_AUDIT_MAX_NAMES = 200

# What a line says it is.
TOOL_CALL = "tool_call"
REFUSED = "refused"
# The longest name and version of the MCP client a line keeps; the client declares them.
MCP_CLIENT_NAME_CHARS = 64
MCP_CLIENT_VERSION_CHARS = 32

_current_request_id: ContextVar[str | None] = ContextVar("mcp_current_request_id", default=None)
_current_client_ip: ContextVar[str] = ContextVar("mcp_current_client_ip", default="")
_current_mcp_client: ContextVar[str] = ContextVar("mcp_current_mcp_client", default="")


def utc_now() -> str:
    """Now, in UTC, as ISO 8601 with milliseconds and a ``Z``: the ``ts`` of a line."""
    return utc_iso(time.time())


def utc_iso(seconds: float) -> str:
    """``seconds`` since the epoch, in UTC, as ISO 8601 with milliseconds and a ``Z``."""
    moment = dt.datetime.fromtimestamp(seconds, tz=dt.timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def current_client_ip() -> str:
    """The address of the client of the current request (see ``MCP_TRUSTED_PROXIES``), or
    empty outside an HTTP request."""
    return _current_client_ip.get()


def set_current_client_ip(address: str) -> Token:
    """Bind the client's address to the current request; returns the token for reset."""
    return _current_client_ip.set(address)


def reset_current_client_ip(token: Token) -> None:
    """Clear the address bound by ``set_current_client_ip``."""
    _current_client_ip.reset(token)


def _declared(text: Any, limit: int) -> str:
    """A name the client declares, as a line may keep it: printable ASCII only (any other
    character becomes ``?``), at most ``limit`` characters; anything but text is nothing."""
    if not isinstance(text, str):
        return ""
    kept = "".join(ch if 32 <= ord(ch) < 127 else "?" for ch in text[:limit])
    return kept.strip()


def mcp_client_label(name: Any, version: Any) -> str:
    """``name/version`` of the MCP client, as it declared them at initialization, each
    bounded and kept to printable ASCII; empty when it declared no name."""
    kept = _declared(name, MCP_CLIENT_NAME_CHARS)
    if not kept:
        return ""
    release = _declared(version, MCP_CLIENT_VERSION_CHARS)
    return f"{kept}/{release}" if release else kept


def current_mcp_client() -> str:
    """The MCP client of the current request, as it declared itself, or empty."""
    return _current_mcp_client.get()


def set_current_mcp_client(label: str) -> Token:
    """Bind the declared MCP client to the current request; returns the token for reset."""
    return _current_mcp_client.set(label)


def reset_current_mcp_client(token: Token) -> None:
    """Clear the client bound by ``set_current_mcp_client``."""
    _current_mcp_client.reset(token)


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


# The fields of the core's lines: an extension's field never takes one of them.
CORE_FIELDS = frozenset(
    {
        "ts",
        "event",
        "request_id",
        "tool",
        "subject",
        "client_id",
        "mcp_client",
        "client_ip",
        "identity_mode",
        "trino_user",
        "ok",
        "error",
        "duration_ms",
        "query_ids",
        "query_ids_cut",
        "reason",
        "status",
        "extra_dropped",
    }
)


def merged(line: dict, extra: Mapping[str, Any], reserved: frozenset[str]) -> dict:
    """``line`` with the fields of ``extra`` after it, except those ``reserved``: dropped,
    and counted in ``extra_dropped``."""
    kept = {k: v for k, v in extra.items() if k not in reserved}
    if len(kept) < len(extra):
        kept["extra_dropped"] = len(extra) - len(kept)
    return {**line, **kept}


@dataclass(frozen=True)
class AuditEvent:
    """One tool call: who called which tool, with which outcome, for how long.

    ``subject`` is the verified caller (empty without a verified token), ``client_id``
    the application its token was issued to (a verified claim), ``mcp_client`` what the
    MCP client declared at initialization (never verified), ``client_ip`` the address the
    request came from, ``identity_mode`` how the server reaches Trino and ``trino_user`` the
    user Trino saw for the queries of the call (empty when it ran none); ``query_ids`` are
    the ids Trino gave them, in order, and ``query_ids_cut`` how many more there were.

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
    ts: str = field(default_factory=utc_now)
    client_id: str = ""
    mcp_client: str = ""
    client_ip: str = ""
    identity_mode: str = ""
    trino_user: str = ""
    query_ids: tuple[str, ...] = ()
    query_ids_cut: int = 0

    def core_dict(self) -> dict:
        """The core's fields of the line, in order; ``query_ids_cut`` only when ids were cut."""
        line: dict[str, Any] = {
            "ts": self.ts,
            "event": TOOL_CALL,
            "request_id": self.request_id,
            "tool": self.tool,
            "subject": self.subject,
            "client_id": self.client_id,
            "mcp_client": self.mcp_client,
            "client_ip": self.client_ip,
            "identity_mode": self.identity_mode,
            "trino_user": self.trino_user,
            "ok": self.ok,
            "error": self.error,
            "duration_ms": self.duration_ms,
            "query_ids": list(self.query_ids),
        }
        if self.query_ids_cut:
            line["query_ids_cut"] = self.query_ids_cut
        return line

    def as_dict(self) -> dict:
        """The line as a plain dict, ready for JSON: the core's fields, then the extra ones,
        none of which may take a core field (dropped, counted in ``extra_dropped``)."""
        return merged(self.core_dict(), self.extra, CORE_FIELDS)


@dataclass(frozen=True)
class RefusalEvent:
    """One request refused before any tool ran: why (``reason``, a stable code), with which
    HTTP status, for which request and from which address. ``subject`` and ``client_id``
    are those of a verified token, empty otherwise: a token the server could not verify
    names nobody, and none of its claims is written."""

    request_id: str
    reason: str
    status: int
    client_ip: str = ""
    subject: str = ""
    client_id: str = ""
    ts: str = field(default_factory=utc_now)

    def as_dict(self) -> dict:
        """The line as a plain dict, ready for JSON."""
        return {
            "ts": self.ts,
            "event": REFUSED,
            "request_id": self.request_id,
            "reason": self.reason,
            "status": self.status,
            "client_ip": self.client_ip,
            "subject": self.subject,
            "client_id": self.client_id,
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
    # The names each list keeps at most, and how many more were noted, once each.
    max_names: int = DEFAULT_AUDIT_MAX_NAMES
    tables_cut: int = 0
    metadata_served_cut: int = 0
    # The ids Trino gave the queries the call ran, in order, and the user it saw for them.
    query_ids: list[str] = field(default_factory=list)
    query_ids_cut: int = 0
    trino_user: str = ""
    # Why an answer was cut, when it was: "rows" (the row cap) or "bytes" (the answer's size).
    truncated_reason: str = ""
    # Every name noted, by list: kept or counted once each, at a constant cost.
    _seen: dict[str, set[str]] = field(default_factory=dict, init=False, repr=False, compare=False)
    # The reads of search_columns note their queries from several threads at once.
    _lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False, compare=False
    )

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


def bind_call(
    tool: str, budget_seconds: float = 0.0, max_names: int = DEFAULT_AUDIT_MAX_NAMES
) -> tuple[ToolCall, Token]:
    """Start the record of one call of ``tool``, due within ``budget_seconds`` when above 0,
    whose lists keep ``max_names`` names each; returns it and the token for ``reset_call``."""
    call = ToolCall(tool=tool, budget_seconds=max(0.0, budget_seconds), max_names=max_names)
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


def _add(call: ToolCall, key: str, names: Iterable[str]) -> None:
    """Add ``names`` to the list ``key`` of ``call``, each once and in order, up to its
    ``max_names``; count the others in ``<key>_cut``, once each too. Safe from several
    threads at once."""
    with call._lock:
        seen = call._seen.setdefault(key, set())
        kept: list[str] = getattr(call, key)
        for name in names:
            if name in seen:
                continue
            seen.add(name)
            if len(kept) < call.max_names:
                kept.append(name)
            else:
                setattr(call, f"{key}_cut", getattr(call, f"{key}_cut") + 1)


def note(
    *,
    sql: Optional[str] = None,
    tables: Iterable[str] = (),
    rows: Optional[int] = None,
    truncated: Optional[bool] = None,
    truncated_reason: Optional[str] = None,
    metadata_served: Iterable[str] = (),
    error: Optional[str] = None,
    query_id: Optional[str] = None,
    trino_user: Optional[str] = None,
) -> None:
    """Add to the current call's record what the tool did; nothing outside a call.

    ``sql`` is the statement run for the caller, exactly as sent (only its
    SHA-256 is kept); ``tables`` the tables it names or the call is about (each list keeps
    the call's ``max_names`` names, and counts the others in ``tables_cut`` and
    ``metadata_served_cut``);
    ``rows`` the rows or items returned; ``truncated`` whether they were cut and
    ``truncated_reason`` why (``rows`` or ``bytes``; no data, no statement);
    ``metadata_served`` the tables an extension served descriptions of;
    ``error`` the code of the failure the tool returns, never its message;
    ``query_id`` the id Trino gave a query of the call (the Trino client notes it, failed
    queries included; the list keeps ``max_names`` ids and counts the others in
    ``query_ids_cut``), ``trino_user`` the user Trino saw for it.
    """
    call = _current_call.get()
    if call is None:
        return
    if sql is not None:
        call.sql_sha256 = sql_digest(sql)
    _add(call, "tables", tables)
    if rows is not None:
        call.rows = rows
    if truncated is not None:
        call.truncated = truncated
    if truncated_reason is not None:
        call.truncated_reason = truncated_reason
    _add(call, "metadata_served", metadata_served)
    if error is not None:
        call.error = error
    if query_id:
        _add(call, "query_ids", (query_id,))
    if trino_user:
        call.trino_user = trino_user


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
    """Writes one JSON line per event to a logger (``mcp.audit`` by default).

    The line is the message; the same fields go with the log record as ``audit``, so that
    the JSON log format (``MCP_LOG_FORMAT=json``, see `akko_mcp_trino.logs`) writes them as
    the fields of its own line rather than as a string inside it."""

    def __init__(self, logger: logging.Logger | None = None) -> None:
        """Use ``logger``, or the ``mcp.audit`` logger."""
        self._log = logger or logging.getLogger("mcp.audit")

    def record(self, event: Any) -> None:
        """Log the event as one compact JSON line."""
        line = event.as_dict()
        self._log.info(json.dumps(line, separators=(",", ":")), extra={"audit": line})
