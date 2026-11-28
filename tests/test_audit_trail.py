"""Audit and log lines for production (0.8.7): when, what, who, from where, and every refusal.

Each guarantee has a test here that fails when it is taken away:

* every refusal before the tools leaves an audit line and counts, with a stable reason;
* no line holds the token, nor a claim of a token the server could not verify;
* every audit line says when it was written (``ts``), in both log formats;
* ``MCP_LOG_FORMAT=json`` writes pure JSON lines, nothing before them;
* ``/ready`` answers a code, never the exception (see test_server_health too);
* the address of the client is read behind trusted proxies only;
* a call's line says who Trino saw, under which mode, and which queries it ran.
"""

from __future__ import annotations

import io
import json
import logging
import re
import subprocess
import sys
from dataclasses import replace
from types import SimpleNamespace

import anyio
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from akko_mcp_trino import audit as audit_module
from akko_mcp_trino import logs, otel
from akko_mcp_trino.audit import (
    CORE_FIELDS,
    AuditEvent,
    InMemoryAudit,
    LoggingAudit,
    RefusalEvent,
    bind_call,
    mcp_client_label,
    note,
    reset_call,
    set_current_client_ip,
    set_current_mcp_client,
    utc_iso,
)
from akko_mcp_trino.auth import Principal, TokenVerdict
from akko_mcp_trino.extensions import Extension, Refusal
from akko_mcp_trino.metrics import Metrics
from akko_mcp_trino.middleware import AuthIdentityMiddleware, client_address, refusal_reason

TS = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
# A made-up token, built from its parts so that a secret scanner does not take it for one.
TOKEN = ".".join(("eyJhbGciOiJSUzI1NiJ9", "eyJzdWIiOiJtYWxsb3J5In0", "Zm9yZ2Vk"))


# ---- time and format


def test_a_timestamp_is_utc_iso_8601_with_milliseconds():
    assert utc_iso(0) == "1970-01-01T00:00:00.000Z"
    assert utc_iso(1764321302.4819) == "2025-11-28T09:15:02.481Z"
    assert TS.match(audit_module.utc_now())


def test_every_audit_line_says_when_it_was_written_in_the_text_format_too(caplog):
    with caplog.at_level(logging.INFO, logger="mcp.audit"):
        LoggingAudit().record(AuditEvent(request_id="r", tool="t", subject="", ok=True))
        LoggingAudit().record(RefusalEvent(request_id="r", reason="missing_token", status=401))
    for record in caplog.records:
        assert TS.match(json.loads(record.getMessage())["ts"])


def _format(record: logging.LogRecord, fmt: str) -> str:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(
        logs.JsonFormatter() if fmt == "json" else logging.Formatter(logs.TEXT_FORMAT)
    )
    handler.emit(record)
    return stream.getvalue().rstrip("\n")


def _record(name: str, msg: str, **extra) -> logging.LogRecord:
    record = logging.LogRecord(name, logging.INFO, __file__, 1, msg, None, None)
    record.__dict__.update(extra)
    return record


def test_json_lines_are_pure_json_with_ts_level_and_logger():
    line = _format(_record("akko_mcp_trino.__main__", "serving transport=stdio"), "json")
    doc = json.loads(line)  # the whole line, nothing before it
    assert list(doc) == ["ts", "level", "logger", "msg"]
    assert doc["level"] == "INFO" and doc["logger"] == "akko_mcp_trino.__main__"
    assert doc["msg"] == "serving transport=stdio" and TS.match(doc["ts"])


def test_an_audit_line_in_json_puts_its_fields_at_the_top_level_and_keeps_its_ts():
    event = AuditEvent(request_id="r", tool="t", subject="", ok=True, ts="2026-11-28T09:00:00.000Z")
    stream = io.StringIO()
    logger = logging.getLogger("audit-json-test")
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logs.JsonFormatter())
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    try:
        LoggingAudit(logger).record(event)
    finally:
        logger.removeHandler(handler)
    doc = json.loads(stream.getvalue())
    assert list(doc)[:4] == ["ts", "level", "logger", "event"]
    assert doc["ts"] == "2026-11-28T09:00:00.000Z" and doc["event"] == "tool_call"
    assert "msg" not in doc, "the audit fields are the line, not a string inside it"


def test_a_technical_line_in_json_carries_its_exception():
    try:
        raise ValueError("boom")
    except ValueError:
        record = logging.LogRecord("x", logging.ERROR, __file__, 1, "failed", None, sys.exc_info())
    doc = json.loads(_format(record, "json"))
    assert doc["level"] == "ERROR" and "ValueError: boom" in doc["exc"]
    assert "\n" not in _format(record, "json"), "one line, whatever the traceback"


def test_the_text_format_is_the_one_of_earlier_versions():
    assert _format(_record("mcp.audit", '{"a":1}'), "text") == 'INFO:mcp.audit:{"a":1}'


@pytest.mark.parametrize("fmt", ["text", "json"])
def test_configure_logging_replaces_the_root_handlers(fmt):
    root = logging.getLogger()
    saved, level = list(root.handlers), root.level
    try:
        logs.configure_logging(fmt, logging.WARNING)
        assert len(root.handlers) == 1 and root.level == logging.WARNING
        formatter = root.handlers[0].formatter
        assert isinstance(formatter, logs.JsonFormatter) == (fmt == "json")
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in saved:
            root.addHandler(handler)
        root.setLevel(level)


# ---- the lines themselves


def test_the_mcp_client_is_kept_bounded_and_printable():
    assert mcp_client_label("claude-code", "2.1.0") == "claude-code/2.1.0"
    assert mcp_client_label("vibe", None) == "vibe"
    assert mcp_client_label(None, "1") == "" and mcp_client_label(42, "1") == ""
    assert mcp_client_label("a\nb\u00e9", "1\x00") == "a?b?/1?"
    long = mcp_client_label("n" * 500, "v" * 500)
    assert long == "n" * 64 + "/" + "v" * 32


def test_a_refusal_line_names_the_reason_status_request_and_address():
    line = RefusalEvent(
        request_id="r",
        reason="client_not_allowed",
        status=401,
        client_ip="10.1.2.3",
        subject="alice",
        client_id="agent",
        ts="2026-11-28T09:00:00.000Z",
    ).as_dict()
    assert line == {
        "ts": "2026-11-28T09:00:00.000Z",
        "event": "refused",
        "request_id": "r",
        "reason": "client_not_allowed",
        "status": 401,
        "client_ip": "10.1.2.3",
        "subject": "alice",
        "client_id": "agent",
    }


def test_an_extensions_field_never_takes_a_field_of_the_core():
    line = AuditEvent(
        request_id="r",
        tool="t",
        subject="alice",
        ok=True,
        extra={"subject": "mallory", "ts": "1970", "event": "x", "site": "a"},
    ).as_dict()
    assert line["subject"] == "alice" and line["event"] == "tool_call" and line["ts"] != "1970"
    assert line["site"] == "a" and line["extra_dropped"] == 3


def test_the_record_keeps_the_query_ids_up_to_its_bound_and_counts_the_others():
    call, token = bind_call("execute_query", max_names=2)
    try:
        for qid in ("q1", "q2", "q2", "q3", "q4"):
            note(query_id=qid, trino_user="svc")
        note(query_id=None, trino_user=None)
        note(query_id="")
    finally:
        reset_call(token)
    assert call.query_ids == ["q1", "q2"] and call.query_ids_cut == 2
    assert call.trino_user == "svc"
    line = AuditEvent(
        request_id="r",
        tool="t",
        subject="",
        ok=True,
        query_ids=tuple(call.query_ids),
        query_ids_cut=call.query_ids_cut,
    ).as_dict()
    assert line["query_ids"] == ["q1", "q2"] and line["query_ids_cut"] == 2
    assert list(line).index("query_ids_cut") == list(line).index("query_ids") + 1
    assert "query_ids_cut" not in AuditEvent(request_id="r", tool="t", subject="", ok=1).as_dict()


def test_query_ids_noted_from_several_threads_stay_within_the_bound():
    import threading

    call, token = bind_call("search_columns", max_names=50)
    try:
        import contextvars

        threads = [
            threading.Thread(
                target=contextvars.copy_context().run,
                args=(lambda n=n: [note(query_id=f"q{n}-{i}") for i in range(100)],),
            )
            for n in range(8)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        reset_call(token)
    assert len(call.query_ids) == 50 and call.query_ids_cut == 750


def test_the_core_fields_name_every_field_a_core_line_writes():
    call = AuditEvent(request_id="r", tool="t", subject="", ok=True, query_ids_cut=1).as_dict()
    refusal = RefusalEvent(request_id="r", reason="x", status=401).as_dict()
    assert set(call) | set(refusal) <= CORE_FIELDS


# ---- the client's address


def _scope(peer, *forwarded):
    headers = [(b"x-forwarded-for", value.encode()) for value in forwarded]
    return {"client": (peer, 1234) if peer is not None else None, "headers": headers}


def _nets(*cidrs):
    from akko_mcp_trino.config import trusted_networks

    return trusted_networks(",".join(cidrs))


def test_without_trusted_proxies_the_address_is_the_peers_whatever_the_header_says():
    assert client_address(_scope("203.0.113.9", "198.51.100.1")) == "203.0.113.9"
    assert client_address(_scope(None)) == ""


def test_behind_a_trusted_proxy_the_address_is_the_first_untrusted_hop_from_the_right():
    nets = _nets("10.0.0.0/8")
    # The client wrote a hop of its own; the proxy appended the address it saw.
    assert client_address(_scope("10.0.0.5", "1.2.3.4, 198.51.100.7"), nets) == "198.51.100.7"
    # Two trusted proxies in a row, on two header lines.
    assert client_address(_scope("10.0.0.5", "198.51.100.7", "10.0.0.6"), nets) == "198.51.100.7"
    # Every hop trusted: the leftmost.
    assert client_address(_scope("10.0.0.5", "10.0.0.7, 10.0.0.6"), nets) == "10.0.0.7"
    # No header: the proxy itself.
    assert client_address(_scope("10.0.0.5"), nets) == "10.0.0.5"


def test_a_peer_that_is_not_trusted_cannot_choose_its_address():
    nets = _nets("10.0.0.0/8")
    assert client_address(_scope("203.0.113.9", "10.0.0.1"), nets) == "203.0.113.9"
    assert client_address(_scope("testclient", "1.2.3.4"), nets) == "testclient"


def test_a_malformed_hop_stops_at_the_last_trusted_one():
    nets = _nets("10.0.0.0/8", "2001:db8::/32")
    assert client_address(_scope("10.0.0.5", "198.51.100.7, garbage"), nets) == "10.0.0.5"
    assert client_address(_scope("2001:db8::1", "2001:0db8::2, 198.51.100.7"), nets) == (
        "198.51.100.7"
    )


def test_trusted_proxies_are_validated():
    from akko_mcp_trino.config import trusted_networks

    assert [str(n) for n in trusted_networks(" 10.0.0.0/8 , 192.0.2.1,, ")] == [
        "10.0.0.0/8",
        "192.0.2.1/32",
    ]
    with pytest.raises(ValueError, match="MCP_TRUSTED_PROXIES: 'ten' is not"):
        trusted_networks("10.0.0.0/8,ten")


# ---- refusals at the door


class _Check:
    """A provider that says why it refuses, like JwksJwtAuth."""

    def __init__(self, verdict):
        self.verdict = verdict

    def check(self, headers):
        return self.verdict

    def verify(self, headers):  # pragma: no cover - the middleware asks check
        return self.verdict.principal


class _Verify:
    """A provider of 0.8 that only says who, never why."""

    def __init__(self, principal):
        self._p = principal

    def verify(self, headers):
        return self._p


def _app(provider, *, extensions=(), inner_status=200, proxies=(), audit=None, metrics=None):
    async def endpoint(request):
        if inner_status == 200:
            return JSONResponse({"ok": True})
        return PlainTextResponse("no", status_code=inner_status)

    app = Starlette(routes=[Route("/mcp", endpoint, methods=["GET", "POST"])])
    app.add_middleware(
        AuthIdentityMiddleware,
        auth_provider=provider,
        require_auth=True,
        extensions=tuple(extensions),
        audit=audit,
        metrics=metrics,
        trusted_proxies=proxies,
    )
    return app


def _refusals(metrics: Metrics) -> dict[str, float]:
    return {
        s.labels["reason"]: s.value
        for family in metrics.registry.collect()
        if family.name == "mcp_trino_request_refusals"
        for s in family.samples
        if s.name.endswith("_total")
    }


def test_a_request_without_a_token_is_refused_audited_and_counted():
    sink, metrics = InMemoryAudit(), Metrics()
    client = TestClient(_app(_Check(TokenVerdict(None)), audit=sink, metrics=metrics))
    r = client.post("/mcp")
    assert r.status_code == 401 and r.headers["x-reason"] == "unauthenticated"
    (event,) = sink.events
    line = event.as_dict()
    assert line["event"] == "refused" and line["reason"] == "missing_token"
    assert line["status"] == 401 and TS.match(line["ts"])
    assert line["request_id"] == r.headers["x-request-id"]
    assert line["client_ip"] == "testclient" and line["subject"] == ""
    assert _refusals(metrics) == {"missing_token": 1.0}


def test_an_invalid_token_is_refused_with_its_reason_and_never_written():
    sink, metrics = InMemoryAudit(), Metrics()
    provider = _Check(TokenVerdict(None, "wrong_audience"))
    client = TestClient(_app(provider, audit=sink, metrics=metrics))
    r = client.post("/mcp", headers={"Authorization": f"Bearer {TOKEN}", "X-Request-Id": "e-1"})
    assert r.status_code == 401
    line = sink.events[0].as_dict()
    assert (line["reason"], line["request_id"], line["subject"]) == ("wrong_audience", "e-1", "")
    text = json.dumps(line)
    assert TOKEN not in text and "eyJ" not in text and "mallory" not in text
    assert _refusals(metrics) == {"wrong_audience": 1.0}


def test_a_provider_that_does_not_say_why_refuses_as_invalid_token():
    sink = InMemoryAudit()
    TestClient(_app(_Verify(None), audit=sink)).post(
        "/mcp", headers={"Authorization": f"Bearer {TOKEN}"}
    )
    assert sink.events[0].reason == "invalid_token"


def test_a_check_without_a_reason_still_refuses_as_invalid_token():
    sink = InMemoryAudit()
    TestClient(_app(_Check(TokenVerdict(None)), audit=sink)).post(
        "/mcp", headers={"Authorization": f"Bearer {TOKEN}"}
    )
    assert sink.events[0].reason == "invalid_token"


class _Guard(Extension):
    name = "guard"

    def __init__(self, before=None, after=None):
        self._before, self._after = before, after

    def before_auth(self, admission):
        return self._before

    def after_auth(self, admission):
        return self._after


def test_an_extensions_refusal_after_the_token_names_the_verified_caller():
    sink, metrics = InMemoryAudit(), Metrics()
    alice = Principal(subject="alice", client_id="agent-x")
    refusal = Refusal("insufficient_scope", status=403, error="insufficient_scope")
    app = _app(
        _Check(TokenVerdict(alice)), extensions=[_Guard(after=refusal)], audit=sink, metrics=metrics
    )
    r = TestClient(app).post("/mcp", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 403
    line = sink.events[0].as_dict()
    assert (line["reason"], line["status"], line["subject"], line["client_id"]) == (
        "insufficient_scope",
        403,
        "alice",
        "agent-x",
    )
    assert _refusals(metrics) == {"insufficient_scope": 1.0}


@pytest.mark.parametrize(
    ("headers", "verdict", "reason"),
    [
        ({}, TokenVerdict(None), "missing_token"),
        (
            {"Authorization": f"Bearer {TOKEN}"},
            TokenVerdict(None, "token_expired"),
            "token_expired",
        ),
        ({"Authorization": f"Bearer {TOKEN}"}, TokenVerdict(None), "invalid_token"),
    ],
)
def test_token_policy_behind_a_lenient_door_audits_why_as_the_core_does(headers, verdict, reason):
    """MCP_AUTH_REQUIRED=false lets a request without a verified token through the door;
    token-policy refuses it with the core's 401, and the line says why."""
    from akko_mcp_trino.modules import token_policy
    from tests.tool_doubles import core_config

    policy = token_policy.create(core_config(), {"MCP_ALLOWED_CLIENTS": "agent"})
    sink = InMemoryAudit()
    app = Starlette(routes=[Route("/mcp", lambda r: JSONResponse({}), methods=["POST"])])
    app.add_middleware(
        AuthIdentityMiddleware,
        auth_provider=_Check(verdict),
        require_auth=False,
        extensions=(policy,),
        audit=sink,
    )
    r = TestClient(app).post("/mcp", headers=headers)
    assert r.status_code == 401 and r.headers["x-reason"] == "unauthenticated"
    assert sink.events[0].reason == reason


def test_an_extensions_odd_reason_is_kept_out_of_the_counters_labels():
    sink, metrics = InMemoryAudit(), Metrics()
    odd = Refusal("Client Not Allowed: " + "x" * 300)
    app = _app(
        _Check(TokenVerdict(None)), extensions=[_Guard(before=odd)], audit=sink, metrics=metrics
    )
    TestClient(app).post("/mcp")
    assert sink.events[0].reason == "extension_refused"
    assert _refusals(metrics) == {"extension_refused": 1.0}
    assert refusal_reason("client_not_allowed") == "client_not_allowed"


@pytest.mark.parametrize(
    ("status", "headers", "reason"),
    [
        (421, {}, "host_not_allowed"),
        (413, {}, "payload_too_large"),
        (403, {}, "origin_not_allowed"),
        (400, {}, "bad_request"),
        (406, {}, "not_acceptable"),
        (415, {}, "unsupported_media_type"),
        (404, {"mcp-session-id": "inventee"}, "session_invalid"),
        (404, {}, "not_found"),
        (418, {}, "transport_refused"),
    ],
)
def test_a_refusal_of_the_transport_behind_the_door_is_audited(status, headers, reason):
    sink, metrics = InMemoryAudit(), Metrics()
    alice = Principal(subject="alice")
    app = _app(_Check(TokenVerdict(alice)), inner_status=status, audit=sink, metrics=metrics)
    r = TestClient(app).post("/mcp", headers={"Authorization": f"Bearer {TOKEN}", **headers})
    assert r.status_code == status
    line = sink.events[0].as_dict()
    assert (line["reason"], line["status"], line["subject"]) == (reason, status, "alice")
    assert _refusals(metrics) == {reason: 1.0}


def test_an_sse_message_to_an_unknown_session_is_a_session_refusal():
    sink = InMemoryAudit()
    app = _app(_Check(TokenVerdict(Principal(subject="a"))), inner_status=404, audit=sink)
    TestClient(app).post("/mcp?session_id=abc", headers={"Authorization": "Bearer t"})
    assert sink.events[0].reason == "session_invalid"


@pytest.mark.parametrize("status", [200, 500])
def test_an_answer_or_a_server_error_is_no_refusal(status):
    sink = InMemoryAudit()
    app = _app(_Check(TokenVerdict(Principal(subject="a"))), inner_status=status, audit=sink)
    TestClient(app, raise_server_exceptions=False).post(
        "/mcp", headers={"Authorization": "Bearer t"}
    )
    assert sink.events == []


def test_the_address_of_a_refused_request_is_read_behind_a_trusted_proxy():
    sink = InMemoryAudit()
    app = _app(_Check(TokenVerdict(None)), audit=sink, proxies=_nets("10.0.0.0/8"))

    async def run():
        import httpx

        transport = httpx.ASGITransport(app=app, client=("10.0.0.5", 4242))
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            await c.post("/mcp", headers={"X-Forwarded-For": "6.6.6.6, 198.51.100.7"})

    anyio.run(run)
    assert sink.events[0].client_ip == "198.51.100.7"


def test_a_failing_audit_sink_never_changes_the_refusal(caplog):
    class _Broken:
        def record(self, event):
            raise OSError("disk full")

    with caplog.at_level(logging.ERROR, logger="akko_mcp_trino.middleware"):
        r = TestClient(_app(_Check(TokenVerdict(None)), audit=_Broken())).post("/mcp")
    assert r.status_code == 401
    assert "audit line of a refusal was not written: OSError" in caplog.text


def test_without_an_audit_sink_a_refusal_is_still_counted():
    metrics = Metrics()
    TestClient(_app(_Check(TokenVerdict(None)), metrics=metrics)).post("/mcp")
    assert _refusals(metrics) == {"missing_token": 1.0}


# ---- the verification of a token says why


def test_the_refusal_code_of_each_verification_error():
    import jwt

    from akko_mcp_trino.auth import TOKEN_REFUSAL_CODES, token_refusal_code

    cases = {
        jwt.ExpiredSignatureError: "token_expired",
        jwt.ImmatureSignatureError: "token_not_yet_valid",
        jwt.InvalidAudienceError: "wrong_audience",
        jwt.InvalidIssuerError: "wrong_issuer",
        jwt.InvalidSignatureError: "bad_signature",
        jwt.DecodeError: "invalid_token",
        jwt.InvalidAlgorithmError: "wrong_algorithm",
        jwt.PyJWKClientConnectionError: "jwks_unreachable",
        jwt.PyJWKClientError: "unknown_key",
        ValueError: "invalid_token",
    }
    for exc, code in cases.items():
        assert token_refusal_code(exc("x")) == code
        assert code in TOKEN_REFUSAL_CODES
    missing = jwt.MissingRequiredClaimError("exp")
    assert token_refusal_code(missing) == "missing_claim"


def test_the_jwks_provider_says_why_it_refuses():
    from akko_mcp_trino import auth
    from tests.test_auth import _ISS, _OTHER, _bearer, _jwks_provider, _sign

    provider = _jwks_provider()
    assert provider.check({}) == TokenVerdict(None, "")
    assert provider.check(_bearer(_sign({"sub": "a"}, exp_delta=-3600))).reason == "token_expired"
    assert provider.check(_bearer(_sign({"sub": "a"}, aud="other"))).reason == "wrong_audience"
    assert provider.check(_bearer(_sign({"sub": "a"}, iss="https://x"))).reason == "wrong_issuer"
    assert provider.check(_bearer(_sign({"sub": "a"}, key=_OTHER))).reason == "bad_signature"
    assert provider.check(_bearer("not-a-jwt")).reason == "invalid_token"
    assert provider.check(_bearer(_sign({}))).reason == "no_user_claim"
    verified = provider.check(_bearer(_sign({"sub": "alice", "azp": "agent"})))
    assert verified.principal.subject == "alice" and verified.reason == ""
    refused = auth.JwksJwtAuth(
        "https://kc.example/jwks",
        _ISS,
        ["akko-mcp", "trino"],
        jwks_client=provider._client,
        refused_audiences=["trino"],
    )
    assert refused.check(_bearer(_sign({"sub": "a"}, aud=["akko-mcp", "trino"]))).reason == (
        "wrong_audience"
    )


# ---- stdio


def _stdio_config(**over):
    from tests.tool_doubles import core_config

    return core_config(transport="stdio", **over)


class _StdioMCP:
    def __init__(self):
        self.middleware = []
        self.ran = False

    def run(self, transport):
        self.ran = True


def test_a_stdio_refusal_at_start_is_audited_before_the_server_stops():
    from akko_mcp_trino.app import run_stdio

    sink = InMemoryAudit()
    cfg = _stdio_config(auth_enabled=True, auth_required=True)
    with pytest.raises(PermissionError):
        run_stdio(cfg, _StdioMCP(), auth_provider=_Check(TokenVerdict(None)), audit=sink)
    with pytest.raises(PermissionError):
        run_stdio(
            replace(cfg, user_token=TOKEN),
            _StdioMCP(),
            auth_provider=_Check(TokenVerdict(None, "token_expired")),
            audit=sink,
        )
    with pytest.raises(PermissionError):
        run_stdio(
            replace(cfg, user_token=TOKEN), _StdioMCP(), auth_provider=_Verify(None), audit=sink
        )
    assert [(e.reason, e.status) for e in sink.events] == [
        ("missing_token", 0),
        ("token_expired", 0),
        ("invalid_token", 0),
    ]
    assert all(e.request_id and e.client_ip == "" for e in sink.events)
    assert TOKEN not in json.dumps([e.as_dict() for e in sink.events])


def test_a_stdio_admission_refused_by_an_extension_is_audited():
    from akko_mcp_trino.app import run_stdio

    sink = InMemoryAudit()
    alice = Principal(subject="alice", client_id="desk")
    cfg = _stdio_config(auth_enabled=True, user_token=TOKEN)
    with pytest.raises(PermissionError, match="client_not_allowed"):
        run_stdio(
            cfg,
            _StdioMCP(),
            auth_provider=_Check(TokenVerdict(alice)),
            extensions=[_Guard(after=Refusal("client_not_allowed"))],
            audit=sink,
        )
    line = sink.events[0].as_dict()
    assert (line["reason"], line["subject"], line["client_id"]) == (
        "client_not_allowed",
        "alice",
        "desk",
    )


def test_a_stdio_server_without_a_sink_refuses_as_before():
    from akko_mcp_trino.app import run_stdio

    cfg = _stdio_config(auth_enabled=True, auth_required=True)
    with pytest.raises(PermissionError):
        run_stdio(cfg, _StdioMCP(), auth_provider=_Check(TokenVerdict(None)))


# ---- who Trino saw, and the queries of a call


class _Cursor:
    description = [("x",)]

    def __init__(self, query_id, fail=None):
        self.query_id = query_id
        self._fail = fail

    def execute(self, sql, params=None):
        if self._fail is not None:
            raise self._fail

    def fetchmany(self, n):
        return [(1,)]


class _Conn:
    def __init__(self, user, cursor):
        self.user = user
        self._cursor = cursor

    def cursor(self):
        return self._cursor


def _client_with(monkeypatch, cursor, cfg=None, metrics=None):
    import akko_mcp_trino.trino_client as tc
    from tests.tool_doubles import core_config

    cfg = cfg or core_config()
    monkeypatch.setattr(tc.trino.dbapi, "connect", lambda **kw: _Conn(kw["user"], cursor))
    return tc.TrinoClient(cfg, metrics=metrics)


def test_a_query_of_a_call_notes_its_id_and_the_user_trino_saw(monkeypatch):
    client = _client_with(monkeypatch, _Cursor("20261128_091502_00001_abcde"))
    call, token = bind_call("execute_query")
    try:
        client.query("SELECT 1", user="alice")
    finally:
        reset_call(token)
    assert call.query_ids == ["20261128_091502_00001_abcde"] and call.trino_user == "svc"


def test_a_failed_query_still_notes_its_id(monkeypatch):
    client = _client_with(monkeypatch, _Cursor("q-failed", fail=RuntimeError("denied")))
    call, token = bind_call("execute_query")
    try:
        with pytest.raises(RuntimeError):
            client.query("SELECT 1")
    finally:
        reset_call(token)
    assert call.query_ids == ["q-failed"]


def test_a_query_trino_named_no_user_for_notes_the_caller_and_no_odd_id(monkeypatch):
    import akko_mcp_trino.trino_client as tc
    from tests.tool_doubles import core_config

    class _NoUser:
        def describe(self):
            return []

        def connection(self, user):
            return None, None

        def ping(self, client):  # pragma: no cover - not probed here
            pass

    monkeypatch.setattr(tc.trino.dbapi, "connect", lambda **kw: _Conn(None, _Cursor(None)))
    client = tc.TrinoClient(core_config(), identity=_NoUser())
    call, token = bind_call("execute_query")
    try:
        client.query("SELECT 1", user="bob")
    finally:
        reset_call(token)
    assert call.query_ids == [] and call.trino_user == "bob"


def test_the_readiness_probe_is_not_counted_as_a_query(monkeypatch):
    metrics = Metrics()
    client = _client_with(monkeypatch, _Cursor("q-probe"), metrics=metrics)
    client.ping()
    client.ping()
    client.query("SELECT 1")
    assert metrics.queries._value.get() == 1, "two probes, one query"


def test_a_tool_call_line_says_who_trino_saw_from_where_with_which_client(monkeypatch):
    import akko_mcp_trino.trino_client as tc
    from akko_mcp_trino.server import build_server
    from tests.tool_doubles import FakeMCP, core_config, in_request

    monkeypatch.setattr(tc.trino.dbapi, "connect", lambda **kw: _Conn(kw["user"], _Cursor("q-42")))
    sink = InMemoryAudit()
    mcp, _ = build_server(core_config(), mcp_factory=lambda name, **_: FakeMCP(), audit=sink)

    def call():
        set_current_client_ip("198.51.100.7")
        set_current_mcp_client("claude-code/2.1.0")
        return mcp.tools["execute_query"]("SELECT 1")

    import contextvars

    contextvars.copy_context().run(in_request, call)
    line = sink.events[0].as_dict()
    assert {k: line[k] for k in ("subject", "trino_user", "identity_mode", "query_ids")} == {
        "subject": "alice_admin",
        "trino_user": "svc",
        "identity_mode": "service",
        "query_ids": ["q-42"],
    }
    assert line["client_ip"] == "198.51.100.7" and line["mcp_client"] == "claude-code/2.1.0"
    assert "SELECT" not in json.dumps(line)


def test_the_mcp_client_is_bound_from_what_it_declared(monkeypatch):
    from akko_mcp_trino.audit import current_mcp_client
    from akko_mcp_trino.server import bind_mcp_client

    seen = []

    async def call_next(ctx):
        seen.append(current_mcp_client())
        return "done"

    info = SimpleNamespace(name="vibe", version="2.0")
    declared = SimpleNamespace(
        session=SimpleNamespace(client_params=SimpleNamespace(client_info=info))
    )
    silent = SimpleNamespace(session=SimpleNamespace(client_params=None))
    assert anyio.run(bind_mcp_client, declared, call_next) == "done"
    anyio.run(bind_mcp_client, silent, call_next)
    anyio.run(bind_mcp_client, SimpleNamespace(), call_next)
    assert seen == ["vibe/2.0", "", ""] and current_mcp_client() == ""


def test_the_server_installs_the_binding_when_the_sdk_has_middleware():
    from akko_mcp_trino.server import bind_mcp_client, build_server
    from tests.tool_doubles import FakeMCP, core_config

    class _WithMiddleware(FakeMCP):
        def __init__(self):
            super().__init__()
            self.middleware = []

    mcp, _ = build_server(core_config(), mcp_factory=lambda name, **_: _WithMiddleware())
    assert mcp.middleware == [bind_mcp_client]


# ---- readiness


def test_each_reason_not_to_be_ready_has_its_code():
    import ssl

    import httpx
    import trino.exceptions as te
    import urllib3.exceptions

    from akko_mcp_trino.health import READINESS_CODES, readiness_code
    from akko_mcp_trino.trino_client import TrinoNotReady

    def chained(outer, inner):
        try:
            try:
                raise inner
            except Exception as exc:
                raise outer from exc
        except Exception as exc:
            return exc

    cases = [
        (TrinoNotReady("trino_starting", "Trino is starting"), "trino_starting"),
        (TrinoNotReady("odd", "?"), "trino_error"),
        (PermissionError("no verified caller"), "identity_refused"),
        (te.TrinoAuthError("x"), "trino_auth_failed"),
        (te.HttpError("error 401: Unauthorized"), "trino_auth_failed"),
        (te.HttpError("error 403: Forbidden"), "trino_permission_denied"),
        (te.Http503Error("error 503: service unavailable"), "trino_unavailable"),
        (te.HttpError("error 418"), "trino_http_error"),
        (te.HttpError("odd"), "trino_http_error"),
        (te.TrinoUserError({"errorName": "PERMISSION_DENIED"}), "trino_permission_denied"),
        (te.TrinoUserError({"errorName": "SYNTAX_ERROR"}), "trino_query_failed"),
        (chained(te.TrinoConnectionError("x"), ssl.SSLError("cert")), "trino_tls_failed"),
        (chained(RuntimeError("x"), urllib3.exceptions.SSLError("x")), "trino_tls_failed"),
        (httpx.ConnectTimeout("x"), "trino_timeout"),
        (TimeoutError("x"), "trino_timeout"),
        (te.TrinoConnectionError("failed to execute"), "trino_unreachable"),
        (httpx.ConnectError("x"), "trino_unreachable"),
        (ConnectionRefusedError("x"), "trino_unreachable"),
        (
            chained(RuntimeError("x"), urllib3.exceptions.NewConnectionError(None, "x")),
            "trino_unreachable",
        ),
        (
            chained(RuntimeError("x"), urllib3.exceptions.NameResolutionError("h", None, "x")),
            "trino_unreachable",
        ),
        (
            chained(RuntimeError("x"), urllib3.exceptions.ReadTimeoutError(None, "/", "x")),
            "trino_timeout",
        ),
        (RuntimeError("anything else"), "trino_error"),
    ]
    for exc, code in cases:
        assert readiness_code(exc) == code, exc
        assert code in READINESS_CODES


def test_the_reason_goes_to_the_log_once_until_it_changes_and_the_probes_count(caplog):
    from akko_mcp_trino.health import build_health_app

    failures = [ConnectionRefusedError("trino.internal:8443"), None]

    class _Client:
        def ping(self):
            if failures[0] is not None:
                raise failures[0]

    metrics = Metrics()
    client = TestClient(build_health_app(_Client(), metrics=metrics))
    with caplog.at_level(logging.INFO, logger="akko_mcp_trino.health"):
        assert client.get("/ready").status_code == 503
        assert client.get("/ready").status_code == 503
        failures[0] = None
        assert client.get("/ready").status_code == 200
        assert client.get("/ready").status_code == 200
    messages = [r.getMessage() for r in caplog.records]
    assert messages == [
        "not ready: trino_unreachable (ConnectionRefusedError: trino.internal:8443)",
        "ready again after trino_unreachable",
    ]
    body = metrics.render()[0].decode()
    assert 'mcp_trino_ready_checks_total{result="trino_unreachable"} 2.0' in body
    assert 'mcp_trino_ready_checks_total{result="ready"} 2.0' in body


# ---- OpenTelemetry


def test_an_unset_tracer_provider_is_left_alone():
    env: dict[str, str] = {}
    assert otel.drop_missing_tracer_provider(env) == "" and env == {}


def test_a_tracer_provider_that_is_installed_is_left_alone(monkeypatch):
    found = SimpleNamespace(name="sdk_tracer_provider")
    monkeypatch.setattr(otel, "entry_points", lambda group: [found])
    env = {"OTEL_PYTHON_TRACER_PROVIDER": "sdk_tracer_provider"}
    assert otel.drop_missing_tracer_provider(env) == ""
    assert env == {"OTEL_PYTHON_TRACER_PROVIDER": "sdk_tracer_provider"}


def test_a_tracer_provider_that_is_not_installed_is_dropped_with_a_warning(monkeypatch):
    monkeypatch.setattr(otel, "entry_points", lambda group: [])
    env = {"OTEL_PYTHON_TRACER_PROVIDER": "sdk_tracer_provider", "OTHER": "1"}
    notice = otel.drop_missing_tracer_provider(env)
    assert env == {"OTHER": "1"}
    assert "OTEL_PYTHON_TRACER_PROVIDER='sdk_tracer_provider'" in notice
    assert "emits no span" in notice


def _python(code: str, **env: str) -> subprocess.CompletedProcess:
    import os

    clean = {k: v for k, v in os.environ.items() if not k.startswith("OTEL_")}
    return subprocess.run(
        [sys.executable, "-c", code],
        env={**clean, **env},
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_the_server_starts_when_the_environment_names_a_tracer_provider_it_lacks():
    """Without the guard, the MCP SDK's first import raises (proven by the second run)."""
    variable = {"OTEL_PYTHON_TRACER_PROVIDER": "sdk_tracer_provider"}
    served = _python(
        "import akko_mcp_trino, akko_mcp_trino.server, os;"
        "print(bool(akko_mcp_trino.OTEL_NOTICE), 'OTEL_PYTHON_TRACER_PROVIDER' in os.environ)",
        **variable,
    )
    assert served.returncode == 0, served.stderr
    assert served.stdout.split() == ["True", "False"]
    bare = _python("import mcp.server.lowlevel", **variable)
    assert bare.returncode != 0 and "StopIteration" in bare.stderr


# ---- configuration and --check


def test_the_log_format_and_the_proxies_are_read_and_validated(monkeypatch):
    from akko_mcp_trino.config import Config
    from tests.tool_doubles import core_config

    monkeypatch.setenv("MCP_LOG_FORMAT", " JSON ")
    monkeypatch.setenv("MCP_TRUSTED_PROXIES", "10.0.0.0/8")
    cfg = Config.from_env()
    assert (cfg.log_format, cfg.trusted_proxies) == ("json", "10.0.0.0/8")
    assert Config.__dataclass_fields__["log_format"].default == "text"
    assert Config.__dataclass_fields__["trusted_proxies"].default == ""
    problems = core_config(log_format="xml", trusted_proxies="10.0.0.0/33").problems()
    assert "MCP_LOG_FORMAT='xml': expected one of text, json" in problems
    assert any(p.startswith("MCP_TRUSTED_PROXIES: '10.0.0.0/33'") for p in problems)


def test_check_says_how_the_audit_is_written(monkeypatch, capsys):
    from akko_mcp_trino import __main__ as entry
    from tests.tool_doubles import core_config

    monkeypatch.setattr(entry, "OTEL_NOTICE", "OTEL_PYTHON_TRACER_PROVIDER='x' names no ...")
    entry.check_config(core_config(log_format="json", trusted_proxies="10.0.0.0/8"))
    out = capsys.readouterr().out
    assert "refusals audited; log lines JSON (MCP_LOG_FORMAT=json); client address from " in out
    assert "X-Forwarded-For behind 10.0.0.0/8" in out
    assert "WARNING: OTEL_PYTHON_TRACER_PROVIDER='x'" in out
    monkeypatch.setattr(entry, "OTEL_NOTICE", "")
    entry.check_config(core_config())
    out = capsys.readouterr().out
    assert "log lines text; client address from the peer (no trusted proxy)" in out
    assert "OTEL_PYTHON" not in out


def test_the_asgi_app_audits_refusals_through_the_sink_it_is_given():
    from akko_mcp_trino.app import build_asgi_app
    from tests.tool_doubles import core_config

    class _MCP:
        def streamable_http_app(self, *, transport_security=None):
            return Starlette()

    sink, metrics = InMemoryAudit(), Metrics()
    app = build_asgi_app(
        core_config(auth_enabled=True, auth_required=True, trusted_proxies="10.0.0.0/8"),
        _MCP(),
        auth_provider=_Check(TokenVerdict(None)),
        audit=sink,
        metrics=metrics,
    )
    assert TestClient(app).post("/mcp").status_code == 401
    assert sink.events[0].reason == "missing_token"
    assert _refusals(metrics) == {"missing_token": 1.0}


def test_a_cycle_of_exceptions_ends_the_reading():
    from akko_mcp_trino.health import readiness_code

    exc = RuntimeError("a")
    exc.__context__ = exc  # a chain that loops never ends the probe
    assert readiness_code(exc) == "trino_error"


def test_a_key_missing_because_the_jwks_could_not_be_read_says_so():
    """The keys' cache tells an unreadable JWKS (jwks_unreachable) from a kid it does not
    hold once the JWKS was read (unknown_key), and goes back once a read succeeds."""
    import jwt

    from akko_mcp_trino.auth import token_refusal_code
    from tests.test_jwks_keys import _KEY, _OTHER, _Clock, _Issuer, _jwk, _keys, _token

    issuer, clock = _Issuer(_jwk(_KEY, "k1")), _Clock()
    issuer.down = True
    keys = _keys(issuer, clock)
    with pytest.raises(jwt.PyJWKClientError) as down:
        keys.get_signing_key_from_jwt(_token("k1"))
    assert token_refusal_code(down.value) == "jwks_unreachable"
    issuer.down = False
    clock.now += 30
    assert keys.get_signing_key_from_jwt(_token("k1")).key_id == "k1"
    with pytest.raises(jwt.PyJWKClientError) as unknown:
        keys.get_signing_key_from_jwt(_token("k9", _OTHER))
    assert token_refusal_code(unknown.value) == "unknown_key"
