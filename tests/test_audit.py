"""Audit join: one line per tool call, keyed by request id, never the raw token.

The operator question is "what did this request do, and for whom?". The answer
is a record carrying the request id the edge assigned, the tool, the caller's
subject and the outcome. The bearer itself is never in it: a leaked audit log must not become
a leaked credential.
"""

from __future__ import annotations

import json
import logging

import pytest

from akko_mcp_trino.audit import (
    AuditEvent,
    CallTimeout,
    InMemoryAudit,
    LoggingAudit,
    ToolCall,
    bind_call,
    current_call,
    error_code,
    note,
    reset_call,
    sql_digest,
    time_allowed,
)


def _event(**over) -> AuditEvent:
    base = dict(
        request_id="req-1",
        tool="execute_query",
        subject="alice_admin",
        ok=True,
        error="",
    )
    base.update(over)
    return AuditEvent(**base)


def test_event_is_frozen_and_serialisable():
    e = _event()
    doc = e.as_dict()
    assert doc == {
        "request_id": "req-1",
        "tool": "execute_query",
        "subject": "alice_admin",
        "ok": True,
        "error": "",
        "duration_ms": 0,
    }


def test_event_has_no_field_that_could_hold_a_token():
    """The record's shape forbids the bearer: there is no place to put it."""
    names = set(AuditEvent.__dataclass_fields__)
    assert not {"token", "bearer", "authorization", "jwt"} & names


def test_in_memory_audit_keeps_events_in_order():
    a = InMemoryAudit()
    a.record(_event(tool="list_catalogs"))
    a.record(_event(tool="execute_query", ok=False, error="denied"))
    assert [e.tool for e in a.events] == ["list_catalogs", "execute_query"]
    assert a.events[1].ok is False and a.events[1].error == "denied"


def test_logging_audit_emits_one_json_line(caplog):
    a = LoggingAudit(logging.getLogger("audit-test"))
    with caplog.at_level(logging.INFO, logger="audit-test"):
        a.record(_event())
    lines = [r.getMessage() for r in caplog.records]
    assert len(lines) == 1
    doc = json.loads(lines[0])
    assert doc["request_id"] == "req-1" and doc["subject"] == "alice_admin"


# ---- what the call did: SQL digest, tables, rows, metadata served


def test_the_sql_is_recorded_as_its_sha256_never_as_text():
    """The SQL may carry literals (an email, a name): the record keeps its digest.

    The digest is the one Trino's query text and the policy engine's audit
    (which records the query text) hash to, so the three traces join on it."""
    assert (
        sql_digest("SELECT 1") == "e004ebd5b5532a4b85984a62f8ad48a81aa3460c1ca07701f386135d72cdecf5"
    )
    assert "sql" not in set(AuditEvent.__dataclass_fields__)
    assert "sql" not in set(ToolCall.__dataclass_fields__)
    assert "sql_sha256" in set(ToolCall.__dataclass_fields__)


def test_note_fills_the_current_call_and_is_a_no_op_outside_one():
    note(sql="SELECT 1", tables=["a.b.c"], rows=3)  # no call bound: nothing, no error
    assert current_call() is None
    call, token = bind_call("execute_query")
    try:
        assert current_call() is call and call.tool == "execute_query"
        note(sql="SELECT * FROM a.b.c", tables=["a.b.c", "a.b.c"], rows=2, truncated=True)
        note(tables=["x.y.z"], metadata_served=["a.b.c"], rows=5)
        note(metadata_served=["a.b.c", "x.y.z"])
    finally:
        reset_call(token)
    assert current_call() is None
    assert call.sql_sha256 == sql_digest("SELECT * FROM a.b.c")
    assert call.tables == ["a.b.c", "x.y.z"], "tables are kept once, in order"
    assert call.metadata_served == ["a.b.c", "x.y.z"]
    assert call.rows == 5 and call.truncated is True
    note(truncated=False)  # outside the call again: the finished record is untouched
    assert call.truncated is True


def test_a_call_that_notes_nothing_leaves_the_defaults():
    call, token = bind_call("list_catalogs")
    reset_call(token)
    assert (
        call.sql_sha256,
        call.tables,
        call.rows,
        call.truncated,
        call.metadata_served,
        call.error,
    ) == ("", [], None, False, [], "")
    assert call.time_left() is None, "no budget, no deadline"


# ---- what the audit keeps of an error: a code, never a message


class _TrinoError(Exception):
    """What the Trino client raises: its message may quote a value read from a row."""

    def __init__(self, name, error_type, message):
        super().__init__(message)
        self.error_name, self.error_type, self.message = name, error_type, message


def test_the_code_of_a_trino_error_is_its_type_and_name_never_its_message():
    exc = _TrinoError(
        "INVALID_CAST_ARGUMENT", "USER_ERROR", "Cannot cast 'FR7630006000011234567890189' to INT"
    )
    assert error_code(exc) == "USER_ERROR:INVALID_CAST_ARGUMENT"
    assert error_code(_TrinoError("GENERIC_INTERNAL_ERROR", "", "boom")) == "GENERIC_INTERNAL_ERROR"


def test_the_code_of_any_other_error_is_its_class():
    assert error_code(TimeoutError("the query exceeded 5 s")) == "TimeoutError"
    assert error_code(ValueError("Invalid catalog name: 'x;y'")) == "ValueError"


def test_a_tool_notes_the_code_of_its_failure():
    call, token = bind_call("execute_query")
    try:
        note(error="USER_ERROR:PERMISSION_DENIED")
    finally:
        reset_call(token)
    assert call.error == "USER_ERROR:PERMISSION_DENIED"


# ---- a call has a time budget: past it, no query starts, and the call knows it expired


def test_a_call_with_a_budget_has_a_deadline_and_knows_when_it_expired(monkeypatch):
    import akko_mcp_trino.audit as audit

    now = [100.0]
    monkeypatch.setattr(audit.time, "monotonic", lambda: now[0])
    call, token = bind_call("a_tool", budget_seconds=30)
    reset_call(token)
    assert call.time_left() == 30 and call.expired is False
    now[0] += 31
    assert call.time_left() == -1
    exc = call.timed_out()
    assert isinstance(exc, CallTimeout) and isinstance(exc, TimeoutError)
    assert str(exc) == "the tool call exceeded its time budget of 30 s"
    assert call.expired is True
    with pytest.raises(CallTimeout):
        raise exc


def test_a_wait_within_a_call_gets_what_is_left_of_its_budget_when_that_is_less(monkeypatch):
    """What a plugin's own request to its backend may wait: its own timeout, or less."""
    import akko_mcp_trino.audit as audit

    now = [100.0]
    monkeypatch.setattr(audit.time, "monotonic", lambda: now[0])
    assert time_allowed(300) == 300, "outside a call, the wait's own timeout"
    _, token = bind_call("ask_data")
    try:
        assert time_allowed(300) == 300, "a call without a budget sets no deadline"
    finally:
        reset_call(token)
    call, token = bind_call("ask_data", budget_seconds=120)
    try:
        assert time_allowed(300) == 120 and time_allowed(30) == 30
        assert time_allowed(0) == 120, "a wait with no limit of its own gets the call's"
        now[0] += 119.5
        assert time_allowed(300) == 0.5
        now[0] += 1
        with pytest.raises(CallTimeout):
            time_allowed(300)
    finally:
        reset_call(token)
    assert call.expired is True, "past the deadline, nothing waits and the call knows it"


def test_a_call_with_no_time_left_is_over(monkeypatch):
    """A wait bounded by what is left of the budget ends at the deadline itself: the call has
    spent its budget then, not a moment later."""
    import akko_mcp_trino.audit as audit

    now = [100.0]
    monkeypatch.setattr(audit.time, "monotonic", lambda: now[0])
    call, token = bind_call("ask_data", budget_seconds=10)
    reset_call(token)
    assert call.over() is False
    now[0] += 10
    assert call.over() is True and call.expired is False
    free, token = bind_call("ask_data")
    reset_call(token)
    now[0] += 1000
    assert free.over() is False, "no budget, never over"


def test_an_extensions_fields_follow_the_cores_on_the_line():
    """An extension adds its fields to the line (``extra``); the call and the principal
    it reads them from are never written as such."""
    a = LoggingAudit(logging.getLogger("audit-full"))
    e = _event(duration_ms=12, extra={"rows": 3}, call=ToolCall(tool="t"), principal=object())
    records = []

    class _H(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    log = logging.getLogger("audit-full")
    log.addHandler(_H())
    log.setLevel(logging.INFO)
    a.record(e)
    doc = json.loads(records[-1])
    assert doc["duration_ms"] == 12 and doc["rows"] == 3
    assert "call" not in doc and "principal" not in doc and "extra" not in doc
