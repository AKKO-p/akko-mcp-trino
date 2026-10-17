"""The extension points: what another package adds, chosen by name, never in place of the core.

An extension is loaded only when ``MCP_EXTENSIONS`` names it; an unknown name or
a setting it cannot serve refuses to start. Once loaded, its guards run around
the core's token verification and can only refuse; its routes sit behind the
guard; its audit wraps the core's; its identity modes, client tags, describers
and tools are applied by the server's assembly.
"""

from __future__ import annotations

import contextvars
import json

import anyio
import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from akko_mcp_trino import extensions as ext_module
from akko_mcp_trino.app import build_asgi_app, run_stdio
from akko_mcp_trino.audit import InMemoryAudit
from akko_mcp_trino.auth import Principal
from akko_mcp_trino.config import Config
from akko_mcp_trino.extensions import Admission, Extension, Refusal, load_extensions
from akko_mcp_trino.identity import current_principal
from akko_mcp_trino.middleware import AuthIdentityMiddleware
from akko_mcp_trino.server import build_server
from tests.test_tools import FakeMCP

_SEEN: contextvars.ContextVar = contextvars.ContextVar("seen", default=None)


def _config(**over) -> Config:
    base = dict(
        trino_host="h",
        trino_identity_mode="service",
        query_timeout_seconds=0.0,
        call_timeout_seconds=0.0,
        trino_port=8443,
        trino_user="svc",
        trino_catalog="c",
        max_rows=10,
        read_only=True,
        auth_enabled=False,
        server_name="t",
        health_port=3001,
        jwks_url="",
        oidc_issuer="",
        oidc_audience="",
        transport="sse",
        mcp_port=3000,
        auth_required=False,
        trino_http_scheme="https",
    )
    base.update(over)
    return Config(**base)


class _Auth:
    def __init__(self, principal=None):
        self._p = principal

    def verify(self, headers):
        return self._p if headers.get("authorization") else None


class _Recorder:
    def __init__(self):
        self.seen = []

    async def __call__(self, scope, receive, send):
        self.seen.append((current_principal(), _SEEN.get()))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


async def _call(app, path="/mcp", headers=None):
    sent = []

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(m):
        sent.append(m)

    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    await app({"type": "http", "path": path, "headers": raw}, receive, send)
    start = next(m for m in sent if m["type"] == "http.response.start")
    hdrs = {k.decode(): v.decode() for k, v in start["headers"]}
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], hdrs, body


class _Guarding(Extension):
    """Refuses without a key before the token, refuses one subject after it, binds a value."""

    name = "guarding"

    def before_auth(self, admission):
        if admission.headers.get("x-key") != "k":
            return Refusal("key_missing")
        admission.bind(_SEEN, "bound")
        return None

    def after_auth(self, admission):
        if admission.principal is not None and admission.principal.subject == "mallory":
            return Refusal("forbidden_subject", status=403, error="insufficient_scope")
        if admission.headers.get("x-slow"):
            return Refusal("too_many_calls", status=429, headers={"Retry-After": "7"})
        return None

    def challenge(self, refusal):
        return f'Bearer error="{refusal.error}"' if refusal.status == 403 else None

    def public_documents(self):
        return {"/.well-known/doc": {"hello": "world"}}


# ---- the base class adds nothing


def test_the_base_extension_adds_nothing():
    e = Extension()
    a = Admission(headers={})
    assert (e.check_lines(), dict(e.identity_modes()), e.audiences()) == ([], {}, [])
    assert e.before_auth(a) is None and e.after_auth(a) is None
    assert dict(e.public_documents()) == {} and e.challenge(Refusal("x")) is None
    assert e.routes() == [] and e.client_tags() == [] and e.describe_table_doc() == ""
    sink = object()
    assert e.wrap_audit(sink) is sink and e.tool_registrars() == []


# ---- loading: by name only, in order, refused when unknown or unservable


class _EP:
    def __init__(self, name, factory):
        self.name, self._factory = name, factory

    def load(self):
        return self._factory


def test_nothing_is_loaded_unless_named(monkeypatch):
    monkeypatch.setattr(ext_module, "_entry_points", lambda: pytest.fail("looked up"))
    assert load_extensions(_config(), {}) == []


def test_named_extensions_are_built_in_order_from_the_environment(monkeypatch):
    built = []

    def factory(name):
        def make(config, env):
            built.append((name, config.server_name, env["X"]))
            return Extension()

        return make

    monkeypatch.setattr(
        ext_module, "_entry_points", lambda: [_EP("a", factory("a")), _EP("b", factory("b"))]
    )
    assert len(load_extensions(_config(extensions="b, a"), {"X": "1"})) == 2
    assert built == [("b", "t", "1"), ("a", "t", "1")]


def test_an_unknown_name_refuses_to_start_and_names_what_is_installed(monkeypatch):
    monkeypatch.setattr(ext_module, "_entry_points", lambda: [_EP("real", lambda c, e: None)])
    with pytest.raises(ValueError, match=r"unknown extension 'typo'.*\['real'\]"):
        load_extensions(_config(extensions="typo"), {})
    monkeypatch.setattr(ext_module, "_entry_points", lambda: [])
    with pytest.raises(ValueError, match="installed extensions: none"):
        load_extensions(_config(extensions="typo"), {})


def test_the_real_entry_point_group_is_read(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        ext_module, "entry_points", lambda group: seen.setdefault("g", group) and []
    )
    with pytest.raises(ValueError):
        load_extensions(_config(extensions="x"), {})
    assert seen["g"] == "akko_mcp_trino.extensions"


# ---- the door: guards around the core's verification, never in place of it


def test_a_guard_before_the_token_refuses_with_its_reason():
    app = AuthIdentityMiddleware(_Recorder(), _Auth(), extensions=[_Guarding()])
    status, hdrs, body = anyio.run(_call, app)
    assert (status, hdrs["x-reason"], json.loads(body)) == (
        401,
        "key_missing",
        {"error": "key_missing"},
    )
    assert "www-authenticate" not in hdrs


def test_the_core_still_refuses_an_unauthenticated_request_in_strict_mode():
    app = AuthIdentityMiddleware(_Recorder(), _Auth(), require_auth=True, extensions=[_Guarding()])
    status, hdrs, _ = anyio.run(_call, app, "/mcp", {"x-key": "k"})
    assert (status, hdrs["x-reason"]) == (401, "unauthenticated")


def test_a_guard_after_the_token_refuses_with_its_status_challenge_and_headers():
    app = AuthIdentityMiddleware(
        _Recorder(), _Auth(Principal(subject="mallory")), extensions=[_Guarding()]
    )
    status, hdrs, _ = anyio.run(_call, app, "/mcp", {"x-key": "k", "authorization": "Bearer t"})
    assert (status, hdrs["www-authenticate"]) == (403, 'Bearer error="insufficient_scope"')
    status, hdrs, _ = anyio.run(_call, app, "/mcp", {"x-key": "k", "x-slow": "1"})
    assert (status, hdrs["retry-after"]) == (429, "7")


def test_a_request_the_guards_admit_carries_the_principal_and_the_bindings_then_resets():
    down = _Recorder()
    app = AuthIdentityMiddleware(down, _Auth(Principal(subject="alice")), extensions=[_Guarding()])
    status, _, _ = anyio.run(_call, app, "/mcp", {"x-key": "k", "authorization": "Bearer t"})
    assert status == 200 and down.seen[0][0].subject == "alice" and down.seen[0][1] == "bound"
    assert current_principal() is None and _SEEN.get() is None


def test_a_public_document_is_served_without_a_token():
    app = AuthIdentityMiddleware(_Recorder(), _Auth(), require_auth=True, extensions=[_Guarding()])
    status, _, body = anyio.run(_call, app, "/.well-known/doc")
    assert status == 200 and json.loads(body) == {"hello": "world"}


# ---- the served app: routes behind the guard, stdio under the same guards


class _FakeMCP:
    def sse_app(self, *, transport_security=None):
        return Starlette()

    streamable_http_app = sse_app


class _Routed(Extension):
    def routes(self):
        return [Route("/ops", lambda request: PlainTextResponse("ops"))]


def test_an_extensions_routes_are_served_behind_the_guard():
    app = build_asgi_app(
        _config(auth_required=True),
        _FakeMCP(),
        auth_provider=_Auth(Principal(subject="a")),
        extensions=[_Routed()],
    )
    client = TestClient(app)
    assert client.get("/ops").status_code == 401
    assert client.get("/ops", headers={"Authorization": "Bearer t"}).text == "ops"


class _Stdio:
    def __init__(self):
        self.ran = None

    def run(self, transport):
        self.ran = (transport, current_principal(), _SEEN.get())


def test_stdio_runs_the_guards_once_for_the_process():
    class _Binding(Extension):
        def after_auth(self, admission):
            assert admission.stdio is True
            admission.bind(_SEEN, "stdio")
            return None

    mcp = _Stdio()
    ctx = contextvars.copy_context()
    ctx.run(
        run_stdio,
        _config(transport="stdio", user_token="tok"),
        mcp,
        auth_provider=_Auth(Principal(subject="alice")),
        extensions=[_Binding()],
    )
    assert mcp.ran[0] == "stdio" and mcp.ran[1].subject == "alice" and mcp.ran[2] == "stdio"


def test_stdio_refuses_to_serve_a_token_a_guard_refuses():
    class _No(Extension):
        def after_auth(self, admission):
            return Refusal("client_not_allowed")

    with pytest.raises(PermissionError, match="client_not_allowed"):
        run_stdio(
            _config(transport="stdio", user_token="tok"),
            _Stdio(),
            auth_provider=_Auth(Principal(subject="alice")),
            extensions=[_No()],
        )


# ---- the assembly: identity modes, tags, audit, describers, tools


class _Everything(Extension):
    def __init__(self):
        self.order = []

    def identity_modes(self):
        class _Mode:
            def describe(self):
                return ["mode: stand-in"]

            def connection(self, user):
                return user or "svc", None

            def ping(self, client):
                return None

        return {"stand-in": _Mode()}

    def client_tags(self):
        return ["origin=test"]

    def wrap_audit(self, audit):
        class _Tagged:
            def record(self, event):
                audit.record(event.__class__(**{**event.__dict__, "extra": {"by": "ext"}}))

        return _Tagged()

    def describe_table_doc(self):
        return "Also the owner of the table."

    def tool_registrars(self):
        def register(mcp, client, tools):
            self.order.append("extension")

            def owner(cat, sch, tbl, rows):
                return {"owner": "data team"}

            def broken(cat, sch, tbl, rows):
                raise RuntimeError("catalogue down")

            tools.add_describer(owner)
            tools.add_describer(broken)

        return [register]


def test_build_server_applies_everything_an_extension_adds(monkeypatch):
    import akko_mcp_trino.trino_client as tc

    seen = {}

    class _Cur:
        description = [("c",)]

        def execute(self, sql, params=None):
            seen["sql"] = sql

        def fetchmany(self, n):
            return [("v",)]

    def connect(**kw):
        seen.update(kw)
        return type("C", (), {"cursor": lambda self: _Cur()})()

    monkeypatch.setattr(tc.trino.dbapi, "connect", connect)
    ext, audit = _Everything(), InMemoryAudit()

    def plugin(mcp, client):
        ext.order.append("plugin")

    mcp, _ = build_server(
        _config(trino_identity_mode="stand-in"),
        extensions=[ext],
        extra_tool_registrars=[plugin],
        mcp_factory=lambda name, **_: FakeMCP(),
        audit=audit,
    )
    assert ext.order == ["extension", "plugin"], "an extension's tools come before plugins'"
    out = json.loads(mcp.tools["describe_table"]("c", "s", "t"))
    assert out["owner"] == "data team" and out["rows"] == [["v"]]
    assert "Also the owner of the table." in mcp.tools["describe_table"].__doc__
    assert seen["client_tags"] == ["origin=test"] and seen["user"] == "svc"
    assert audit.events[-1].as_dict()["by"] == "ext"


def test_an_extension_that_adds_nothing_leaves_the_core_as_it_is():
    mcp, _ = build_server(
        _config(), extensions=[Extension()], mcp_factory=lambda name, **_: FakeMCP()
    )
    plain, _ = build_server(_config(), mcp_factory=lambda name, **_: FakeMCP())
    assert mcp.tools["describe_table"].__doc__ == plain.tools["describe_table"].__doc__
    app = build_asgi_app(_config(), _FakeMCP(), auth_provider=None, extensions=[Extension()])
    assert TestClient(app).get("/ops").status_code == 404


def test_check_names_the_extension_that_could_not_load_and_not_what_follows(monkeypatch, capsys):
    from akko_mcp_trino.__main__ import check_config

    monkeypatch.setenv("MCP_EXTENSIONS", "absent")
    monkeypatch.setenv("TRINO_IDENTITY_MODE", "service-account")
    assert check_config(Config.from_env()) == 2
    out = capsys.readouterr().out
    assert "unknown extension 'absent'" in out and "TRINO_IDENTITY_MODE" not in out
