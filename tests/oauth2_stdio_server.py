"""A stdio server in ``oauth2`` mode against a fake Trino, run as a subprocess by
``test_integration_transports``.

The real SDK serves it, so what a tool sees is what the SDK's middleware chain gives
it, task by task. Trino is faked at the driver (``trino.dbapi.connect``): every query
still goes through the core's identity mode. A guard extension binds a value of its
own to each admission. Each connection, guard call and audit record is one JSON line in
the file ``OAUTH2_EVENTS`` names; nothing is written to stdout, the protocol channel.
``OAUTH2_REFUSE=1`` makes the guard refuse.
"""

from __future__ import annotations

import contextvars
import json
import os

import trino.dbapi

from akko_mcp_trino.app import run_stdio
from akko_mcp_trino.config import Config
from akko_mcp_trino.extensions import Extension, Refusal
from akko_mcp_trino.identity import current_subject
from akko_mcp_trino.server import build_server

BOUND: contextvars.ContextVar[str] = contextvars.ContextVar("bound", default="unbound")


def _event(**fields):
    with open(os.environ["OAUTH2_EVENTS"], "a", encoding="utf-8") as out:
        out.write(json.dumps(fields) + "\n")


class _Cursor:
    def __init__(self, user):
        self._user, self.description, self.query_id = user, [("c",)], "q"

    def execute(self, sql, params=None):
        _event(trino=sql, user=self._user, subject=current_subject(), bound=BOUND.get())
        self._rows = [("alice",)] if sql == "SELECT current_user" else [(1,)]

    def fetchmany(self, n):
        return self._rows[:n]

    def cancel(self):
        pass


class _Connection:
    def __init__(self, user):
        self._user = user

    def cursor(self):
        return _Cursor(self._user)


trino.dbapi.connect = lambda **kw: _Connection(kw["user"])


class _Guard(Extension):
    name = "test-guard"
    calls = 0

    def after_auth(self, admission):
        _Guard.calls += 1
        subject = admission.principal.subject if admission.principal else None
        _event(guard=_Guard.calls, subject=subject, stdio=admission.stdio)
        if os.environ.get("OAUTH2_REFUSE") == "1":
            return Refusal("test_refused", message="ask an administrator")
        admission.bind(BOUND, f"call-{_Guard.calls}")
        return None

    def tool_registrars(self):
        return [_register]


def _seen():
    return json.dumps({"subject": current_subject(), "bound": BOUND.get()})


def _register(mcp, client):
    @mcp.tool()
    def seen_sync() -> str:
        """What a synchronous tool, run in a worker thread, sees of its call."""
        return _seen()

    @mcp.tool()
    async def seen_async() -> str:
        """What an asynchronous tool sees of its call."""
        return _seen()

    @mcp.resource("test://seen")
    def seen_resource() -> str:
        """What a resource an extension adds sees of its read."""
        return _seen()


class _Audit:
    def record(self, event):
        _event(audit=event.tool, subject=event.subject, ok=event.ok)


config = Config.from_env()
extensions = [_Guard()]
mcp, client = build_server(config, audit=_Audit(), extensions=extensions)
run_stdio(config, mcp, auth_provider=None, extensions=extensions, client=client)
