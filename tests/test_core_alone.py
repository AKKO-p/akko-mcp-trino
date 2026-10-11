"""The core alone serves the basics, and nothing more slips into it unnoticed.

The core is what anyone may read and run: six SQL tools, the read-only guard
on the SQL tree, two Trino identities (the one the administrator configures, or
the caller's own token, verified again by Trino), bounds on rows
and time, one audit line per call. Every function beyond that lives in an extension and
is loaded only when ``MCP_EXTENSIONS`` names it. These tests list what the core
holds; they fail the day something is added without being listed here.
"""

from __future__ import annotations

import pathlib
import re
from dataclasses import fields

import pytest

from akko_mcp_trino.audit import AuditEvent
from akko_mcp_trino.config import Config
from akko_mcp_trino.server import build_server
from akko_mcp_trino.trino_client import resolve_identity
from tests.test_tools import FakeMCP

ROOT = pathlib.Path(__file__).resolve().parents[1]
CORE = ROOT / "akko_mcp_trino"

BASIC_TOOLS = {
    "list_catalogs",
    "list_schemas",
    "list_tables",
    "describe_table",
    "explain_query",
    "execute_query",
}

CORE_MODULES = {
    "__init__",
    "__main__",
    "app",
    "audit",
    "auth",
    "config",
    "extensions",
    "health",
    "identity",
    "metrics",
    "middleware",
    "plugins",
    "server",
    "sql_guard",
    "tools",
    "transport_security",
    "trino_client",
}

# The entry-point groups the core reads: its tool plugins and its extensions.
CORE_GROUPS = {"akko_mcp_trino.tools", "akko_mcp_trino.extensions"}


def _config(**over) -> Config:
    base = dict(
        trino_host="h",
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
        transport="streamable-http",
        mcp_port=3000,
        auth_required=False,
        trino_http_scheme="https",
    )
    base.update(over)
    return Config(**base)


def _source() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in sorted(CORE.glob("*.py")))


def test_the_core_serves_the_six_basic_tools_and_no_other():
    mcp, _ = build_server(_config(), mcp_factory=lambda name: FakeMCP())
    assert set(mcp.tools) == BASIC_TOOLS


def test_the_core_ships_its_listed_modules_and_no_other():
    assert {p.stem for p in CORE.glob("*.py")} == CORE_MODULES


def test_every_setting_the_core_reads_is_documented():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    read = set(re.findall(r'environ\.get\("([A-Z_]+)"', _source()))
    assert read, "the core reads its settings from the environment"
    assert {name for name in read if f"`{name}`" not in readme} == set()


def test_the_core_identity_modes_are_the_configured_one_and_the_callers_token():
    """Without an extension, a query runs as ``TRINO_USER`` (``service``, the default) or as
    the caller, whose own token Trino verifies (``jwt``); a mode the core does not know
    refuses to start rather than fall back to either."""
    assert type(resolve_identity(_config())).__name__ == "ServiceIdentity"
    assert type(resolve_identity(_config(trino_identity_mode="jwt"))).__name__ == "JwtIdentity"
    for mode in ("per-user", "token", "magic"):
        with pytest.raises(ValueError, match="expected one of \\('jwt', 'service'\\)"):
            resolve_identity(_config(trino_identity_mode=mode))
    assert "trino_identity_mode" in {f.name for f in fields(Config)}


def test_the_core_sends_trino_the_configured_password_or_the_callers_token_nothing_else():
    assert set(re.findall(r"trino\.auth\.(\w+)", _source())) == {
        "BasicAuthentication",
        "JWTAuthentication",
    }


def test_the_core_audit_line_has_its_six_fields_and_trino_gets_no_client_tag():
    line = AuditEvent(request_id="r", tool="t", subject="s", ok=True).as_dict()
    assert set(line) == {"request_id", "tool", "subject", "ok", "error", "duration_ms"}
    assert 'f"request_id=' not in _source() and 'f"tool=' not in _source()


def test_without_an_extension_no_public_document_is_served_and_no_challenge_is_sent():
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from akko_mcp_trino.app import build_asgi_app

    class _MCP:
        def streamable_http_app(self, *, transport_security=None):
            return Starlette()

    app = build_asgi_app(
        _config(auth_required=True),
        _MCP(),
        auth_provider=type("A", (), {"verify": lambda s, h: None})(),
    )
    client = TestClient(app)
    for path in ("/.well-known/any-document", "/.well-known/any-document/mcp"):
        assert client.get(path).status_code == 401
    refused = client.post("/mcp")
    assert refused.status_code == 401 and "www-authenticate" not in refused.headers


def test_the_core_reads_only_its_own_entry_point_groups():
    source = _source()
    named = set(re.findall(r"akko_mcp_trino\.[a-z_]+", source))
    groups = {name for name in named if name.removeprefix("akko_mcp_trino.") not in CORE_MODULES}
    assert groups <= CORE_GROUPS
