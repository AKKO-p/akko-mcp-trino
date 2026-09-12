"""Audit join: one line per tool call, keyed by request id, never the raw token.

The operator question is "what did this request do, for whom, from which
product?". The answer is a record carrying the request id the edge assigned,
the tool, the user subject, the agent product, the token id (`jti`) and the
outcome. The bearer itself is never in it: a leaked audit log must not become
a leaked credential.
"""
from __future__ import annotations

import json
import logging

from core.audit import AuditEvent, InMemoryAudit, LoggingAudit


def _event(**over) -> AuditEvent:
    base = dict(request_id="req-1", tool="execute_query", subject="alice_admin",
                agent="cursor", token_id="jti-9", ok=True, error="")
    base.update(over)
    return AuditEvent(**base)


def test_event_is_frozen_and_serialisable():
    e = _event()
    doc = e.as_dict()
    assert doc == {"request_id": "req-1", "tool": "execute_query", "subject": "alice_admin",
                   "agent": "cursor", "token_id": "jti-9", "ok": True, "error": ""}


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
    assert doc["request_id"] == "req-1" and doc["agent"] == "cursor" and doc["token_id"] == "jti-9"
