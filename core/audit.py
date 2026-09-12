"""Audit join: one record per tool call, keyed by the request id, never the token.

The edge (a gateway, an ingress) assigns `X-Request-Id`; when it does not, the
middleware generates one and echoes it. Every tool call then leaves a record
carrying that id, the tool, the user subject, the agent product, the token id
(`jti`) and the outcome. An operator joins gateway logs and Trino query logs
on the id. The bearer itself has no field to live in: a leaked audit log must
not become a leaked credential.

`AuditSink` is the contract. `LoggingAudit` writes one JSON line per record to
a logger, which is what a container runtime collects. `InMemoryAudit` is for
tests and for a product that wants to expose the join over HTTP.
"""
from __future__ import annotations

import json
import logging
from contextvars import ContextVar, Token
from dataclasses import asdict, dataclass
from typing import Protocol

REQUEST_ID_HEADER = "x-request-id"

_current_request_id: ContextVar[str | None] = ContextVar("mcp_current_request_id", default=None)


def current_request_id() -> str | None:
    return _current_request_id.get()


def set_current_request_id(request_id: str | None) -> Token:
    return _current_request_id.set(request_id)


def reset_current_request_id(token: Token) -> None:
    _current_request_id.reset(token)


@dataclass(frozen=True)
class AuditEvent:
    request_id: str
    tool: str
    subject: str
    agent: str
    token_id: str
    ok: bool
    error: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


class AuditSink(Protocol):
    def record(self, event: AuditEvent) -> None:  # pragma: no cover - protocol
        ...


class InMemoryAudit:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def record(self, event: AuditEvent) -> None:
        self.events.append(event)


class LoggingAudit:
    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._log = logger or logging.getLogger("mcp.audit")

    def record(self, event: AuditEvent) -> None:
        self._log.info(json.dumps(event.as_dict(), separators=(",", ":")))
